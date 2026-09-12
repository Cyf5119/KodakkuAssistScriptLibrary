#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫描所有贡献者文件夹里的 .cs，提取 [ScriptType(...)]，生成合并后的索引。

产物（可分别开关）：
  --out DIR          站点目录：DIR/index.json（合并索引）+ DIR/index.html（说明页）
  --root-json PATH   仓库根目录的总索引，例如 OnlineRepo.json

合并规则：
  - 只扫描仓库第一层里不以 . 或 _ 开头的目录（即贡献者文件夹），递归收集 .cs
  - 每个 .cs 用 pr_review.validate_script_file 校验（与 PR 审核同一套规则）
  - DownloadUrl 自动填成本仓库该 .cs 的 raw 直链
  - 按「文件夹名 -> 文件路径」排序处理，保证输出稳定、可复现
  - 按 Guid（忽略大小写）去重：先出现的生效，冲突列入报告

只使用标准库。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT_DEFAULT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import pr_review  # noqa: E402  （同目录，复用校验规则）

#: 输出时统一字段顺序，让合并结果整齐、diff 友好
CANONICAL_FIELD_ORDER = [
    "Name",
    "Guid",
    "Version",
    "Author",
    "Repo",
    "DownloadUrl",
    "Note",
    "UpdateInfo",
    "TerritoryIds",
]

#: 任何层级都不参与合并的目录名
EXCLUDED_DIRS = {".git", "node_modules"}

#: 已提交的 guid -> 源文件 映射，供 PR 审核阶段查重
DEFAULT_GUID_MAP = ".github/index/guid-map.json"

INDEX_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KodakkuAssistScriptLibrary 索引</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         max-width: 860px; margin: 40px auto; padding: 0 16px; line-height: 1.6; color: #24292f; }}
  code {{ background: #f3f4f6; padding: 2px 6px; border-radius: 4px; }}
  table {{ border-collapse: collapse; width: 100%; margin: 16px 0; }}
  th, td {{ border: 1px solid #d0d7de; padding: 6px 10px; text-align: left; }}
  th {{ background: #f6f8fa; }}
  .warn {{ color: #9a6700; }}
  .muted {{ color: #57606a; font-size: 0.9em; }}
</style>
</head>
<body>
<h1>KodakkuAssistScriptLibrary 索引</h1>
<p>把下面任一地址填进 KodakkuAssist 的 <code>OnlineRepo</code>，即可订阅本库全部脚本：</p>
<p><code>index.json</code> —— <a href="index.json">当前站点的 index.json</a></p>
<p>共 <strong>{entry_count}</strong> 个脚本，来自 <strong>{owner_count}</strong> 位贡献者。</p>
{table}
{problems}
<p class="muted">最近生成：{generated_at}（UTC）</p>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# 收集与合并
# --------------------------------------------------------------------------- #


def discover_files(repo_root, ignore_dirs=None):
    """返回 [(owner, rel_path)]，按 owner/路径 排序。

    第一层目录必须像 GitHub 用户名（跳过 . / _ 开头，以及在 ignore_dirs 里的）；
    进入贡献者文件夹后**递归全部子目录**，不再按名字过滤——只要在用户目录下，
    任意深度的 .cs 都会被收录。
    """
    ignored = {str(item) for item in (ignore_dirs or [])} | EXCLUDED_DIRS
    found = []
    for name in sorted(os.listdir(repo_root)):
        full = os.path.join(repo_root, name)
        if not os.path.isdir(full):
            continue
        if name.startswith((".", "_")) or name in ignored:
            continue
        for dirpath, dirnames, filenames in os.walk(full):
            dirnames[:] = sorted(d for d in dirnames if d not in ignored)
            for filename in sorted(filenames):
                if filename.lower().endswith(".cs"):
                    rel = os.path.relpath(os.path.join(dirpath, filename), repo_root)
                    found.append((name, rel.replace(os.sep, "/")))
    return found


def download_url(repo, branch, rel_path):
    return (
        f"https://raw.githubusercontent.com/{repo}/"
        f"{urllib.parse.quote(branch, safe='')}/{urllib.parse.quote(rel_path, safe='/')}"
    )


def build_entry(meta, repo, branch, rel_path, cfg):
    entry = {
        "Name": meta["name"],
        "Guid": meta["guid"],
        "Version": meta["version"],
        "Author": meta["author"],
        # Repo 会被插件用当前订阅地址覆盖，留空即可
        "Repo": "",
        "DownloadUrl": download_url(repo, branch, rel_path),
        "Note": meta["note"],
        "UpdateInfo": meta["update_info"],
        "TerritoryIds": meta["territorys"],
    }
    return {key: entry[key] for key in CANONICAL_FIELD_ORDER if key in entry}


def collect(repo_root, cfg, repo, branch):
    """返回 (records, skipped, warnings, file_counts)。

    records: [(owner, rel_path, entry)]；skipped: [(rel_path, [错误])]
    """
    records = []
    skipped = []
    warnings = []
    file_counts: dict[str, int] = {}

    for owner, rel in discover_files(repo_root, cfg.get("ignore_dirs")):
        file_counts[owner] = file_counts.get(owner, 0) + 1
        try:
            with open(os.path.join(repo_root, rel), "rb") as handle:
                raw = handle.read()
        except OSError as exc:
            skipped.append((rel, [f"无法读取：{exc}"]))
            continue

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            skipped.append((rel, [f"不是合法的 UTF-8 编码：{exc}"]))
            continue

        meta, errors, file_warnings = pr_review.validate_script_file(text, rel, owner, cfg)
        warnings.extend(file_warnings)
        if errors:
            skipped.append((rel, errors))
            continue

        records.append((owner, rel, build_entry(meta, repo, branch, rel, cfg)))

    return records, skipped, warnings, file_counts


def merge_records(records):
    """按 Guid（忽略大小写）去重，先出现的生效。

    返回 (merged, conflicts, owner_counts, guid_map)；
    guid_map 是 {小写 guid: 生效的源文件路径}，会提交到仓库供 PR 审核查重。
    """
    merged = []
    conflicts = []
    owner_counts = {}
    guid_map = {}
    seen = {}

    for owner, rel, entry in records:
        guid = str(entry.get("Guid", "")).strip()
        key = guid.lower()
        if key in seen:
            conflicts.append((guid, seen[key], rel))
            continue
        seen[key] = rel
        guid_map[key] = rel
        owner_counts[owner] = owner_counts.get(owner, 0) + 1
        merged.append(entry)

    return merged, conflicts, owner_counts, guid_map


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #


def render_table(owner_counts, file_counts):
    rows = ["<table>", "<tr><th>贡献者</th><th>脚本文件</th><th>收录脚本</th></tr>"]
    for owner in sorted(owner_counts):
        rows.append(
            f"<tr><td>{owner}</td><td>{file_counts.get(owner, 0)}</td>"
            f"<td>{owner_counts[owner]}</td></tr>"
        )
    rows.append("</table>")
    return "\n".join(rows)


def render_problems(conflicts, skipped):
    if not conflicts and not skipped:
        return ""
    parts = ['<h2 class="warn">需要注意</h2>', "<ul>"]
    for guid, winner, loser in conflicts:
        parts.append(
            f'<li class="warn">Guid <code>{guid}</code> 重复：<code>{loser}</code> '
            f"与 <code>{winner}</code> 冲突，已保留前者。</li>"
        )
    for rel, errors in skipped:
        parts.append(f'<li class="warn"><code>{rel}</code> 未通过校验，已跳过：{errors[0]}</li>')
    parts.append("</ul>")
    return "\n".join(parts)


def render_html(merged, owner_counts, file_counts, conflicts, skipped):
    return INDEX_HTML_TEMPLATE.format(
        entry_count=len(merged),
        owner_count=len(owner_counts),
        table=render_table(owner_counts, file_counts),
        problems=render_problems(conflicts, skipped),
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
    )


def write_text(path, text):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def write_outputs(out_dir, root_json_path, guid_map_path, guid_map, merged,
                  owner_counts, file_counts, conflicts, skipped, root_json_mode):
    payload = json.dumps(merged, ensure_ascii=False, indent=2)

    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        write_text(os.path.join(out_dir, "index.json"), payload + "\n")
        if root_json_mode:
            # --root-json-mode：让站点根路径直接返回 JSON（插件不检查 Content-Type）
            content = payload
        else:
            content = render_html(merged, owner_counts, file_counts, conflicts, skipped)
        write_text(os.path.join(out_dir, "index.html"), content + "\n")

    if root_json_path:
        write_text(root_json_path, payload + "\n")

    if guid_map_path:
        # guid -> 源文件，供 PR 审核阶段查重（不进公开索引）
        write_text(
            guid_map_path,
            json.dumps(guid_map, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )


def emit_report(merged, file_counts, owner_counts, conflicts, skipped, warnings,
                out_dir, root_json_path, guid_map_path):
    lines = ["## 📦 索引生成结果", ""]
    lines.append(f"- 收录脚本：**{len(merged)}**")
    lines.append(f"- 贡献者：**{len(file_counts)}**")
    lines.append(f"- 扫描文件：**{sum(file_counts.values())}** 个 .cs")
    lines.append("")

    if conflicts:
        lines.append(f"### ⚠️ Guid 冲突（{len(conflicts)}）")
        lines.append("")
        for guid, winner, loser in conflicts:
            lines.append(f"- `{guid}`：`{loser}` 与 `{winner}` 冲突，已保留 `{winner}`")
            print(f"::warning::Guid 冲突：{loser} 与 {winner} 重复，已保留 {winner}")
        lines.append("")

    if skipped:
        lines.append(f"### ❌ 未通过校验被跳过（{len(skipped)}）")
        lines.append("")
        for rel, errors in skipped:
            lines.append(f"- `{rel}`：{errors[0]}（共 {len(errors)} 个问题）")
            print(f"::warning::跳过不合规文件 {rel}：{errors[0]}")
        lines.append("")

    if warnings:
        lines.append(f"### 提醒（{len(warnings)}）")
        lines.append("")
        for item in warnings:
            lines.append(f"- {item}")
        lines.append("")

    if not merged:
        lines.append("> ⚠️ 当前没有任何脚本被收录，生成的是空索引。")
        lines.append("")
        print("::warning::没有收录到任何脚本，生成的是空索引")
    elif not conflicts and not skipped and not warnings:
        lines.append("没有发现问题。")
        lines.append("")

    outputs = []
    if out_dir:
        outputs.append(f"`{out_dir}`（index.json + index.html）")
    if root_json_path:
        outputs.append(f"`{root_json_path}`")
    if guid_map_path:
        outputs.append(f"`{guid_map_path}`")
    lines.append("输出：" + ("、".join(outputs) if outputs else "没有指定输出"))

    report = "\n".join(lines)
    print(report)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as handle:
                handle.write(report + "\n")
        except OSError:
            pass
    return report


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def resolve_repo(explicit):
    if explicit:
        return explicit
    env = os.environ.get("GITHUB_REPOSITORY")
    if env:
        return env
    try:
        url = subprocess.run(
            ["git", "-C", REPO_ROOT_DEFAULT, "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        url = ""
    match = re.search(r"github\.com[:/]([^/]+)/(.+?)(?:\.git)?$", url)
    return f"{match.group(1)}/{match.group(2)}" if match else ""


def resolve_branch(explicit):
    if explicit:
        return explicit
    env = os.environ.get("GITHUB_REF_NAME")
    if env:
        return env
    try:
        name = subprocess.run(
            ["git", "-C", REPO_ROOT_DEFAULT, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        name = ""
    return name or "main"


def build(repo_root, cfg, repo, branch, out_dir=None, root_json_path=None,
          guid_map_path=None, root_json_mode=False, forbid_empty=False, strict=False):
    records, skipped, warnings, file_counts = collect(repo_root, cfg, repo, branch)
    merged, conflicts, owner_counts, guid_map = merge_records(records)

    if not merged and forbid_empty:
        print("::error::没有收录到任何脚本，拒绝生成空索引（--forbid-empty）")
        return 1

    # 自检：合并结果本身必须仍然是合规的 OnlineRepo 文档
    check_errors, _ = pr_review.validate_json_document(
        json.dumps(merged, ensure_ascii=False), "OnlineRepo.json", cfg
    )
    if check_errors:
        print("::error::合并结果自身不合规，说明合并逻辑有 bug：")
        for item in check_errors:
            print(f"  - {item}")
        return 2

    write_outputs(out_dir, root_json_path, guid_map_path, guid_map, merged,
                  owner_counts, file_counts, conflicts, skipped, root_json_mode)
    emit_report(merged, file_counts, owner_counts, conflicts, skipped, warnings,
                out_dir, root_json_path, guid_map_path)

    if strict and (conflicts or skipped):
        return 1
    return 0


# --------------------------------------------------------------------------- #
# 自测
# --------------------------------------------------------------------------- #


def _cs(guid, name="测试脚本", author=None, version="0.0.1", territorys="[1226]"):
    parts = [f'name: "{name}"', f'guid: "{guid}"', f'version: "{version}"']
    if author is not None:
        parts.append(f'author: "{author}"')
    if territorys is not None:
        parts.append(f"territorys: {territorys}")
    return "[ScriptType(" + ", ".join(parts) + ")]\npublic class S { }\n"


def cmd_selftest() -> int:
    import contextlib
    import copy
    import io
    import shutil
    import tempfile

    cfg = pr_review.default_config()
    failures = []

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
        expect("字段齐全且顺序规范",
               list(by_name["A1"].keys()) == CANONICAL_FIELD_ORDER, str(list(by_name["A1"].keys())))
        expect("Repo 留空", by_name["A1"]["Repo"] == "")
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
        records2, _s, _w, counts2 = collect(workdir, skip_cfg, "owner/repo", "main")
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

    print()
    if failures:
        print(f"❌ 自测失败 {len(failures)} 项：{', '.join(failures)}")
        return 1
    print("✅ 全部自测通过")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="合并所有贡献者 .cs 并生成索引")
    parser.add_argument("--repo-root", default=REPO_ROOT_DEFAULT, help="仓库根目录")
    parser.add_argument("--out", default="_site", help="站点输出目录；配合 --no-site 可跳过")
    parser.add_argument("--no-site", action="store_true", help="不生成站点目录")
    parser.add_argument("--root-json", default="", help="同时写一份总索引到该路径（仓库根目录）")
    parser.add_argument("--guid-map", default="",
                        help=f"guid -> 源文件 映射的输出路径（建议 {DEFAULT_GUID_MAP}）")
    parser.add_argument("--root-json-mode", action="store_true",
                        help="让站点根路径 index.html 直接返回 JSON")
    parser.add_argument("--repo", default="", help="owner/repo，用于生成 DownloadUrl")
    parser.add_argument("--branch", default="", help="分支名，用于生成 DownloadUrl")
    parser.add_argument("--forbid-empty", action="store_true", help="没有收录到脚本时报错")
    parser.add_argument("--strict", action="store_true", help="存在跳过/冲突时以非 0 退出")
    parser.add_argument("--selftest", action="store_true", help="运行内置自测")
    args = parser.parse_args()

    if args.selftest:
        return cmd_selftest()

    repo_root = os.path.abspath(args.repo_root)
    cfg = pr_review.load_config()
    repo = resolve_repo(args.repo)
    branch = resolve_branch(args.branch)

    if not repo:
        print("::error::无法确定仓库地址（owner/repo），请用 --repo 指定或用 GITHUB_REPOSITORY")
        return 2

    out_dir = "" if args.no_site else (
        args.out if os.path.isabs(args.out) else os.path.join(repo_root, args.out)
    )
    root_json_path = ""
    if args.root_json:
        root_json_path = args.root_json if os.path.isabs(args.root_json) else os.path.join(
            repo_root, args.root_json
        )

    guid_map_path = ""
    if args.guid_map:
        guid_map_path = args.guid_map if os.path.isabs(args.guid_map) else os.path.join(
            repo_root, args.guid_map
        )

    if not out_dir and not root_json_path and not guid_map_path:
        print("::error::--no-site 且未指定 --root-json / --guid-map 时没有任何输出")
        return 2

    return build(
        repo_root, cfg, repo, branch,
        out_dir=out_dir, root_json_path=root_json_path, guid_map_path=guid_map_path,
        root_json_mode=args.root_json_mode,
        forbid_empty=args.forbid_empty, strict=args.strict,
    )


if __name__ == "__main__":
    sys.exit(main())
