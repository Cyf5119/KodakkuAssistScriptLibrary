#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KodakkuAssistScriptLibrary PR 自动审核。

审核规则（见 README.md）：
  1. 只允许改动「以 PR 作者的 GitHub 用户名命名」的第一层文件夹；
  2. 只允许新增 / 修改 / 删除 .cs 文件（重命名要求两端都是 .cs）；
  3. 每个 .cs 必须包含且只包含一处 [ScriptType(...)] 特性，字段合法：
     Name / Guid / Version / Author / Repo / DownloadUrl / Note / UpdateInfo / TerritoryIds
     对应插件里的 KodakkuAssist.Script.OnlineScriptInfo（Interface/ScriptAttribute.cs）。

只使用标准库，不依赖任何第三方包；只通过网络 API 读取 PR 内容，从不执行 PR 中的代码，
也不会编译 C#——[ScriptType(...)] 是当作文本解析的。
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
REPO_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import csharp_meta  # noqa: E402  （同目录）

CONFIG_PATH = os.path.join(SCRIPT_DIR, "pr_review_rules.json")

# --------------------------------------------------------------------------- #
# 规则常量
# --------------------------------------------------------------------------- #

#: 生成的 OnlineRepo.json 里允许出现的字段
ALLOWED_FIELDS = {
    "Name", "Guid", "Version", "Author", "Repo",
    "DownloadUrl", "Note", "UpdateInfo", "TerritoryIds",
}
REQUIRED_STRING_FIELDS = ("Name", "Guid", "Version", "Author", "DownloadUrl")
OPTIONAL_STRING_FIELDS = ("Repo", "Note", "UpdateInfo")
NON_EMPTY_FIELDS = ("Name", "Guid", "Version", "Author")

#: 插件用 NuGetVersion 解析 Version（ScriptManager.cs / ScriptBrowserColumn.cs），
#: 非法值会在 UI 渲染时抛异常，因此这里按 NuGet 版本号格式校验。
VERSION_RE = re.compile(r"^\d+(\.\d+){0,3}(-[0-9A-Za-z.\-]+)?(\+[0-9A-Za-z.\-]+)?$")
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

#: C# 侧 author 参数的默认值，等同于「没写作者」
AUTHOR_DEFAULT = "Unknown"
NAME_DEFAULT = "Default Script"
VERSION_DEFAULT = "0.0.0.1"

#: 已提交的 guid -> 源文件 映射（由 merge_repos.py 生成），用于 PR 阶段查重
GUID_MAP_PATH = ".github/index/guid-map.json"

#: 单个文件大小上限
MAX_FILE_BYTES = 2 * 1024 * 1024

DEFAULT_CONFIG = {
    # 用户名与文件夹名比较时是否忽略大小写（GitHub 用户名本身不区分大小写）
    "username_case_insensitive": True,
    # 是否允许在 <用户名>/ 下再建子目录
    "allow_subfolders": True,
    # 贡献者文件夹里允许的文件扩展名
    "allowed_extensions": [".cs"],
    # 单个 PR 允许改动的文件数上限
    "max_files_per_pr": 100,
    # 不参与索引的目录名（任意层级匹配），例如放模板的 examples / templates
    "ignore_dirs": [],
    # 白名单账号：列表中的登录名不受「只能改自己文件夹」限制（仍会校验内容）。默认空。
    "maintainers": [],
    # 生成结果（OnlineRepo.json）里出现未知字段时的处理：warn / error / ignore
    "unknown_fields": "warn",
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

    DEFAULT_CONFIG 里含有 list（maintainers / allowed_extensions），浅拷贝会让调用方
    就地修改时污染默认值，进而影响同进程后续所有 load_config() 的结果。
    """
    return copy.deepcopy(DEFAULT_CONFIG)


def load_config() -> dict:
    """读取 pr_review_rules.json，覆盖默认配置；文件缺失或损坏时退回默认值。"""
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
    """规范化登录名以便比较；GitHub 用户名本身不区分大小写。"""
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
    """校验单个路径是否满足「自己的文件夹 / 只允许指定扩展名」两条规则。"""
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
            f"例如 {author}/MyScript.cs"
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

    extensions = tuple(str(e).lower() for e in cfg.get("allowed_extensions", [".cs"]))
    filename = parts[-1]
    if not filename.lower().endswith(extensions):
        errors.append(
            f"{ctx}：只允许 {' / '.join(extensions)} 文件，{filename!r} 不合法"
        )


# --------------------------------------------------------------------------- #
# 规则 3：.cs 里的 ScriptType 特性
# --------------------------------------------------------------------------- #


def validate_script_file(text: str, rel_path: str, folder_owner: str, cfg: dict):
    """校验一个 .cs 文件，返回 (meta, errors, warnings)。有错误时 meta 为 None。"""
    errors: list = []
    warnings: list = []

    def err(msg):
        errors.append(f"{rel_path}：{msg}")

    def warn(msg):
        warnings.append(f"{rel_path}：{msg}")

    params, parse_errors = csharp_meta.extract_script_type(text)
    if parse_errors:
        for item in parse_errors:
            err(item)
        return None, errors, warnings

    # 参数值解析不出来时直接报原文，避免后面用「实际是null」这类看不懂的措辞
    for field, (kind, value) in list(params.items()):
        if kind == "unknown":
            if field == "territorys":
                err(f"territorys 必须是 uint 数组字面量（如 [1226, 1228]），原文：{value!r}")
            else:
                err(f"{field} 无法解析，原文：{value!r}")
        elif kind == "identifier":
            if field in ("note", "updateInfo"):
                warn(
                    f"{field} 引用了标识符 {value!r}，但本文件里找不到对应的 const 声明，"
                    f"将按空字符串处理"
                )
                del params[field]
            else:
                err(
                    f"{field} 引用了标识符 {value!r}，但本文件里找不到对应的 const 声明"
                    f"（只解析本文件内的 const）"
                )
    if errors:
        return None, errors, warnings

    # guid：构造函数第一个参数，没有默认值
    guid = ""
    kind, value = params.get("guid", (None, None))
    if kind is None:
        err("缺少 guid 参数")
    elif kind != "string":
        err(f"guid 必须是字符串，实际是{type_name(value)}")
    elif not str(value).strip():
        err("guid 不能为空")
    else:
        guid = str(value).strip()

    # name
    name = NAME_DEFAULT
    kind, value = params.get("name", (None, None))
    if kind is None:
        warn(f"没有写 name，将使用插件默认的 {NAME_DEFAULT!r}")
    elif kind != "string":
        err(f"name 必须是字符串，实际是{type_name(value)}")
    elif not str(value).strip():
        err("name 不能为空")
    else:
        name = str(value)

    # version
    version = VERSION_DEFAULT
    kind, value = params.get("version", (None, None))
    if kind is None:
        warn(f"没有写 version，将使用插件默认的 {VERSION_DEFAULT!r}")
    elif kind != "string":
        err(f"version 必须是字符串，实际是{type_name(value)}")
    elif not VERSION_RE.match(str(value).strip()):
        err(
            f"version {str(value)!r} 不是合法的版本号 "
            f"（插件用 NuGetVersion 解析，如 0.0.1 / 0.0.0.9 / 1.0.0-beta）"
        )
    else:
        version = str(value).strip()

    # author：未写或写成默认值 Unknown 时，回退到文件夹名（即 GitHub 用户名）
    author = ""
    kind, value = params.get("author", (None, None))
    if kind is None:
        author = folder_owner
        warn(f"没有写 author，改用文件夹名 {folder_owner!r}")
    elif kind != "string":
        err(f"author 必须是字符串，实际是{type_name(value)}")
    else:
        candidate = str(value).strip()
        if not candidate or candidate == AUTHOR_DEFAULT:
            author = folder_owner
            warn(
                f"author 是默认值 {candidate or '空'!r}，改用文件夹名 {folder_owner!r}"
            )
        else:
            author = candidate

    # territorys
    territorys: list = []
    kind, value = params.get("territorys", (None, None))
    if kind is None:
        warn("没有写 territorys，该脚本不会按地图过滤")
    elif kind == "null":
        warn("territorys 写成了 null，等同于空数组")
    elif kind != "array":
        err(f"territorys 必须是 uint 数组字面量，实际是{type_name(value)}")
    else:
        for pos, tid in enumerate(value):
            if isinstance(tid, bool) or not isinstance(tid, int) or not 0 <= tid <= UINT_MAX:
                err(f"territorys[{pos}] = {tid!r} 超出 uint 范围（0~{UINT_MAX}）")
            else:
                territorys.append(tid)

    # note / updateInfo
    note = ""
    kind, value = params.get("note", (None, None))
    if kind is not None and kind != "string":
        err(f"note 必须是字符串，实际是{type_name(value)}")
    elif kind == "string":
        note = str(value)

    update_info = ""
    kind, value = params.get("updateInfo", (None, None))
    if kind is not None and kind != "string":
        err(f"updateInfo 必须是字符串，实际是{type_name(value)}")
    elif kind == "string":
        update_info = str(value)

    # 插件用 "{Name}_{Author}.cs" 作为保存文件名
    if name.strip():
        check_filename_component(name, f"{rel_path} 的 name", errors)
    if author.strip():
        check_filename_component(author, f"{rel_path} 的 author", errors)

    if guid and not UUID_RE.match(guid):
        warn(f"guid {guid!r} 不是标准 UUID 格式；插件按字符串处理仍可用，但建议修正")

    if errors:
        return None, errors, warnings

    return {
        "guid": guid,
        "name": name,
        "version": version,
        "author": author,
        "note": note,
        "update_info": update_info,
        "territorys": territorys,
    }, errors, warnings


# --------------------------------------------------------------------------- #
# 生成的 OnlineRepo.json 自检
# --------------------------------------------------------------------------- #


def validate_json_document(text: str, path: str, cfg: dict):
    """校验生成出来的 OnlineRepo.json（OnlineScriptInfo 数组）。"""
    errors: list[str] = []
    warnings: list[str] = []

    if text.startswith("\ufeff"):
        warnings.append(f"{path}：文件带有 UTF-8 BOM，已忽略")
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
    """校验生成结果里的单个条目，并把 guid / name 记入去重表。"""
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
        if field in entry and not isinstance(entry[field], str):
            errors.append(f"{label}：{field} 必须是字符串，实际是{type_name(entry[field])}")

    for field in NON_EMPTY_FIELDS:
        value = entry.get(field)
        if isinstance(value, str) and not value.strip():
            errors.append(f"{label}：{field} 不能为空")

    version = entry.get("Version")
    if isinstance(version, str) and version.strip() and not VERSION_RE.match(version.strip()):
        errors.append(f"{label}：Version {version!r} 不是合法的版本号")

    guid = entry.get("Guid")
    if isinstance(guid, str) and guid.strip():
        if not UUID_RE.match(guid.strip()):
            warnings.append(f"{label}：Guid {guid!r} 不是标准 UUID 格式")
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
                errors.append(f"{label}：TerritoryIds[{pos}] 必须是整数，实际是{type_name(tid)}")
            elif not 0 <= tid <= UINT_MAX:
                errors.append(f"{label}：TerritoryIds[{pos}] = {tid} 超出 uint 范围")

    download = entry.get("DownloadUrl")
    if isinstance(download, str) and download.strip() and not URL_RE.match(download.strip()):
        errors.append(f"{label}：DownloadUrl 必须以 http:// 或 https:// 开头")

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
    """构造 GitHub API 请求头，带 token 时附带 Authorization。"""
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
    """GET 一个 API 路径并解析 JSON。"""
    request = urllib.request.Request(
        f"{API_URL}{path}", headers=_api_headers("application/vnd.github+json")
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
    return json.loads(body) if body else None


def api_raw(path: str) -> bytes:
    """GET 一个 API 路径并取原始字节。"""
    request = urllib.request.Request(
        f"{API_URL}{path}", headers=_api_headers("application/vnd.github.raw")
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def list_changed_files(repo: str, pr_number: str) -> list:
    """分页拉取 PR 改动的文件列表。"""
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
    ref_q = urllib.parse.quote(ref, safe="")
    info = api_json(f"/repos/{repo}/contents/{quoted}?ref={ref_q}")
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
    return api_raw(f"/repos/{repo}/contents/{quoted}?ref={ref_q}"), None


def load_guid_map(repo: str, ref: str):
    """读取已提交的 guid -> 源文件 映射。不存在时返回 None（表示本次无法查重）。"""
    if not ref:
        return None
    quoted = urllib.parse.quote(GUID_MAP_PATH, safe="/")
    try:
        info = api_json(f"/repos/{repo}/contents/{quoted}?ref={urllib.parse.quote(ref, safe='')}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    if not isinstance(info, dict) or not info.get("content"):
        return None
    try:
        data = json.loads(base64.b64decode(info["content"]).decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return {str(key).lower(): str(value) for key, value in data.items()}


def check_guid_collisions(script_guids, own_paths, author, guid_map, cfg):
    """检查 guid 是否与已有脚本或同一 PR 内的其它文件冲突。

    script_guids: [(路径, guid)]
    own_paths:    本次 PR 涉及的全部路径（含重命名前路径），用于放行改名/移动
    guid_map:     已有的 guid -> 源文件；None 表示拿不到、本次不查重
    """
    errors: list[str] = []

    seen: dict[str, str] = {}
    for path, guid in script_guids:
        key = guid.lower()
        if key in seen:
            errors.append(
                f"{path}：guid {guid} 与本次 PR 里的 {seen[key]} 重复；"
                f"同一 guid 只会保留一个脚本"
            )
        else:
            seen[key] = path

    if not guid_map:
        return errors

    for path, guid in script_guids:
        mapped = guid_map.get(guid.lower())
        # mapped in own_paths：作者在改名 / 移动自己的文件，不算冲突
        if not mapped or mapped == path or mapped in own_paths:
            continue
        folder = mapped.split("/")[0] if "/" in mapped else mapped
        same_owner = normalize_login(folder, cfg["username_case_insensitive"]) == normalize_login(
            author, cfg["username_case_insensitive"]
        )
        if same_owner:
            errors.append(
                f"{path}：guid {guid} 已被你自己文件夹里的 {mapped} 使用；"
                f"同一 guid 只会保留一个脚本，请换成互不相同的 guid"
            )
        else:
            errors.append(
                f"{path}：guid {guid} 已被 {mapped} 使用；"
                f"插件把相同 guid 视为同一个脚本，请换成唯一的 guid"
            )
    return errors


# --------------------------------------------------------------------------- #
# 审核主流程
# --------------------------------------------------------------------------- #


def check_change_set(files, author, cfg):
    """路径层面校验。返回 (errors, warnings, rows)。"""
    errors: list[str] = []
    warnings: list[str] = []
    rows: list[tuple[str, str]] = []

    max_files = cfg.get("max_files_per_pr", 100)
    if len(files) > max_files:
        errors.append(f"本 PR 改动了 {len(files)} 个文件，超过上限 {max_files} 个")

    # 文件夹名如果被 ignore_dirs 排除，PR 通过也永远不会被收录，提前说清楚
    ignore_dirs = {normalize_login(str(d), True) for d in (cfg.get("ignore_dirs") or [])}
    if normalize_login(author, True) in ignore_dirs:
        errors.append(
            f"你的文件夹 {author}/ 在 ignore_dirs 排除名单里，不会被收录；请联系维护者"
        )

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
    """生成报告正文：改动清单 + 错误 + 提醒。"""
    lines = ["### 改动清单", "", "| 状态 | 文件 |", "| --- | --- |"]
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
        "> 规则：只能修改以自己 GitHub 用户名命名的文件夹，只能新增 / 修改 / 删除 `.cs`，"
        "每个 `.cs` 必须包含且只包含一处 `[ScriptType(...)]`。详见 `README.md`。"
    )
    return "\n".join(lines)


def build_report(title, rows, errors, warnings):
    """拼出完整的 PR 评论内容。"""
    return "\n".join([f"## 🤖 PR 自动审核：{title}", "", describe(rows, errors, warnings)])


def write_step_summary(markdown: str):
    """追加到 Actions 运行摘要；不在 Actions 环境下静默跳过。"""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as fh:
                fh.write(markdown + "\n")
        except OSError:
            pass


def post_comment(repo: str, pr_number: str, body: str):
    """在 PR 下发一条评论。"""
    request = urllib.request.Request(
        f"{API_URL}/repos/{repo}/issues/{pr_number}/comments",
        data=json.dumps({"body": body}).encode("utf-8"),
        headers=_api_headers("application/vnd.github+json"),
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()


def post_comment_safely(repo: str, pr_number: str, body: str):
    """在 PR 下留言；失败只告警，不影响审核结论。"""
    try:
        post_comment(repo, pr_number, body)
    except Exception as exc:  # noqa: BLE001
        print(f"::warning::无法在 PR 下留言：{exc}")


def run_ci() -> int:
    """CI 入口：审核 PR 并输出报告。返回 0 通过 / 1 未通过 / 2 无法执行。"""
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
        except Exception:  # noqa: BLE001
            pass
        return 2

    script_guids: list[tuple[str, str]] = []
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

            folder_owner = path.split("/")[0] if "/" in path else author
            meta, file_errors, file_warnings = validate_script_file(
                text, path, folder_owner, cfg
            )
            errors.extend(file_errors)
            warnings.extend(file_warnings)
            if meta:
                script_guids.append((path, meta["guid"]))

        # guid 查重：拿仓库里已提交的映射表比对（改名 / 移动自己的文件不算冲突）
        own_paths = {
            item[key]
            for item in files
            for key in ("filename", "previous_filename")
            if item.get(key)
        }

        base_ref = os.environ.get("PR_BASE_REF") or os.environ.get("PR_BASE_SHA", "")
        guid_map = None
        try:
            guid_map = load_guid_map(repo, base_ref)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"::warning::无法读取 guid 映射表，本次跳过 guid 查重：{exc}")
        if guid_map is None and base_ref:
            print(f"::notice::{GUID_MAP_PATH} 尚不存在，本次跳过 guid 查重")
        errors.extend(check_guid_collisions(script_guids, own_paths, author, guid_map, cfg))

    if errors:
        report = build_report("❌ 未通过", rows, errors, warnings)
        print(report)
        write_step_summary(report)
        post_comment_safely(repo, pr_number, report)
        return 1

    report = build_report("✅ 通过", rows, errors, warnings)
    print(report)
    write_step_summary(report)
    if warnings:
        post_comment_safely(repo, pr_number, report + "\n校验通过，将自动 squash 合并。")
    return 0


# --------------------------------------------------------------------------- #
# 本地子命令
# --------------------------------------------------------------------------- #


def read_utf8(path):
    """读取文件并解码为 UTF-8，返回 (文本, 退出码)；读取或解码失败时文本为 None。"""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        print(f"{path}：无法读取 —— {exc}")
        return None, 2
    try:
        return data.decode("utf-8"), 0
    except UnicodeDecodeError as exc:
        print(f"{path}：不是合法的 UTF-8 编码（{exc}）")
        return None, 1


def cmd_check_json(paths, cfg) -> int:
    """校验生成的 OnlineRepo.json。"""
    exit_code = 0
    for path in paths:
        text, code = read_utf8(path)
        if text is None:
            exit_code = code
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


def infer_owner(path: str) -> str:
    """本地校验时推断文件夹名（即 author 回退用的值）。

    优先按相对仓库根目录的第一层目录判断；文件在仓库外时退化为其父目录名。
    """
    absolute = os.path.abspath(path)
    try:
        rel = os.path.relpath(absolute, REPO_ROOT).replace("\\", "/")
    except ValueError:
        rel = ""
    if rel and not rel.startswith(".."):
        parts = rel.split("/")
        if len(parts) >= 2:
            return parts[0]
    return os.path.basename(os.path.dirname(absolute)) or "unknown"


def cmd_check_cs(paths, owner, cfg) -> int:
    """校验本地 .cs 文件；owner 决定 author 缺失时回退成什么。"""
    exit_code = 0
    for path in paths:
        text, code = read_utf8(path)
        if text is None:
            exit_code = code
            continue
        meta, errors, warnings = validate_script_file(text, path, owner or infer_owner(path), cfg)
        for item in warnings:
            print(f"⚠️  {item}")
        for item in errors:
            print(f"❌ {item}")
        if errors:
            exit_code = 1
        else:
            print(
                f"✅ {path} 通过：Name={meta['name']!r} Guid={meta['guid']!r} "
                f"Version={meta['version']!r} Author={meta['author']!r} "
                f"TerritoryIds={meta['territorys']}"
            )
    return exit_code


def cmd_path_check(author, paths, cfg) -> int:
    """校验路径规则。"""
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


def main() -> int:
    """命令行入口：无子命令时进入 CI 审核模式。"""
    parser = argparse.ArgumentParser(description="KodakkuAssistScriptLibrary PR 自动审核")
    parser.add_argument("--check-cs", nargs="+", metavar="FILE", help="校验本地 .cs 文件")
    parser.add_argument("--check-json", nargs="+", metavar="FILE", help="校验生成的 OnlineRepo.json")
    parser.add_argument("--owner", help="配合 --check-cs：文件夹名（author 回退用）")
    parser.add_argument("--path-check", metavar="AUTHOR", help="校验路径规则（配合 --path）")
    parser.add_argument("--path", action="append", default=[], help="待校验路径，可重复")
    args = parser.parse_args()

    cfg = load_config()

    if args.check_cs:
        return cmd_check_cs(args.check_cs, args.owner, cfg)
    if args.check_json:
        return cmd_check_json(args.check_json, cfg)
    if args.path_check:
        if not args.path:
            parser.error("--path-check 需要至少一个 --path")
        return cmd_path_check(args.path_check, args.path, cfg)
    return run_ci()


if __name__ == "__main__":
    sys.exit(main())
