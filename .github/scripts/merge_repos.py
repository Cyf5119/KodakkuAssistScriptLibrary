#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把仓库里每个 <GitHub 用户名>/ 下的 .json 合并成一份索引，供 GitHub Pages 发布。

产物（默认输出到 _site/）：
  index.json   合并后的数组，直接填进 KodakkuAssist 的 OnlineRepo 即可
  index.html   说明页（加 --root-json 可改为让根路径直接返回 JSON）

合并规则：
  - 只扫描仓库第一层里不以 . 或 _ 开头的目录（即贡献者文件夹）
  - 每个 .json 先按 pr_review 的同一套规则校验，不合规的文件跳过并列入报告
  - 按「文件夹名 -> 文件路径」排序处理，保证输出稳定、可比对
  - 按 Guid（忽略大小写）去重：先出现的生效，冲突列入报告

只使用标准库；校验规则直接复用 pr_review，避免两处规则漂移。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT_DEFAULT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
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

#: 这些第一层目录不参与合并
EXCLUDED_DIRS = {".github", ".git", "_site", "node_modules"}

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
<p>把下面这个地址填进 KodakkuAssist 的 <code>OnlineRepo</code>，即可订阅本库全部脚本：</p>
<p><code>index.json</code> —— <a href="index.json">当前页面的 index.json</a></p>
<p>共 <strong>{entry_count}</strong> 个脚本，来自 <strong>{owner_count}</strong> 位贡献者。</p>
{table}
{problems}
<p class="muted">最近生成：{generated_at}（UTC）</p>
</body>
</html>
"""


def discover_files(repo_root):
    """返回 [(owner, rel_path)]，按 owner/路径 排序。只认第一层的贡献者文件夹。"""
    found = []
    for name in sorted(os.listdir(repo_root)):
        full = os.path.join(repo_root, name)
        if not os.path.isdir(full):
            continue
        if name.startswith((".", "_")) or name in EXCLUDED_DIRS:
            continue
        for dirpath, dirnames, filenames in os.walk(full):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith((".", "_")))
            for filename in sorted(filenames):
                if filename.lower().endswith(".json"):
                    rel = os.path.relpath(os.path.join(dirpath, filename), repo_root)
                    found.append((name, rel.replace(os.sep, "/")))
    return found


def order_fields(entry):
    ordered = {key: entry[key] for key in CANONICAL_FIELD_ORDER if key in entry}
    for key, value in entry.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


def collect(repo_root, cfg):
    """扫描并校验所有贡献者文件，返回 (records, skipped, warnings)。

    records: [(owner, rel_path, entry)]
    skipped: [(rel_path, [错误])]
    warnings: [提醒]
    """
    records = []
    skipped = []
    warnings = []

    for owner, rel in discover_files(repo_root):
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

        errors, file_warnings = pr_review.validate_json_document(text, rel, cfg)
        warnings.extend(file_warnings)
        if errors:
            skipped.append((rel, errors))
            continue

        for entry in json.loads(text):
            records.append((owner, rel, entry))

    return records, skipped, warnings


def merge_records(records):
    """按 Guid（忽略大小写）去重，先出现的生效。返回 (merged, conflicts, owner_counts)。"""
    merged = []
    conflicts = []
    owner_counts = {}
    seen = {}

    for owner, rel, entry in records:
        guid = str(entry.get("Guid", "")).strip()
        key = guid.lower()
        if key in seen:
            conflicts.append((guid, seen[key], rel))
            continue
        seen[key] = rel
        owner_counts[owner] = owner_counts.get(owner, 0) + 1
        merged.append(order_fields(entry))

    return merged, conflicts, owner_counts


def render_table(owner_counts, file_counts):
    rows = ["<table>", "<tr><th>贡献者</th><th>文件数</th><th>脚本数</th></tr>"]
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


def write_site(out_dir, merged, owner_counts, file_counts, conflicts, skipped, root_json):
    os.makedirs(out_dir, exist_ok=True)
    payload = json.dumps(merged, ensure_ascii=False, indent=2)

    with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload + "\n")

    # --root-json：让根路径直接返回 JSON。插件用 GetStringAsync 读取，不关心 Content-Type。
    content = payload if root_json else render_html(
        merged, owner_counts, file_counts, conflicts, skipped
    )
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content + "\n")


def emit_report(merged, file_counts, records, conflicts, skipped, warnings, out_dir):
    lines = ["## 📦 Pages 索引生成结果", ""]
    lines.append(f"- 合并后脚本数：**{len(merged)}**")
    lines.append(f"- 参与贡献者：**{len(file_counts)}**")
    lines.append(f"- 源文件数：**{len(records)}** 条记录 / **{sum(file_counts.values())}** 个文件")
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

    if not conflicts and not skipped and not warnings:
        lines.append("没有发现问题。")
        lines.append("")

    lines.append(f"输出目录：`{out_dir}`（`index.json` + `index.html`）")
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


def build(repo_root, out_dir, cfg, root_json=False, allow_empty=False, strict=False):
    """执行完整流程，返回退出码。"""
    records, skipped, warnings = collect(repo_root, cfg)
    merged, conflicts, owner_counts = merge_records(records)

    file_counts = {}
    for owner, _rel, _entry in records:
        file_counts[owner] = file_counts.get(owner, 0) + 1

    if not merged and not allow_empty:
        print("::error::没有合并到任何脚本条目，拒绝生成空索引（可用 --allow-empty 覆盖）")
        return 1

    # 自检：合并结果本身必须仍然是合规的 OnlineRepo 文档
    check_errors, _ = pr_review.validate_json_document(
        json.dumps(merged, ensure_ascii=False), "index.json", cfg
    )
    if check_errors:
        print("::error::合并结果自身不合规，说明合并逻辑有 bug：")
        for item in check_errors:
            print(f"  - {item}")
        return 2

    write_site(out_dir, merged, owner_counts, file_counts, conflicts, skipped, root_json)
    emit_report(merged, file_counts, records, conflicts, skipped, warnings, out_dir)

    if strict and (conflicts or skipped):
        return 1
    return 0


# --------------------------------------------------------------------------- #
# 自测
# --------------------------------------------------------------------------- #


def cmd_selftest() -> int:
    import contextlib
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
        """自测时吞掉 build() 的报告输出，保持自测结果清爽。"""
        with contextlib.redirect_stdout(io.StringIO()):
            return build(*args, **kwargs)

    def entry(name, guid, version="0.0.1", **over):
        base = {
            "Name": name,
            "Guid": guid,
            "Version": version,
            "Author": "Tester",
            "Repo": "",
            "DownloadUrl": "https://example.com/a.cs",
            "Note": "",
            "UpdateInfo": "",
            "TerritoryIds": [1],
        }
        base.update(over)
        return base

    workdir = tempfile.mkdtemp(prefix="kasl_merge_")
    try:
        # alice：两个文件；bob：一个文件；其中 bob 与 alice 有一个 Guid 冲突
        os.makedirs(os.path.join(workdir, "alice", "sub"))
        os.makedirs(os.path.join(workdir, "bob"))
        os.makedirs(os.path.join(workdir, ".github", "scripts"))
        os.makedirs(os.path.join(workdir, "_site"))

        def write(rel, data):
            with open(os.path.join(workdir, rel), "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)

        write("alice/OnlineRepo.json", [entry("A1", "guid-a1")])
        write("alice/sub/Extra.json", [entry("A2", "guid-a2")])
        write("bob/OnlineRepo.json", [entry("B1", "GUID-A1"), entry("B2", "guid-b2")])
        write(".github/pr_review_rules.json", {"unknown_fields": "warn"})
        write("_site/index.json", [entry("SHOULD-NOT-APPEAR", "guid-x")])
        with open(os.path.join(workdir, "broken.json"), "w", encoding="utf-8") as handle:
            handle.write("[{]")

        records, skipped, _warnings = collect(workdir, cfg)
        expect("忽略 . 与 _ 开头的目录", all("_site" not in rel for _o, rel, _e in records))
        expect("递归收集子目录", any(rel == "alice/sub/Extra.json" for _o, rel, _e in records))
        expect("收集到 4 条记录", len(records) == 4, str(len(records)))
        expect("根目录文件不被收集", all(not rel.endswith("broken.json") for _o, rel, _e in records))

        merged, conflicts, owner_counts = merge_records(records)
        expect("Guid 去重（忽略大小写）", len(merged) == 3, str(len(merged)))
        expect("冲突被记录", len(conflicts) == 1 and conflicts[0][0].lower() == "guid-a1", str(conflicts))
        expect("先出现的 alice 条目胜出",
               any(e["Name"] == "A1" for e in merged) and all(e["Name"] != "B1" for e in merged))
        expect("按贡献者统计", owner_counts == {"alice": 2, "bob": 1}, str(owner_counts))
        expect("字段顺序被规范化", list(merged[0].keys())[:2] == ["Name", "Guid"], str(list(merged[0].keys())))

        out_dir = os.path.join(workdir, "_out")
        code = quiet_build(workdir, out_dir, cfg)
        expect("build 正常退出", code == 0, str(code))
        with open(os.path.join(out_dir, "index.json"), encoding="utf-8") as handle:
            written = json.load(handle)
        expect("写出的 index.json 条目正确", len(written) == 3, str(len(written)))
        expect("index.html 已生成", os.path.isfile(os.path.join(out_dir, "index.html")))

        out_json = os.path.join(workdir, "_out_json")
        quiet_build(workdir, out_json, cfg, root_json=True)
        with open(os.path.join(out_json, "index.html"), encoding="utf-8") as handle:
            expect("--root-json 时 index.html 就是 JSON", json.load(handle) is not None)

        # 不合规文件跳过 + strict 模式
        write("alice/Bad.json", [{"Name": "no guid"}])
        _r, skipped2, _w = collect(workdir, cfg)
        expect("不合规文件被跳过", any(rel == "alice/Bad.json" for rel, _e in skipped2), str(skipped2))
        expect("strict 模式返回 1",
               quiet_build(workdir, os.path.join(workdir, "_out2"), cfg, strict=True) == 1)

        empty_dir = tempfile.mkdtemp(prefix="kasl_empty_")
        try:
            expect("没有条目时拒绝生成",
                   quiet_build(empty_dir, os.path.join(empty_dir, "out"), cfg) == 1)
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
    parser = argparse.ArgumentParser(description="合并所有贡献者 JSON 并生成 Pages 站点")
    parser.add_argument("--repo-root", default=REPO_ROOT_DEFAULT, help="仓库根目录")
    parser.add_argument("--out", default="", help="输出目录（默认 <仓库根>/_site）")
    parser.add_argument("--root-json", action="store_true", help="让根路径 index.html 直接返回 JSON")
    parser.add_argument("--allow-empty", action="store_true", help="允许生成空索引")
    parser.add_argument("--strict", action="store_true", help="存在跳过/冲突时以非 0 退出")
    parser.add_argument("--selftest", action="store_true", help="运行内置自测")
    args = parser.parse_args()

    if args.selftest:
        return cmd_selftest()

    repo_root = os.path.abspath(args.repo_root)
    out_dir = os.path.abspath(args.out) if args.out else os.path.join(repo_root, "_site")
    cfg = pr_review.load_config()
    return build(repo_root, out_dir, cfg, args.root_json, args.allow_empty, args.strict)


if __name__ == "__main__":
    sys.exit(main())
