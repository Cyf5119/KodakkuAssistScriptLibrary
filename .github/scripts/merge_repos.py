#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫描所有贡献者文件夹里的 .cs，提取 [ScriptType(...)]，生成合并后的索引。

产物（可分别开关）：
  --out DIR          站点目录：DIR/index.json（合并索引）+ DIR/index.html（说明页）
  --root-json PATH   仓库根目录的总索引，例如 OnlineRepo.json
  --guid-map PATH    guid -> 源文件 映射表，供 PR 审核阶段查重

合并规则：
  - 只扫描仓库第一层里不以 . 或 _ 开头的目录（即贡献者文件夹），进入文件夹后递归全部子目录
  - 每个 .cs 用 pr_review.validate_script_file 校验（与 PR 审核同一套规则）
  - DownloadUrl 自动填成本仓库该 .cs 的 raw 直链
  - UpdateTime 取该 .cs 的最后提交时间（UTC），未提交时退回文件修改时间
  - 值为空的字段（Note / UpdateInfo / UpdateTime / TerritoryIds 等）直接省略
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
    "DownloadUrl",
    "Note",
    "UpdateInfo",
    "UpdateTime",
    "TerritoryIds",
]

#: 任何层级都不参与合并的目录名
EXCLUDED_DIRS = {".git", "node_modules"}

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
    """该 .cs 在本仓库的 raw 直链，作为索引里的 DownloadUrl。"""
    return (
        f"https://raw.githubusercontent.com/{repo}/"
        f"{urllib.parse.quote(branch, safe='')}/{urllib.parse.quote(rel_path, safe='/')}"
    )


def _commit_epoch(repo_root, rel_path):
    """该文件最后一次提交的 Unix 时间戳；取不到（非 git 仓库 / 未提交）时返回 None。"""
    try:
        result = subprocess.run(
            ["git", "-C", repo_root, "log", "-1", "--format=%ct", "--", rel_path],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    stamp = result.stdout.strip()
    return int(stamp) if stamp.isdigit() else None


def last_update_time(repo_root, rel_path):
    """索引里的 UpdateTime：优先取该文件的最后提交时间，取不到时退回文件修改时间。

    时间统一转成 UTC ISO8601（`2026-09-12T05:45:00Z`），保证同一份内容在
    不同机器 / 多次生成下结果一致，避免「无改动也提交」。
    """
    epoch = _commit_epoch(repo_root, rel_path)
    if epoch is None:
        try:
            epoch = int(os.path.getmtime(os.path.join(repo_root, rel_path)))
        except OSError:
            return ""
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_entry(meta, repo, branch, rel_path, update_time):
    """把 validate_script_file 的 meta 组装成 OnlineScriptInfo 字段。

    字段顺序固定；值为空（空串 / 空数组）的字段直接省略，插件端会回退到默认值。
    `Repo` 由插件在订阅时用当前订阅地址覆盖，所以永远不写。
    """
    entry = {
        "Name": meta["name"],
        "Guid": meta["guid"],
        "Version": meta["version"],
        "Author": meta["author"],
        "DownloadUrl": download_url(repo, branch, rel_path),
        "Note": meta["note"],
        "UpdateInfo": meta["update_info"],
        "UpdateTime": update_time,
        "TerritoryIds": meta["territorys"],
    }
    return {
        key: entry[key]
        for key in CANONICAL_FIELD_ORDER
        if key in entry and entry[key] not in ("", [], None)
    }


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

        update_time = last_update_time(repo_root, rel)
        records.append((owner, rel, build_entry(meta, repo, branch, rel, update_time)))

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
    """贡献者维度的 HTML 表格。"""
    rows = ["<table>", "<tr><th>贡献者</th><th>脚本文件</th><th>收录脚本</th></tr>"]
    for owner in sorted(owner_counts):
        rows.append(
            f"<tr><td>{owner}</td><td>{file_counts.get(owner, 0)}</td>"
            f"<td>{owner_counts[owner]}</td></tr>"
        )
    rows.append("</table>")
    return "\n".join(rows)


def render_problems(conflicts, skipped):
    """冲突与跳过文件的 HTML 列表；都没有则返回空串。"""
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
    """站点说明页。"""
    return INDEX_HTML_TEMPLATE.format(
        entry_count=len(merged),
        owner_count=len(owner_counts),
        table=render_table(owner_counts, file_counts),
        problems=render_problems(conflicts, skipped),
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
    )


def write_text(path, text):
    """写文本文件，自动创建父目录，统一 LF 换行。"""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def write_outputs(dest, payload, guid_map, html):
    """dest: {"site": 站点目录, "root": 总索引路径, "guid_map": 映射表路径}，空值表示不输出。"""
    if dest["site"]:
        os.makedirs(dest["site"], exist_ok=True)
        write_text(os.path.join(dest["site"], "index.json"), payload + "\n")
        write_text(os.path.join(dest["site"], "index.html"), html + "\n")
    if dest["root"]:
        write_text(dest["root"], payload + "\n")
    if dest["guid_map"]:
        # guid -> 源文件，供 PR 审核阶段查重（不进公开索引）
        write_text(
            dest["guid_map"],
            json.dumps(guid_map, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )


def emit_report(stats, dest):
    """把生成结果写到 stdout 与 Actions 摘要。

    stats: (merged, file_counts, owner_counts, conflicts, skipped, warnings)
    """
    merged, file_counts, owner_counts, conflicts, skipped, warnings = stats
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
    if dest["site"]:
        outputs.append(f"`{dest['site']}`（index.json + index.html）")
    if dest["root"]:
        outputs.append(f"`{dest['root']}`")
    if dest["guid_map"]:
        outputs.append(f"`{dest['guid_map']}`")
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


def _git(args):
    """执行只读 git 命令并返回 stdout；失败时返回空字符串。"""
    try:
        return subprocess.run(
            ["git", "-C", REPO_ROOT_DEFAULT, *args],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def resolve_repo(explicit):
    """确定 owner/repo：显式参数 -> GITHUB_REPOSITORY -> origin 远端地址。"""
    if explicit:
        return explicit
    if os.environ.get("GITHUB_REPOSITORY"):
        return os.environ["GITHUB_REPOSITORY"]
    match = re.search(r"github\.com[:/]([^/]+)/(.+?)(?:\.git)?$",
                      _git(["config", "--get", "remote.origin.url"]))
    return f"{match.group(1)}/{match.group(2)}" if match else ""


def resolve_branch(explicit):
    """确定分支名：显式参数 -> GITHUB_REF_NAME -> 当前分支 -> main。"""
    return (
        explicit
        or os.environ.get("GITHUB_REF_NAME")
        or _git(["rev-parse", "--abbrev-ref", "HEAD"])
        or "main"
    )


def build(repo_root, cfg, repo, branch, out_dir=None, root_json_path=None,
          guid_map_path=None, root_json_mode=False, forbid_empty=False, strict=False):
    """生成合并索引并写出。返回进程退出码。"""
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

    dest = {"site": out_dir, "root": root_json_path, "guid_map": guid_map_path}
    payload = json.dumps(merged, ensure_ascii=False, indent=2)
    # --root-json-mode：让站点根路径直接返回 JSON（插件不检查 Content-Type）
    html = payload if root_json_mode else render_html(
        merged, owner_counts, file_counts, conflicts, skipped
    )

    write_outputs(dest, payload, guid_map, html)
    emit_report((merged, file_counts, owner_counts, conflicts, skipped, warnings), dest)

    if strict and (conflicts or skipped):
        return 1
    return 0


def main() -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="合并所有贡献者 .cs 并生成索引")
    parser.add_argument("--repo-root", default=REPO_ROOT_DEFAULT, help="仓库根目录")
    parser.add_argument("--out", default="_site", help="站点输出目录；配合 --no-site 可跳过")
    parser.add_argument("--no-site", action="store_true", help="不生成站点目录")
    parser.add_argument("--root-json", default="", help="同时写一份总索引到该路径（仓库根目录）")
    parser.add_argument("--guid-map", default="",
                        help=f"guid -> 源文件 映射的输出路径（建议 {pr_review.GUID_MAP_PATH}）")
    parser.add_argument("--root-json-mode", action="store_true",
                        help="让站点根路径 index.html 直接返回 JSON")
    parser.add_argument("--repo", default="", help="owner/repo，用于生成 DownloadUrl")
    parser.add_argument("--branch", default="", help="分支名，用于生成 DownloadUrl")
    parser.add_argument("--forbid-empty", action="store_true", help="没有收录到脚本时报错")
    parser.add_argument("--strict", action="store_true", help="存在跳过/冲突时以非 0 退出")
    args = parser.parse_args()

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
