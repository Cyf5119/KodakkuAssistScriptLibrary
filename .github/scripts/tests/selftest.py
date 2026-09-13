#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内置自测：校验 pr_review 与 merge_repos 的核心规则。

从两个生产脚本里拆出来的——它们会在 CI 里无人值守地审核并合并 PR，还托管着会被
插件编译执行的脚本，改完之后必须能快速确认行为没变。这里不联网、不碰真实仓库，
只在临时目录里造样本。

    python .github/scripts/tests/selftest.py
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import shutil
import sys
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(SCRIPT_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import merge_repos  # noqa: E402  （被测试的模块）
import pr_review  # noqa: E402


# --------------------------------------------------------------------------- #
# pr_review：路径规则、.cs 校验、guid 查重、生成结果自检
# --------------------------------------------------------------------------- #


def run_pr_review_checks() -> list[str]:
    """跑 pr_review 的断言，返回失败项名称。"""
    validate_script_file = pr_review.validate_script_file
    validate_json_document = pr_review.validate_json_document
    check_path_rules = pr_review.check_path_rules
    check_change_set = pr_review.check_change_set
    check_guid_collisions = pr_review.check_guid_collisions
    default_config = pr_review.default_config
    DEFAULT_CONFIG = pr_review.DEFAULT_CONFIG
    UINT_MAX = pr_review.UINT_MAX

    cfg = default_config()
    failures: list[str] = []

    def expect(name, condition, detail=""):
        if condition:
            print(f"✅ {name}")
        else:
            failures.append(name)
            print(f"❌ {name} {detail}")

    GUID = "8010d865-7d6d-4c23-92e0-f4b0120e18ac"
    good_cs = (
        '[ScriptType(name: "M1s绘图", territorys: [1226], guid: "' + GUID + '", '
        'version: "0.0.0.9", author: "Karlin")]\npublic class M1s { }\n'
    )

    meta, errors, warnings = validate_script_file(good_cs, "Karlin-Z/M1s.cs", "Karlin-Z", cfg)
    expect("合法 .cs 通过", not errors and meta is not None, str(errors))
    expect("字段提取正确",
           meta and meta["name"] == "M1s绘图" and meta["guid"] == GUID
           and meta["version"] == "0.0.0.9" and meta["author"] == "Karlin"
           and meta["territorys"] == [1226], str(meta))
    expect("合法 .cs 无提醒", not warnings, str(warnings))

    # author 回退到文件夹名
    no_author = good_cs.replace(', author: "Karlin"', "")
    meta, errors, _ = validate_script_file(no_author, "publisher/A.cs", "publisher", cfg)
    expect("缺 author 时用文件夹名", not errors and meta["author"] == "publisher", str(errors))

    unknown_author = good_cs.replace('"Karlin"', '"Unknown"')
    meta, errors, warnings = validate_script_file(unknown_author, "publisher/A.cs", "publisher", cfg)
    expect("author=Unknown 时用文件夹名", not errors and meta["author"] == "publisher", str(errors))
    expect("author 回退有提醒", any("文件夹名" in w for w in warnings), str(warnings))

    # 结构性错误
    _m, errors, _w = validate_script_file("class X {}", "a/A.cs", "a", cfg)
    expect("没有特性被拒绝", bool(errors))

    two = good_cs + good_cs
    _m, errors, _w = validate_script_file(two, "a/A.cs", "a", cfg)
    expect("两处特性被拒绝", any("只能有一处" in e for e in errors), str(errors))

    commented = "// " + good_cs.replace("\n", " ") + "\n" + good_cs
    meta, errors, _w = validate_script_file(commented, "a/A.cs", "a", cfg)
    expect("注释里的特性不计数", not errors and meta is not None, str(errors))

    # 词法器边界：字符字面量、字符串里的假特性、逐字字符串都必须被正确跳过（跳过失败会死循环）
    tricky = good_cs + (
        "class Q {\n"
        "    char a = '\\'';\n"
        "    char b = ',';\n"
        "    void F() { Log(\"[ScriptType(guid: \\\"x\\\")]\"); }\n"
        "    string p = @\"C:\\\\path, x\";\n"
        "}\n"
    )
    meta, errors, _w = validate_script_file(tricky, "a/A.cs", "a", cfg)
    expect("字符字面量 / 字符串不干扰解析", not errors and meta is not None, str(errors))

    # 插值字符串：真实的 note / updateInfo 常写成插值原始字符串，不能因为含插值就整段丢掉
    q3 = '"' * 3
    interpolated = (
        '[ScriptType(name: "N", guid: "' + GUID + '", version: "0.0.0.9", '
        'author: "A", territorys: [1226], note: NoteStr, updateInfo: UpdateInfo)]\n'
        "public class S {\n"
        "    const string NoteStr =\n"
        f'    ${q3}\n'
        "    v{Version} 说明\n"
        f"    {q3};\n"
        '    const string Version = "1.2.3";\n'
        '    const string UpdateStr = $"v{Version} 更新";\n'
        "    const string UpdateInfo = UpdateStr;\n"
        "}\n"
    )
    meta, errors, warnings = validate_script_file(interpolated, "a/A.cs", "a", cfg)
    expect("插值 const / const 引用 const 被识别且不报警",
           not errors and not warnings, str(errors) + str(warnings))
    expect("插值里的 {const} 被替换成实际值",
           meta is not None and meta["note"] == "v1.2.3 说明"
           and meta["update_info"] == "v1.2.3 更新", str(meta))

    unresolved = (
        '[ScriptType(name: "N", guid: "' + GUID + '", version: "0.0.0.9", '
        'author: "A", territorys: [1226], note: $"v{Missing} 说明")]\npublic class S { }\n'
    )
    meta, errors, warnings = validate_script_file(unresolved, "a/A.cs", "a", cfg)
    expect("解不出来的插值原样保留，不报错",
           not errors and not warnings and meta["note"] == "v{Missing} 说明",
           str(meta) + str(errors) + str(warnings))

    double_dollar = (
        '[ScriptType(name: "N", guid: "' + GUID + '", version: "0.0.0.9", '
        'author: "A", territorys: [1226], note: Two)]\n'
        "public class S {\n"
        '    const string Version = "1.2.3";\n'
        f"    const string Two = $${q3}\n"
        "    v{{Version}} 价格 {100}\n"
        f"    {q3};\n"
        "}\n"
    )
    meta, errors, warnings = validate_script_file(double_dollar, "a/A.cs", "a", cfg)
    expect("两个 $ 时只有双花括号才是插值",
           not errors and not warnings and meta["note"] == "v1.2.3 价格 {100}",
           str(meta) + str(errors) + str(warnings))

    concat = (
        '[ScriptType(name: "N", guid: "' + GUID + '", version: "0.0.0.9", '
        'author: "A", territorys: [1226], note: "a" + $"b{Version}")]\n'
        'public class S { const string Version = "9"; }\n'
    )
    meta, errors, warnings = validate_script_file(concat, "a/A.cs", "a", cfg)
    expect("属性里内联写 + 拼接字面量也能还原",
           not errors and not warnings and meta["note"] == "ab9",
           str(meta) + str(errors) + str(warnings))

    no_name = good_cs.replace('name: "M1s绘图", ', "")
    meta, errors, warnings = validate_script_file(no_name, "a/A.cs", "a", cfg)
    expect("缺 name 用默认值并提醒",
           not errors and meta["name"] == "Default Script" and any("name" in w for w in warnings))

    # 字段值错误
    for title, bad, needle in (
        ("guid 为空", good_cs.replace(GUID, ""), "guid 不能为空"),
        ("version 非法", good_cs.replace('"0.0.0.9"', '"v1.0"'), "版本号"),
        ("territorys 为负数", good_cs.replace("[1226]", "[-1]"), "uint 范围"),
        ("territorys 超 uint", good_cs.replace("[1226]", f"[{UINT_MAX + 1}]"), "uint 范围"),
        ("territorys 是字符串数组", good_cs.replace("[1226]", '["1226"]'), "uint 数组"),
    ):
        _m, errors, _w = validate_script_file(bad, "a/A.cs", "a", cfg)
        expect(f"{title} 被拒绝", any(needle in e for e in errors), str(errors))

    traversal = good_cs.replace('"M1s绘图"', '"../../pwn"')
    _m, errors, _w = validate_script_file(traversal, "a/A.cs", "a", cfg)
    expect("name 含路径穿越被拒绝", any("文件名" in e for e in errors), str(errors))

    reserved = good_cs.replace('"Karlin"', '"CON"')
    _m, errors, _w = validate_script_file(reserved, "a/A.cs", "a", cfg)
    expect("author 为 Windows 保留名被拒绝", any("保留" in e for e in errors), str(errors))

    bad_guid = good_cs.replace(GUID, "d99c7e91-9b56-432d-a3a8-49a8586915b7e2a")
    _m, errors, warnings = validate_script_file(bad_guid, "a/A.cs", "a", cfg)
    expect("非标准 guid 仅提醒", not errors and any("UUID" in w for w in warnings), str(errors))

    # 生成的 json 自检
    doc = json.dumps([{
        "Name": "M1s绘图", "Guid": GUID, "Version": "0.0.0.9", "Author": "Karlin",
        "Repo": "", "DownloadUrl": "https://raw.githubusercontent.com/a/b/main/c.cs",
        "Note": "", "UpdateInfo": "", "TerritoryIds": [1226],
    }], ensure_ascii=False)
    errors, warnings = validate_json_document(doc, "OnlineRepo.json", cfg)
    expect("生成的 json 通过自检", not errors and not warnings, str(errors))
    errors, _ = validate_json_document("{}", "OnlineRepo.json", cfg)
    expect("生成的 json 顶层非数组被拒绝", bool(errors))

    # 路径规则
    def path_errors(path, author, config=None):
        collected: list[str] = []
        check_path_rules(path, author, config or cfg, collected, "路径")
        return collected

    expect("正确文件夹通过", not path_errors("Karlin-Z/M1s.cs", "Karlin-Z"))
    expect("大小写不同通过", not path_errors("karlin-z/M1s.cs", "Karlin-Z"))
    expect("子目录通过", not path_errors("Karlin-Z/sub/M1s.cs", "Karlin-Z"))
    expect("改别人文件夹被拒绝", bool(path_errors("Other/M1s.cs", "Karlin-Z")))
    expect("根目录文件被拒绝", bool(path_errors("M1s.cs", "Karlin-Z")))
    expect("非 cs 文件被拒绝", bool(path_errors("Karlin-Z/data.json", "Karlin-Z")))
    expect("路径穿越被拒绝", bool(path_errors("Karlin-Z/../Other/a.cs", "Karlin-Z")))
    expect("绝对路径被拒绝", bool(path_errors("/Karlin-Z/a.cs", "Karlin-Z")))

    strict_sub = copy.deepcopy(cfg)
    strict_sub["allow_subfolders"] = False
    expect("禁止子目录时被拒绝", bool(path_errors("Karlin-Z/sub/a.cs", "Karlin-Z", strict_sub)))

    case_sensitive = copy.deepcopy(cfg)
    case_sensitive["username_case_insensitive"] = False
    expect("区分大小写时被拒绝", bool(path_errors("karlin-z/a.cs", "Karlin-Z", case_sensitive)))

    change_files = [
        {"status": "added", "filename": "Karlin-Z/M1s.cs"},
        {"status": "removed", "filename": "Karlin-Z/M2s.cs"},
    ]
    errors, _, rows = check_change_set(change_files, "Karlin-Z", cfg)
    expect("改动集合校验通过", not errors and len(rows) == 2, str(errors))

    rename_out = [{
        "status": "renamed", "filename": "Karlin-Z/a.cs", "previous_filename": "Other/a.cs",
    }]
    errors, _, _ = check_change_set(rename_out, "Karlin-Z", cfg)
    expect("从别人文件夹重命名过来被拒绝", bool(errors))

    errors, _, _ = check_change_set(
        [{"status": "added", "filename": "README.md"}], "Karlin-Z", cfg
    )
    expect("改 README 被拒绝", bool(errors))

    bypass_cfg = copy.deepcopy(cfg)
    bypass_cfg["maintainers"] = ["Karlin-Z"]
    errors, _, _ = check_change_set(
        [{"status": "added", "filename": "README.md"}], "Karlin-Z", bypass_cfg
    )
    expect("维护者白名单可跳过路径限制", not errors, str(errors))

    # guid 查重
    guid_map = {GUID: "alice/A.cs"}
    errs = check_guid_collisions([("bob/B.cs", GUID)], set(), "bob", guid_map, cfg)
    expect("guid 被别人占用被拒绝",
           any("已被 alice/A.cs 使用" in e for e in errs), str(errs))

    errs = check_guid_collisions([("alice/B.cs", GUID)], {"alice/B.cs"}, "alice", guid_map, cfg)
    expect("自己文件夹里 guid 重复被拒绝",
           any("你自己文件夹" in e for e in errs), str(errs))

    errs = check_guid_collisions(
        [("alice/sub/A.cs", GUID)], {"alice/A.cs", "alice/sub/A.cs"}, "alice", guid_map, cfg
    )
    expect("改名 / 移动自己的文件不算冲突", not errs, str(errs))

    errs = check_guid_collisions([("alice/A.cs", GUID), ("alice/B.cs", GUID)], set(), "alice", {}, cfg)
    expect("同一个 PR 内 guid 重复被拒绝", any("本次 PR" in e for e in errs), str(errs))

    errs = check_guid_collisions(
        [("alice/A.cs", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")], set(), "alice", {}, cfg
    )
    expect("拿不到映射表时不误报", not errs, str(errs))

    # ignore_dirs 里的文件夹
    ignore_cfg = copy.deepcopy(cfg)
    ignore_cfg["ignore_dirs"] = ["skipme"]
    errs, _w, _r = check_change_set(
        [{"status": "added", "filename": "skipme/a.cs"}], "skipme", ignore_cfg
    )
    expect("作者文件夹在 ignore_dirs 里被拒绝",
           any("ignore_dirs" in e for e in errs), str(errs))

    # 回归测试：配置拷贝必须是深拷贝
    probe = default_config()
    probe["maintainers"].append("__probe__")
    expect("default_config 返回深拷贝", DEFAULT_CONFIG["maintainers"] == [])

    return failures


# --------------------------------------------------------------------------- #
# merge_repos：扫描范围、DownloadUrl、去重、ignore_dirs、产物
# --------------------------------------------------------------------------- #


def _cs(guid, name="测试脚本", author=None, version="0.0.1", territorys="[1226]"):
    """自测用：拼一个最小可解析的 .cs 内容。"""
    parts = [f'name: "{name}"', f'guid: "{guid}"', f'version: "{version}"']
    if author is not None:
        parts.append(f'author: "{author}"')
    if territorys is not None:
        parts.append(f"territorys: {territorys}")
    return "[ScriptType(" + ", ".join(parts) + ")]\npublic class S { }\n"


def run_merge_repos_checks() -> list[str]:
    """跑 merge_repos 的断言，返回失败项名称。"""
    collect = merge_repos.collect
    merge_records = merge_repos.merge_records
    build = merge_repos.build
    CANONICAL_FIELD_ORDER = merge_repos.CANONICAL_FIELD_ORDER

    cfg = pr_review.default_config()
    failures: list[str] = []

    def expect(name, condition, detail=""):
        if condition:
            print(f"✅ {name}")
        else:
            failures.append(name)
            print(f"❌ {name} {detail}")

    def quiet_build(*args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return build(*args, **kwargs)

    GUID_A1 = "8010d865-7d6d-4c23-92e0-f4b0120e18ac"
    GUID_A2 = "d99c7e91-9b56-432d-a3a8-49a8586915b7e2a"
    GUID_A3 = "e7f7c69b-cc82-4b74-b1ea-2f3f0eecb2e2"
    GUID_A4 = "37ea4922-dee4-b998-f23f-e2a1cd1b1bcd"
    GUID_B2 = "a4e14eff-0aea-a4b6-d8c3-47644a3e9e9a"

    workdir = tempfile.mkdtemp(prefix="kasl_merge_")
    try:
        for rel in ("alice/sub", "alice/_draft", "alice/skipme", "bob",
                    ".github/scripts", "_site"):
            os.makedirs(os.path.join(workdir, rel))

        def write(rel, content):
            with open(os.path.join(workdir, rel), "w", encoding="utf-8") as handle:
                handle.write(content)

        write("alice/A1.cs", _cs(GUID_A1, "A1", "Alice"))
        write("alice/sub/A2.cs", _cs(GUID_A2, "A2", None))            # 作者回退到文件夹名
        write("alice/_draft/A3.cs", _cs(GUID_A3, "A3", "Alice"))      # 子目录里 _ 开头也要收录
        write("alice/skipme/A4.cs", _cs(GUID_A4, "A4", "Alice"))      # 供 ignore_dirs 测试
        write("bob/B1.cs", _cs(GUID_A1, "B1", "Bob"))                 # 与 alice 冲突
        write("bob/B2.cs", _cs(GUID_B2, "B2", "Bob", territorys="[1226, 1228]"))
        write("alice/Bad.cs", "public class NoAttribute { }\n")       # 无特性 -> 跳过
        write(".github/scripts/x.cs", _cs(GUID_A1, "ignored"))
        write("_site/y.cs", _cs(GUID_A1, "ignored"))
        write("OnlineRepo.json", "[]")                                # 根目录产物不参与扫描

        records, skipped, _warnings, file_counts = collect(workdir, cfg, "owner/repo", "main")
        rels = [rel for _o, rel, _e in records]
        expect("忽略 . 与 _ 开头的第一层目录",
               all("_site" not in r and ".github" not in r for r in rels))
        expect("递归收集子目录", "alice/sub/A2.cs" in rels)
        expect("子目录里 _ 开头的目录也收录", "alice/_draft/A3.cs" in rels, str(rels))
        expect("根目录 json 不参与扫描", all(r != "OnlineRepo.json" for r in rels))
        expect("收集到 6 个有效文件", len(records) == 6, str(len(records)))
        expect("无特性的文件被跳过", any(r == "alice/Bad.cs" for r, _e in skipped), str(skipped))
        expect("按贡献者统计文件数", file_counts == {"alice": 5, "bob": 2}, str(file_counts))

        by_name = {e["Name"]: e for _o, _r, e in records}
        expect("author 缺失时用文件夹名", by_name["A2"]["Author"] == "alice",
               by_name["A2"]["Author"])
        expect("DownloadUrl 自动生成",
               by_name["A2"]["DownloadUrl"] ==
               "https://raw.githubusercontent.com/owner/repo/main/alice/sub/A2.cs",
               by_name["A2"]["DownloadUrl"])
        expect("深层子目录 DownloadUrl 正确",
               by_name["A3"]["DownloadUrl"].endswith("/alice/_draft/A3.cs"),
               by_name["A3"]["DownloadUrl"])
        keys_a1 = list(by_name["A1"].keys())
        expect("字段顺序规范且省略空值",
               keys_a1 == [k for k in CANONICAL_FIELD_ORDER if k in keys_a1]
               and all(by_name["A1"][k] not in ("", [], None) for k in keys_a1), str(keys_a1))
        expect("Repo / Note / UpdateInfo 为空时被省略",
               all(k not in by_name["A1"] for k in ("Repo", "Note", "UpdateInfo")),
               str(by_name["A1"]))
        expect("TerritoryIds 正确", by_name["B2"]["TerritoryIds"] == [1226, 1228],
               str(by_name["B2"]["TerritoryIds"]))

        merged, conflicts, owner_counts, guid_map = merge_records(records)
        expect("Guid 去重（忽略大小写）", len(merged) == 5, str(len(merged)))
        expect("冲突被记录且先出现的胜出",
               len(conflicts) == 1 and any(e["Name"] == "A1" for e in merged)
               and all(e["Name"] != "B1" for e in merged), str(conflicts))
        expect("按贡献者统计收录数", owner_counts == {"alice": 4, "bob": 1}, str(owner_counts))
        expect("guid_map 指向生效文件",
               guid_map.get(GUID_A1.lower()) == "alice/A1.cs", str(guid_map))

        # ignore_dirs：任意深度匹配
        skip_cfg = copy.deepcopy(cfg)
        skip_cfg["ignore_dirs"] = ["skipme"]
        records2, _s, _w, _counts = collect(workdir, skip_cfg, "owner/repo", "main")
        rels2 = [r for _o, r, _e in records2]
        expect("ignore_dirs 排除深层目录", "alice/skipme/A4.cs" not in rels2, str(rels2))
        expect("ignore_dirs 生效后数量正确", len(records2) == 5, str(len(records2)))

        out_dir = os.path.join(workdir, "_out")
        root_out = os.path.join(workdir, "_generated", "OnlineRepo.json")
        map_out = os.path.join(workdir, "_generated", "guid-map.json")
        code = quiet_build(workdir, cfg, "owner/repo", "main",
                           out_dir=out_dir, root_json_path=root_out, guid_map_path=map_out)
        expect("build 正常退出", code == 0, str(code))
        with open(os.path.join(out_dir, "index.json"), encoding="utf-8") as handle:
            written = json.load(handle)
        expect("站点 index.json 条目正确", len(written) == 5, str(len(written)))
        expect("index.html 已生成", os.path.isfile(os.path.join(out_dir, "index.html")))
        with open(root_out, encoding="utf-8") as handle:
            root_written = json.load(handle)
        expect("根目录 json 内容一致", root_written == written)
        with open(map_out, encoding="utf-8") as handle:
            written_map = json.load(handle)
        expect("guid-map 文件已写出且内容正确",
               written_map.get(GUID_B2.lower()) == "bob/B2.cs", str(written_map))

        # 稳定可复现：再生成一次内容应完全相同（决定「无变化就不提交」是否成立）
        again = os.path.join(workdir, "_out2")
        quiet_build(workdir, cfg, "owner/repo", "main", out_dir=again)
        with open(os.path.join(again, "index.json"), encoding="utf-8") as handle:
            expect("重复生成结果一致", handle.read() ==
                   open(os.path.join(out_dir, "index.json"), encoding="utf-8").read())

        only_root = os.path.join(workdir, "_root_only.json")
        quiet_build(workdir, cfg, "owner/repo", "main", root_json_path=only_root)
        expect("--no-site 只写根目录 json", os.path.isfile(only_root))

        # 中文与空格路径需要 URL 编码
        write("alice/极佐拉加 绘图.cs", _cs(GUID_A2, "中文名", "Alice"))
        records3, _s, _w, _f = collect(workdir, cfg, "owner/repo", "main")
        cn = [e for _o, r, e in records3 if "绘图" in r]
        expect("中文路径被百分号编码",
               cn and "%" in cn[0]["DownloadUrl"] and " " not in cn[0]["DownloadUrl"],
               cn[0]["DownloadUrl"] if cn else "not found")

        expect("strict 模式在跳过时报错",
               quiet_build(workdir, cfg, "owner/repo", "main",
                           out_dir=os.path.join(workdir, "_out3"), strict=True) == 1)

        # 第一层目录被 ignore_dirs 排除（例如放模板的 examples/）
        first = tempfile.mkdtemp(prefix="kasl_ignore_")
        try:
            os.makedirs(os.path.join(first, "examples"))
            os.makedirs(os.path.join(first, "alice"))
            with open(os.path.join(first, "examples", "t.cs"), "w", encoding="utf-8") as handle:
                handle.write(_cs(GUID_A1, "template"))
            with open(os.path.join(first, "alice", "a.cs"), "w", encoding="utf-8") as handle:
                handle.write(_cs(GUID_A2, "real"))
            example_cfg = copy.deepcopy(cfg)
            example_cfg["ignore_dirs"] = ["examples"]
            got, _s, _w, _f = collect(first, example_cfg, "owner/repo", "main")
            expect("ignore_dirs 排除第一层目录",
                   [r for _o, r, _e in got] == ["alice/a.cs"], str(got))
        finally:
            shutil.rmtree(first, ignore_errors=True)

        empty_dir = tempfile.mkdtemp(prefix="kasl_empty_")
        try:
            expect("空仓库默认允许生成并告警",
                   quiet_build(empty_dir, cfg, "owner/repo", "main",
                               out_dir=os.path.join(empty_dir, "out")) == 0)
            expect("--forbid-empty 时拒绝生成",
                   quiet_build(empty_dir, cfg, "owner/repo", "main",
                               out_dir=os.path.join(empty_dir, "out2"),
                               forbid_empty=True) == 1)
        finally:
            shutil.rmtree(empty_dir, ignore_errors=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    return failures


def main() -> int:
    """依次跑两套检查，返回进程退出码。"""
    failures: list[str] = []
    for title, runner in (
        ("pr_review", run_pr_review_checks),
        ("merge_repos", run_merge_repos_checks),
    ):
        print(f"=== {title} ===")
        failed = runner()
        failures.extend(failed)
        print(f"--- {title}：{'全部通过' if not failed else f'失败 {len(failed)} 项'}")
        print()

    if failures:
        print(f"❌ 自测失败 {len(failures)} 项：{', '.join(failures)}")
        return 1
    print("✅ 全部自测通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
