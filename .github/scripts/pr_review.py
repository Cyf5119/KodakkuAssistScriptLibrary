#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KodakkuAssistScriptLibrary PR 自动审核。

审核规则（见 README.md）：
  1. 只允许改动「以 PR 作者的 GitHub 用户名命名」的第一层文件夹；
  2. 只允许新增 / 修改 / 删除 .json 文件（重命名要求两端都是 .json）；
  3. 每个 .json 必须是合法的 UTF-8 JSON —— 顶层为数组，元素对应插件里的
     KodakkuAssist.Script.OnlineScriptInfo（Interface/ScriptAttribute.cs）：
     Name / Guid / Version / Author / Repo / DownloadUrl / Note / UpdateInfo / TerritoryIds。

只使用标准库，不依赖任何第三方包；只通过网络 API 读取 PR 内容，从不执行 PR 中的代码。

用法：
  python3 pr_review.py                                   # CI 模式（由 workflow 调用）
  python3 pr_review.py --schema-check Karlin-Z/OnlineRepo.json [更多文件...]
  python3 pr_review.py --path-check Karlin-Z --path Karlin-Z/OnlineRepo.json
  python3 pr_review.py --selftest                        # 内置自测，不联网
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "pr_review_rules.json")

# --------------------------------------------------------------------------- #
# 规则常量
# --------------------------------------------------------------------------- #

#: 条目允许出现的全部字段
ALLOWED_FIELDS = {
    "Name",
    "Guid",
    "Version",
    "Author",
    "Repo",
    "DownloadUrl",
    "Note",
    "UpdateInfo",
    "TerritoryIds",
}

#: 必须存在且为字符串的字段
REQUIRED_STRING_FIELDS = ("Name", "Guid", "Version", "Author", "DownloadUrl")

#: 存在时必须为字符串的字段
OPTIONAL_STRING_FIELDS = ("Repo", "Note", "UpdateInfo")

#: 不允许为空的字段
NON_EMPTY_FIELDS = ("Name", "Guid", "Version", "Author")

#: 插件用 NuGetVersion 解析 Version（ScriptManager.cs / ScriptBrowserColumn.cs），
#: 非法值会在 UI 渲染时抛异常，因此这里按 NuGet 版本号格式校验。
VERSION_RE = re.compile(
    r"^\d+(\.\d+){0,3}(-[0-9A-Za-z.\-]+)?(\+[0-9A-Za-z.\-]+)?$"
)
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
URL_RE = re.compile(r"^https?://[^\s]+$")

#: OnlineScriptInfo.TerritoryIds 是 HashSet<uint>
UINT_MAX = 0xFFFFFFFF

#: Name / Author 会被插件拼成保存文件名（"Name_Author.cs"），必须能安全用作文件名
INVALID_FILENAME_CHARS = set('<>:"/\\|?*')
WINDOWS_RESERVED_NAMES = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

#: 单个 JSON 文件大小上限（防止误提交大文件）
MAX_FILE_BYTES = 2 * 1024 * 1024
#: GitHub files API 最多返回 3000 个文件
MAX_FILES_HARD_LIMIT = 3000

DEFAULT_CONFIG = {
    # 用户名与文件夹名比较时是否忽略大小写（GitHub 用户名本身不区分大小写）
    "username_case_insensitive": True,
    # 是否允许在 <用户名>/ 下再建子目录
    "allow_subfolders": True,
    # 若设为文件名（如 "OnlineRepo.json"），则该名字为强制要求；null 表示不限
    "required_filename": None,
    # 出现未知字段时的处理：warn / error / ignore
    "unknown_fields": "warn",
    # 单个 PR 允许改动的文件数上限
    "max_files_per_pr": 100,
    # 白名单账号：列表中的登录名不受「只能改自己文件夹」限制（仍会校验 JSON）。默认空。
    "maintainers": [],
}

STATUS_LABEL = {
    "added": "新增",
    "modified": "修改",
    "removed": "删除",
    "renamed": "重命名",
    "copied": "复制",
    "changed": "变更",
    "unchanged": "未变",
}


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def type_name(value) -> str:
    """把 Python 类型翻译成中文，便于写错误信息。"""
    return {
        dict: "对象",
        list: "数组",
        str: "字符串",
        bool: "布尔值",
        int: "整数",
        float: "小数",
        type(None): "null",
    }.get(type(value), type(value).__name__)


def default_config() -> dict:
    """返回默认配置的深拷贝。

    DEFAULT_CONFIG 里含有 list（maintainers），浅拷贝会让调用方就地修改时污染默认值，
    进而影响同进程后续所有 load_config() 的结果。
    """
    return copy.deepcopy(DEFAULT_CONFIG)


def load_config() -> dict:
    cfg = default_config()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            user_cfg = json.load(fh)
        if isinstance(user_cfg, dict):
            cfg.update(user_cfg)
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError) as exc:
        print(f"::warning::无法读取审核规则配置 {CONFIG_PATH}: {exc}")
    return cfg


def normalize_login(login: str, case_insensitive: bool) -> str:
    login = (login or "").strip()
    return login.lower() if case_insensitive else login


def check_filename_component(value: str, label: str, errors: list):
    """Name / Author 会被插件拼成保存文件名：

        Path.Combine(saveFolder, $"{info.Name}_{info.Author}.cs")   # ScriptManager.cs

    含路径分隔符或 `..` 时可能写到 ScriptCache 目录之外，含非法字符时直接失败。
    """
    if any(ord(ch) < 32 for ch in value):
        errors.append(f"{label} 含有控制字符，不能用作文件名")
    bad = sorted(set(value) & INVALID_FILENAME_CHARS)
    if bad:
        errors.append(
            f"{label} 含有不能用于文件名的字符：{' '.join(bad)}"
            f"（插件用「Name_Author.cs」作为保存文件名）"
        )
    if value != value.strip():
        errors.append(f"{label} 首尾有多余空白")
    stem = value.rstrip(" .")
    if stem != value:
        errors.append(f"{label} 不能以点或空格结尾")
    elif stem.upper() in WINDOWS_RESERVED_NAMES:
        errors.append(f"{label} 是 Windows 保留设备名，不能用作文件名")


# --------------------------------------------------------------------------- #
# 规则 1 + 2：路径校验
# --------------------------------------------------------------------------- #


def check_path_rules(path, author, cfg, errors, ctx):
    """校验单个路径是否满足「自己的文件夹 / 仅 .json」两条规则。"""
    if not isinstance(path, str) or not path:
        errors.append(f"{ctx}：路径为空或非法")
        return

    if path.startswith("/") or "\\" in path:
        errors.append(f"{ctx}：{path!r} 不是合法的仓库相对路径")
        return

    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        errors.append(f"{ctx}：{path!r} 包含非法路径片段")
        return

    if len(parts) < 2:
        errors.append(
            f"{ctx}：文件 {path!r} 必须放在以你的 GitHub 用户名命名的文件夹里，"
            f"例如 {author}/OnlineRepo.json"
        )
        return

    folder = parts[0]
    same = normalize_login(folder, cfg["username_case_insensitive"]) == normalize_login(
        author, cfg["username_case_insensitive"]
    )
    if not same:
        errors.append(
            f"{ctx}：第一层文件夹 {folder!r} 与 PR 作者 {author!r} 不一致；"
            f"你只能修改自己的文件夹 {author}/"
        )

    if not cfg.get("allow_subfolders", True) and len(parts) > 2:
        errors.append(f"{ctx}：不允许在 {folder}/ 下创建子文件夹（{path!r}）")

    filename = parts[-1]
    if not filename.lower().endswith(".json"):
        errors.append(f"{ctx}：只允许 .json 文件，{filename!r} 不合法")

    required = cfg.get("required_filename")
    if required and filename != required:
        errors.append(f"{ctx}：文件名必须是 {required!r}，实际为 {filename!r}")


# --------------------------------------------------------------------------- #
# 规则 3：JSON 合规性校验
# --------------------------------------------------------------------------- #


def validate_json_document(text: str, path: str, cfg: dict):
    """校验 JSON 文本，返回 (errors, warnings)。"""
    errors: list[str] = []
    warnings: list[str] = []

    if text.startswith("\ufeff"):
        warnings.append(f"{path}：文件带有 UTF-8 BOM，已忽略；建议用「无 BOM 的 UTF-8」保存")
        text = text[1:]

    if not text.strip():
        errors.append(f"{path}：文件内容为空")
        return errors, warnings

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        errors.append(
            f"{path}：JSON 语法错误 —— {exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）"
        )
        return errors, warnings

    if not isinstance(data, list):
        errors.append(f"{path}：顶层必须是数组 [ ... ]，实际是{type_name(data)}")
        return errors, warnings

    if not data:
        warnings.append(f"{path}：顶层数组为空，没有任何脚本条目")

    guid_seen: dict[str, int] = {}
    name_seen: dict[str, int] = {}
    for idx, entry in enumerate(data):
        validate_entry(idx, entry, path, cfg, errors, warnings, guid_seen, name_seen)
    return errors, warnings


def validate_entry(idx, entry, path, cfg, errors, warnings, guid_seen, name_seen):
    label = f"{path}[{idx}]"

    if not isinstance(entry, dict):
        errors.append(f"{label}：每一项必须是 JSON 对象，实际是{type_name(entry)}")
        return

    for field in REQUIRED_STRING_FIELDS:
        if field not in entry:
            errors.append(f"{label}：缺少必填字段 {field}")
        elif not isinstance(entry[field], str):
            errors.append(f"{label}：{field} 必须是字符串，实际是{type_name(entry[field])}")

    for field in OPTIONAL_STRING_FIELDS:
        if field not in entry:
            continue
        value = entry[field]
        if value is None:
            errors.append(f"{label}：{field} 不能为 null，会导致插件读取时异常")
        elif isinstance(value, (dict, list)):
            errors.append(
                f"{label}：{field} 必须是字符串，实际是{type_name(value)}，会导致整个文件解析失败"
            )
        elif isinstance(value, bool) or isinstance(value, (int, float)):
            warnings.append(
                f"{label}：{field} 写成了{type_name(value)}，会被转换成字符串，建议直接写字符串"
            )

    for field in NON_EMPTY_FIELDS:
        value = entry.get(field)
        if isinstance(value, str) and not value.strip():
            errors.append(f"{label}：{field} 不能为空")

    for field in ("Name", "Author"):
        value = entry.get(field)
        if isinstance(value, str) and value.strip():
            check_filename_component(value, f"{label} 的 {field}", errors)

    version = entry.get("Version")
    if isinstance(version, str) and version.strip():
        if not VERSION_RE.match(version.strip()):
            errors.append(
                f"{label}：Version {version!r} 不是合法的版本号 "
                f"（插件用 NuGetVersion 解析，如 0.0.1 / 0.0.0.9 / 1.0.0-beta）"
            )

    guid = entry.get("Guid")
    if isinstance(guid, str) and guid.strip():
        if not UUID_RE.match(guid.strip()):
            warnings.append(
                f"{label}：Guid {guid!r} 不是标准 UUID 格式，插件可能无法正确识别"
            )
        key = guid.strip().lower()
        if key in guid_seen:
            errors.append(f"{label}：Guid 与 {path}[{guid_seen[key]}] 重复")
        else:
            guid_seen[key] = idx

    name = entry.get("Name")
    if isinstance(name, str) and name.strip():
        if name.strip() in name_seen:
            warnings.append(f"{label}：Name 与 {path}[{name_seen[name.strip()]}] 重复")
        else:
            name_seen[name.strip()] = idx

    if "TerritoryIds" not in entry:
        errors.append(f"{label}：缺少必填字段 TerritoryIds")
    elif not isinstance(entry["TerritoryIds"], list):
        errors.append(
            f"{label}：TerritoryIds 必须是整数数组，实际是{type_name(entry['TerritoryIds'])}"
        )
    else:
        for pos, tid in enumerate(entry["TerritoryIds"]):
            if isinstance(tid, bool) or not isinstance(tid, int):
                errors.append(
                    f"{label}：TerritoryIds[{pos}] 必须是整数，实际是{type_name(tid)}"
                )
            elif not 0 <= tid <= UINT_MAX:
                errors.append(
                    f"{label}：TerritoryIds[{pos}] = {tid} 超出 uint 范围"
                    f"（0~{UINT_MAX}），会导致整个文件解析失败"
                )

    download = entry.get("DownloadUrl")
    if isinstance(download, str) and download.strip():
        if not URL_RE.match(download.strip()):
            errors.append(f"{label}：DownloadUrl 必须以 http:// 或 https:// 开头")
        elif not download.strip().lower().startswith("https://"):
            warnings.append(f"{label}：DownloadUrl 建议使用 https")

    # Repo 不校验内容：插件读取时会用当前订阅地址覆盖它（ScriptManager.cs: info.Repo = repoUrl）

    unknown = sorted(set(entry) - ALLOWED_FIELDS)
    if unknown:
        message = f"{label}：存在未知字段 {', '.join(unknown)}"
        mode = cfg.get("unknown_fields", "warn")
        if mode == "error":
            errors.append(message)
        elif mode == "warn":
            warnings.append(message)


# --------------------------------------------------------------------------- #
# GitHub API
# --------------------------------------------------------------------------- #

API_URL = os.environ.get("GITHUB_API_URL", "https://api.github.com")


def _api_headers(accept: str) -> dict:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    headers = {
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "kasl-pr-reviewer",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def api_json(path: str):
    request = urllib.request.Request(
        f"{API_URL}{path}", headers=_api_headers("application/vnd.github+json")
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
    return json.loads(body) if body else None


def api_raw(path: str) -> bytes:
    request = urllib.request.Request(
        f"{API_URL}{path}", headers=_api_headers("application/vnd.github.raw")
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def list_changed_files(repo: str, pr_number: str) -> list:
    files: list = []
    page = 1
    while True:
        batch = api_json(f"/repos/{repo}/pulls/{pr_number}/files?per_page=100&page={page}")
        if not batch:
            break
        files.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return files


def fetch_file_bytes(repo: str, path: str, ref: str):
    """读取 PR 中某个文件的原始字节。返回 (data, error_message)。"""
    quoted = urllib.parse.quote(path, safe="/")
    info = api_json(f"/repos/{repo}/contents/{quoted}?ref={urllib.parse.quote(ref, safe='')}")
    if not isinstance(info, dict) or info.get("type") != "file":
        kind = info.get("type") if isinstance(info, dict) else type_name(info)
        return None, f"只允许普通文件（不允许符号链接 / 子模块 / 目录），实际类型为 {kind}"
    size = info.get("size") or 0
    if size > MAX_FILE_BYTES:
        return None, f"文件过大（{size} 字节，上限 {MAX_FILE_BYTES} 字节）"
    if info.get("encoding") == "base64" and info.get("content"):
        try:
            return base64.b64decode(info["content"]), None
        except (ValueError, TypeError) as exc:
            return None, f"内容解码失败：{exc}"
    # 超过 1 MB 时 contents API 不返回 content，改用 raw 媒体类型
    return api_raw(f"/repos/{repo}/contents/{quoted}?ref={urllib.parse.quote(ref, safe='')}"), None


# --------------------------------------------------------------------------- #
# 审核主流程
# --------------------------------------------------------------------------- #


def check_change_set(files, author, cfg):
    """返回 (errors, warnings, rows)。files 为 GitHub files API 的元素列表。"""
    errors: list[str] = []
    warnings: list[str] = []
    rows: list[tuple[str, str]] = []

    max_files = cfg.get("max_files_per_pr", 100)
    if len(files) > max_files:
        errors.append(f"本 PR 改动了 {len(files)} 个文件，超过上限 {max_files} 个")

    bypass = author in (cfg.get("maintainers") or [])
    if bypass:
        warnings.append(f"{author} 在维护者白名单中，跳过「只能修改自己文件夹」限制")

    for item in files:
        status = item.get("status", "changed")
        path = item.get("filename", "")
        previous = item.get("previous_filename")
        label = STATUS_LABEL.get(status, status)
        rows.append((label, path))

        if not bypass:
            check_path_rules(path, author, cfg, errors, f"文件 {path}")

        # 重命名 / 复制：来源路径同样必须合法，否则等于绕过限制
        if status in ("renamed", "copied") and previous:
            prefix = "重命名前" if status == "renamed" else "复制来源"
            rows.append((f"{label}（来源）", previous))
            if not bypass:
                check_path_rules(previous, author, cfg, errors, f"文件 {previous}（{prefix}路径）")

    return errors, warnings, rows


def describe(files, errors, warnings):
    lines = []
    lines.append("### 改动清单")
    lines.append("")
    lines.append("| 状态 | 文件 |")
    lines.append("| --- | --- |")
    for label, path in files:
        lines.append(f"| {label} | `{path}` |")
    lines.append("")

    if errors:
        lines.append("### ❌ 需要修复")
        lines.append("")
        for index, item in enumerate(errors, 1):
            lines.append(f"{index}. {item}")
        lines.append("")

    if warnings:
        lines.append("### ⚠️ 提醒（不影响合并）")
        lines.append("")
        for item in warnings:
            lines.append(f"- {item}")
        lines.append("")

    if not errors and not warnings:
        lines.append("未发现问题。")
        lines.append("")

    lines.append(
        "> 规则：只能修改以自己 GitHub 用户名命名的文件夹，且只能新增 / 修改 / 删除 `.json`；"
        "JSON 顶层必须是数组，条目字段见 `README.md`。"
    )
    return "\n".join(lines)


def build_report(title, rows, errors, warnings):
    body = [f"## 🤖 PR 自动审核：{title}", ""]
    body.append(describe(rows, errors, warnings))
    return "\n".join(body)


def write_step_summary(markdown: str):
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as fh:
                fh.write(markdown + "\n")
        except OSError:
            pass


def post_comment(repo: str, pr_number: str, body: str):
    request = urllib.request.Request(
        f"{API_URL}/repos/{repo}/issues/{pr_number}/comments",
        data=json.dumps({"body": body}).encode("utf-8"),
        headers=_api_headers("application/vnd.github+json"),
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()


def run_ci() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    pr_number = os.environ.get("PR_NUMBER", "")
    author = os.environ.get("PR_AUTHOR", "")
    head_sha = os.environ.get("PR_HEAD_SHA", "")

    missing = [k for k, v in (
        ("GITHUB_REPOSITORY", repo), ("PR_NUMBER", pr_number), ("PR_AUTHOR", author)
    ) if not v]
    if missing:
        print(f"::error::缺少环境变量：{', '.join(missing)}")
        return 2

    cfg = load_config()

    try:
        files = list_changed_files(repo, pr_number)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        message = f"无法读取 PR 改动（GitHub API 错误）：{exc}"
        print(f"::error::{message}")
        write_step_summary(f"## 🤖 PR 自动审核：无法执行\n\n{message}\n")
        try:
            post_comment(repo, pr_number, f"## 🤖 PR 自动审核：无法执行\n\n{message}\n\n未做合并，请维护者手动检查。")
        except Exception:  # noqa: BLE001 - 留言失败不应掩盖原始错误
            pass
        return 2

    if not files:
        errors = ["该 PR 没有包含任何文件改动"]
        warnings: list[str] = []
        rows: list[tuple[str, str]] = []
    else:
        errors, warnings, rows = check_change_set(files, author, cfg)

        for item in files:
            path = item.get("filename", "")
            if item.get("status") == "removed":
                continue
            try:
                data, fetch_error = fetch_file_bytes(repo, path, head_sha)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                errors.append(f"{path}：读取文件内容失败 —— {exc}")
                continue
            if fetch_error:
                errors.append(f"{path}：{fetch_error}")
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                errors.append(f"{path}：不是合法的 UTF-8 编码（{exc}）")
                continue
            file_errors, file_warnings = validate_json_document(text, path, cfg)
            errors.extend(file_errors)
            warnings.extend(file_warnings)

    if errors:
        report = build_report("❌ 未通过", rows, errors, warnings)
        print(report)
        write_step_summary(report)
        try:
            post_comment(repo, pr_number, report)
        except Exception as exc:  # noqa: BLE001
            print(f"::warning::无法在 PR 下留言：{exc}")
        return 1

    report = build_report("✅ 通过", rows, errors, warnings)
    print(report)
    write_step_summary(report)
    if warnings:
        try:
            post_comment(
                repo,
                pr_number,
                report + "\n校验通过，将自动 squash 合并。",
            )
        except Exception as exc:  # noqa: BLE001
            print(f"::warning::无法在 PR 下留言：{exc}")
    return 0


# --------------------------------------------------------------------------- #
# 本地子命令
# --------------------------------------------------------------------------- #


def cmd_schema_check(paths, cfg) -> int:
    exit_code = 0
    for path in paths:
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            print(f"{path}：无法读取 —— {exc}")
            exit_code = 2
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            print(f"{path}：不是合法的 UTF-8 编码（{exc}）")
            exit_code = 1
            continue
        errors, warnings = validate_json_document(text, path, cfg)
        for item in warnings:
            print(f"⚠️  {item}")
        for item in errors:
            print(f"❌ {item}")
        if errors:
            exit_code = 1
        else:
            print(f"✅ {path} 通过")
    return exit_code


def cmd_path_check(author, paths, cfg) -> int:
    exit_code = 0
    for path in paths:
        errors: list[str] = []
        check_path_rules(path, author, cfg, errors, "路径")
        for item in errors:
            print(f"❌ {item}")
        if errors:
            exit_code = 1
        else:
            print(f"✅ {path} 通过（作者 {author}）")
    return exit_code


def cmd_selftest() -> int:
    cfg = default_config()
    failures: list[str] = []

    def expect(name, condition, detail=""):
        if condition:
            print(f"✅ {name}")
        else:
            failures.append(name)
            print(f"❌ {name} {detail}")

    valid_entry = {
        "Name": "M1s绘图",
        "Guid": "8010d865-7d6d-4c23-92e0-f4b0120e18ac",
        "Version": "0.0.0.9",
        "Author": "Karlin",
        "Repo": "",
        "DownloadUrl": "https://raw.githubusercontent.com/x/y/main/a.cs",
        "Note": "",
        "UpdateInfo": "",
        "TerritoryIds": [1226],
    }
    doc = json.dumps([valid_entry], ensure_ascii=False)
    errors, warnings = validate_json_document(doc, "f.json", cfg)
    expect("合法文档通过", not errors, str(errors))
    expect("合法文档无警告", not warnings, str(warnings))

    errors, _ = validate_json_document(doc + ",", "f.json", cfg)
    expect("尾随逗号被拒绝", bool(errors))

    errors, _ = validate_json_document("{}", "f.json", cfg)
    expect("顶层非数组被拒绝", bool(errors))

    errors, _ = validate_json_document(json.dumps({"a": 1}) + "", "f.json", cfg)
    expect("顶层对象被拒绝", bool(errors))

    broken = dict(valid_entry)
    broken.pop("Name")
    errors, _ = validate_json_document(json.dumps([broken]), "f.json", cfg)
    expect("缺必填字段被拒绝", bool(errors))

    bad_type = dict(valid_entry)
    bad_type["TerritoryIds"] = [True, "1"]
    errors, _ = validate_json_document(json.dumps([bad_type]), "f.json", cfg)
    expect("TerritoryIds 非整数被拒绝", bool(errors))

    bad_version = dict(valid_entry)
    bad_version["Version"] = "v1.0"
    errors, _ = validate_json_document(json.dumps([bad_version]), "f.json", cfg)
    expect("Version 格式错误被拒绝", bool(errors))

    dup = dict(valid_entry)
    errors, _ = validate_json_document(json.dumps([valid_entry, dup]), "f.json", cfg)
    expect("重复 Guid 被拒绝", bool(errors))

    nonstandard_guid = dict(valid_entry)
    nonstandard_guid["Guid"] = "d99c7e91-9b56-432d-a3a8-49a8586915b7e2a"
    errors, warnings = validate_json_document(json.dumps([nonstandard_guid]), "f.json", cfg)
    expect("非标准 Guid 仅告警不报错", not errors and bool(warnings), str(errors))

    errors, warnings = validate_json_document("\ufeff" + doc, "f.json", cfg)
    expect("BOM 被容忍", not errors and bool(warnings))

    unknown = dict(valid_entry)
    unknown["Extra"] = 1
    errors, warnings = validate_json_document(json.dumps([unknown]), "f.json", cfg)
    expect("未知字段默认仅告警", not errors and bool(warnings))

    errors, _ = validate_json_document(json.dumps([{"Name": "x"}]), "f.json", cfg)
    expect("完全不合规对象被拒绝", bool(errors))

    # --- 依据 OnlineScriptInfo 的真实语义补充的用例 ---
    negative = dict(valid_entry)
    negative["TerritoryIds"] = [-1]
    errors, _ = validate_json_document(json.dumps([negative]), "f.json", cfg)
    expect("负数 TerritoryIds 被拒绝", bool(errors))

    overflow = dict(valid_entry)
    overflow["TerritoryIds"] = [UINT_MAX + 1]
    errors, _ = validate_json_document(json.dumps([overflow]), "f.json", cfg)
    expect("超出 uint 的 TerritoryIds 被拒绝", bool(errors))

    max_tid = dict(valid_entry)
    max_tid["TerritoryIds"] = [UINT_MAX]
    errors, _ = validate_json_document(json.dumps([max_tid]), "f.json", cfg)
    expect("uint 最大值被接受", not errors, str(errors))

    traversal = dict(valid_entry)
    traversal["Name"] = "../evil"
    errors, _ = validate_json_document(json.dumps([traversal]), "f.json", cfg)
    expect("Name 含路径穿越被拒绝", bool(errors))

    reserved = dict(valid_entry)
    reserved["Author"] = "CON"
    errors, _ = validate_json_document(json.dumps([reserved]), "f.json", cfg)
    expect("Author 为 Windows 保留名被拒绝", bool(errors))

    colon = dict(valid_entry)
    colon["Name"] = "a:b"
    errors, _ = validate_json_document(json.dumps([colon]), "f.json", cfg)
    expect("Name 含非法文件名字符被拒绝", bool(errors))

    prerelease = dict(valid_entry)
    prerelease["Version"] = "1.0.0-beta.1+build5"
    errors, _ = validate_json_document(json.dumps([prerelease]), "f.json", cfg)
    expect("NuGet 预发布版本号通过", not errors, str(errors))

    single = dict(valid_entry)
    single["Version"] = "1"
    errors, _ = validate_json_document(json.dumps([single]), "f.json", cfg)
    expect("单段版本号通过", not errors, str(errors))

    repo_value = dict(valid_entry)
    repo_value["Repo"] = "随便写的值"
    errors, _ = validate_json_document(json.dumps([repo_value]), "f.json", cfg)
    expect("Repo 内容不再校验（会被插件覆盖）", not errors, str(errors))

    note_object = dict(valid_entry)
    note_object["Note"] = {"a": 1}
    errors, _ = validate_json_document(json.dumps([note_object]), "f.json", cfg)
    expect("可选字段为对象被拒绝", bool(errors))

    note_number = dict(valid_entry)
    note_number["Note"] = 123
    errors, warnings = validate_json_document(json.dumps([note_number]), "f.json", cfg)
    expect("可选字段为数字仅告警", not errors and bool(warnings))

    null_tid = dict(valid_entry)
    null_tid["TerritoryIds"] = None
    errors, _ = validate_json_document(json.dumps([null_tid]), "f.json", cfg)
    expect("TerritoryIds 为 null 被拒绝", bool(errors))

    def path_errors(path, author, config=None):
        collected: list[str] = []
        check_path_rules(path, author, config or cfg, collected, "路径")
        return collected

    expect("正确文件夹通过", not path_errors("Karlin-Z/OnlineRepo.json", "Karlin-Z"))
    expect("大小写不同通过", not path_errors("karlin-z/OnlineRepo.json", "Karlin-Z"))
    expect("子目录通过", not path_errors("Karlin-Z/sub/a.json", "Karlin-Z"))
    expect("改别人文件夹被拒绝", bool(path_errors("Other/a.json", "Karlin-Z")))
    expect("根目录文件被拒绝", bool(path_errors("OnlineRepo.json", "Karlin-Z")))
    expect("非 json 被拒绝", bool(path_errors("Karlin-Z/a.cs", "Karlin-Z")))
    expect("路径穿越被拒绝", bool(path_errors("Karlin-Z/../Other/a.json", "Karlin-Z")))
    expect("绝对路径被拒绝", bool(path_errors("/Karlin-Z/a.json", "Karlin-Z")))

    strict_sub = copy.deepcopy(cfg)
    strict_sub["allow_subfolders"] = False
    expect("禁止子目录时被拒绝", bool(path_errors("Karlin-Z/sub/a.json", "Karlin-Z", strict_sub)))

    strict_name = copy.deepcopy(cfg)
    strict_name["required_filename"] = "OnlineRepo.json"
    expect("限定文件名时被拒绝", bool(path_errors("Karlin-Z/other.json", "Karlin-Z", strict_name)))
    expect("限定文件名时通过", not path_errors("Karlin-Z/OnlineRepo.json", "Karlin-Z", strict_name))

    case_sensitive = copy.deepcopy(cfg)
    case_sensitive["username_case_insensitive"] = False
    expect(
        "区分大小写时被拒绝",
        bool(path_errors("karlin-z/a.json", "Karlin-Z", case_sensitive)),
    )

    change_files = [
        {"status": "added", "filename": "Karlin-Z/a.json"},
        {"status": "removed", "filename": "Karlin-Z/b.json"},
    ]
    errors, _, rows = check_change_set(change_files, "Karlin-Z", cfg)
    expect("改动集合校验通过", not errors and len(rows) == 2, str(errors))

    rename_out = [
        {
            "status": "renamed",
            "filename": "Karlin-Z/a.json",
            "previous_filename": "Other/a.json",
        }
    ]
    errors, _, _ = check_change_set(rename_out, "Karlin-Z", cfg)
    expect("从别人文件夹重命名过来被拒绝", bool(errors))

    bypass_cfg = copy.deepcopy(cfg)
    bypass_cfg["maintainers"] = ["Karlin-Z"]
    errors, _, _ = check_change_set(
        [{"status": "added", "filename": "README.md"}], "Karlin-Z", bypass_cfg
    )
    expect("维护者白名单可跳过路径限制", not errors, str(errors))

    # 回归测试：配置拷贝必须是深拷贝。若 default_config() 改回 dict(DEFAULT_CONFIG)，
    # 下面的就地 append 会污染 DEFAULT_CONFIG，这条断言就会失败。
    probe = default_config()
    probe["maintainers"].append("__probe__")
    expect("default_config 返回深拷贝", DEFAULT_CONFIG["maintainers"] == [])

    print()
    if failures:
        print(f"❌ 自测失败 {len(failures)} 项：{', '.join(failures)}")
        return 1
    print("✅ 全部自测通过")
    return 0


# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(description="KodakkuAssistScriptLibrary PR 自动审核")
    parser.add_argument("--schema-check", nargs="+", metavar="FILE", help="校验本地 JSON 文件")
    parser.add_argument("--path-check", metavar="AUTHOR", help="校验路径规则（配合 --path）")
    parser.add_argument("--path", action="append", default=[], help="待校验路径，可重复")
    parser.add_argument("--selftest", action="store_true", help="运行内置自测")
    args = parser.parse_args()

    cfg = load_config()

    if args.selftest:
        return cmd_selftest()
    if args.schema_check:
        return cmd_schema_check(args.schema_check, cfg)
    if args.path_check:
        if not args.path:
            parser.error("--path-check 需要至少一个 --path")
        return cmd_path_check(args.path_check, args.path, cfg)
    return run_ci()


if __name__ == "__main__":
    sys.exit(main())
