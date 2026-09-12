#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 C# 源码里找到 [ScriptType(...)] 并解析它的参数。

只用标准库。本模块只做「语法层」的事：定位特性、切分参数、还原字符串/数组字面量；
某个字段是否符合业务规则，由 pr_review.validate_script_file 判断。

为什么要手写而不是用正则：特性的参数支持具名实参且顺序任意、可以跨多行，
字符串里还可能出现 ")"、"," 甚至 "]"（例如 note 里写说明），正则很容易切错。

支持写法：
    [ScriptType(name: "M1s绘图", territorys: [1226], guid: "...", author: "Karlin")]
    [ScriptTypeAttribute("guid", "Name", new uint[] { 1226 }, "0.0.1", "Author")]
    [ScriptType(guid: "g", note: @"逐字""字符串", author: "A")]
"""

from __future__ import annotations

import re

#: ScriptTypeAttribute 构造函数的参数顺序（见 Interface/ScriptAttribute.cs）
PARAM_ORDER = ("guid", "name", "territorys", "version", "author", "note", "updateInfo")

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


# --------------------------------------------------------------------------- #
# 词法：屏蔽注释、跳过字符串
# --------------------------------------------------------------------------- #


def _copy_plain(text: str, i: int, out: list) -> int:
    """从普通字符串字面量的开引号开始复制，返回结束后的下标。"""
    out.append('"')
    i += 1
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            out.append(text[i:i + 2])
            i += 2
            continue
        out.append(ch)
        i += 1
        if ch == '"':
            break
    return i


def _copy_verbatim(text: str, i: int, out: list) -> int:
    """从 @" 开始复制逐字字符串，内部 "" 表示一个引号。"""
    out.append('@"')
    i += 2
    n = len(text)
    while i < n:
        if text[i] == '"':
            if i + 1 < n and text[i + 1] == '"':
                out.append('""')
                i += 2
                continue
            out.append('"')
            i += 1
            break
        out.append(text[i])
        i += 1
    return i


def _copy_char(text: str, i: int, out: list) -> int:
    """从单引号开始复制字符字面量。"""
    out.append("'")
    i += 1
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            out.append(text[i:i + 2])
            i += 2
            continue
        out.append(ch)
        i += 1
        if ch == "'":
            break
    return i


def _copy_raw(text: str, i: int, out: list) -> int:
    """从三引号及以上的原始字符串字面量开始复制。"""
    n = len(text)
    j = i
    while j < n and text[j] == '"':
        j += 1
    quotes = '"' * (j - i)
    end = text.find(quotes, j)
    if end == -1:
        out.append(text[i:])
        return n
    out.append(text[i:end + len(quotes)])
    return end + len(quotes)


def _skip(text: str, i: int) -> int:
    """跳过当前位置的字面量（字符串/字符），返回结束后的下标；i 不在字面量上则原样返回。"""
    scratch: list = []
    ch = text[i]
    if ch == "@" and i + 1 < len(text) and text[i + 1] == '"':
        return _copy_verbatim(text, i, scratch)
    if ch == '"':
        if text.startswith('"""', i):
            return _copy_raw(text, i, scratch)
        return _copy_plain(text, i, scratch)
    if ch == "'":
        return _copy_char(text, i, scratch)
    return i


def strip_comments(text: str) -> str:
    """把注释字符替换成空格（保留换行），字符串字面量原样保留。"""
    out: list = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        if ch == "/" and nxt == "/":
            while i < n and text[i] != "\n":
                out.append(" ")
                i += 1
            continue

        if ch == "/" and nxt == "*":
            out.append("  ")
            i += 2
            while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                out.append("\n" if text[i] == "\n" else " ")
                i += 1
            if i < n:
                out.append("  ")
                i += 2
            continue

        if ch == "@" and nxt == '"':
            i = _copy_verbatim(text, i, out)
            continue

        if ch == '"':
            i = _copy_raw(text, i, out) if text.startswith('"""', i) else _copy_plain(text, i, out)
            continue

        if ch == "'":
            i = _copy_char(text, i, out)
            continue

        out.append(ch)
        i += 1

    return "".join(out)


# --------------------------------------------------------------------------- #
# 定位 [ScriptType(...)]
# --------------------------------------------------------------------------- #


def _match_paren(text: str, open_index: int):
    """返回与 open_index 处 '(' 配对的 ')' 下标；不配对则返回 None。"""
    depth = 0
    i, n = open_index, len(text)
    while i < n:
        ch = text[i]
        if ch in '"\'' or (ch == "@" and i + 1 < n and text[i + 1] == '"'):
            i = _skip(text, i)
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def find_arglists(text: str):
    """返回 (arglists, errors)。

    arglists 是每处 [ScriptType(...)] 括号内的原文；写成 [ScriptType] 无参数时该元素为 None。
    text 需要是 strip_comments 之后的内容。
    """
    arglists: list = []
    errors: list = []
    i, n = 0, len(text)

    while i < n:
        ch = text[i]

        if ch in '"\'' or (ch == "@" and i + 1 < n and text[i + 1] == '"'):
            i = _skip(text, i)
            continue

        if ch != "[":
            i += 1
            continue

        start = i + 1
        match = _IDENT.match(text, start)
        if not match:
            i += 1
            continue

        end = match.end()
        while end < n and text[end] == ".":
            nxt = _IDENT.match(text, end + 1)
            if not nxt:
                break
            end = nxt.end()

        name = text[start:end].split(".")[-1]
        if name not in ("ScriptType", "ScriptTypeAttribute"):
            i += 1
            continue

        # 排除 ScriptTypeHelper 这类同前缀的长标识符
        trailing = _IDENT.match(text, end)
        if trailing:
            i = trailing.end()
            continue

        cursor = end
        while cursor < n and text[cursor] in " \t\r\n":
            cursor += 1

        if cursor < n and text[cursor] == "(":
            close = _match_paren(text, cursor)
            if close is None:
                errors.append("特性的括号不匹配")
                return arglists, errors
            arglists.append(text[cursor + 1:close])
            i = close + 1
            continue

        if cursor < n and text[cursor] == "]":
            arglists.append(None)
            i = cursor + 1
            continue

        i += 1

    return arglists, errors


# --------------------------------------------------------------------------- #
# 切分参数、还原字面量
# --------------------------------------------------------------------------- #


def _split_top_level(text: str, separator: str) -> list:
    parts: list = []
    depth = 0
    current: list = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in '"\'' or (ch == "@" and i + 1 < n and text[i + 1] == '"'):
            end = _skip(text, i)
            current.append(text[i:end])
            i = end
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == separator and depth == 0:
            parts.append("".join(current))
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    parts.append("".join(current))
    return parts


def _find_named_colon(text: str):
    """返回顶层具名实参的 ':' 下标；没有则返回 None。"""
    depth = 0
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in '"\'' or (ch == "@" and i + 1 < n and text[i + 1] == '"'):
            i = _skip(text, i)
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == ":" and depth == 0:
            if i + 1 < n and text[i + 1] == ":":  # 跳过 ::
                i += 2
                continue
            return i
        i += 1
    return None


def _decode_raw_string(body: str) -> str:
    """按 C# 原始字符串字面量的语义还原内容：去掉首行换行，并按结束分隔符的缩进左对齐。"""
    text = body.replace("\r\n", "\n").replace("\r", "\n")
    if text.startswith("\n"):
        text = text[1:]
    lines = text.split("\n")
    indent = lines[-1]
    if indent.strip() == "":
        lines = lines[:-1]
        lines = [ln[len(indent):] if ln.startswith(indent) else ln for ln in lines]
    return "\n".join(lines)


def _decode_string(text: str):
    """把整串字符串字面量还原成 Python 字符串；不是单个字面量则返回 None。"""
    s = text.strip()
    if not s:
        return None

    if s.startswith('@"'):
        if not s.endswith('"') or len(s) < 3:
            return None
        return s[2:-1].replace('""', '"')

    if s.startswith('"""'):
        quotes = 0
        while quotes < len(s) and s[quotes] == '"':
            quotes += 1
        closing = '"' * quotes
        if not s.endswith(closing) or len(s) < quotes * 2:
            return None
        return _decode_raw_string(s[quotes:len(s) - quotes])

    if not s.startswith('"') or not s.endswith('"') or len(s) < 2:
        return None

    body = s[1:-1]
    out: list = []
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= n:
            return None
        esc = body[i]
        simple = {"n": "\n", "r": "\r", "t": "\t", "0": "\0", "a": "\a",
                  "b": "\b", "f": "\f", "v": "\v", "\\": "\\", '"': '"', "'": "'"}
        if esc in simple:
            out.append(simple[esc])
            i += 1
            continue
        if esc in "uUx":
            width = {"u": 4, "x": 2, "U": 8}[esc]
            digits = body[i + 1:i + 1 + width]
            if len(digits) != width or not re.fullmatch(r"[0-9a-fA-F]+", digits):
                return None
            out.append(chr(int(digits, 16)))
            i += 1 + width
            continue
        return None
    return "".join(out)


def _read_literal(text: str, i: int):
    """从 i 开始读一个字面量的原文，返回 (原文, 结束下标)；读不到则返回 (None, i)。"""
    if i >= len(text):
        return None, i
    if text[i] == "@" and i + 1 < len(text) and text[i + 1] == '"':
        scratch: list = []
        end = _copy_verbatim(text, i, scratch)
        return "".join(scratch), end
    if text[i] == '"':
        scratch = []
        end = _copy_raw(text, i, scratch) if text.startswith('"""', i) else _copy_plain(text, i, scratch)
        return "".join(scratch), end
    match = re.match(r"-?(?:0[xX][0-9a-fA-F]+|\d+)[uUlL]*", text[i:])
    if match:
        return match.group(0), i + match.end()
    return None, i


def _read_const_expression(text: str, i: int):
    """读取 const 的初值：单个字面量，或由 + 连接的多个字面量。"""
    values: list = []
    kinds: list = []
    cursor, n = i, len(text)

    while True:
        while cursor < n and text[cursor] in " \t\r\n":
            cursor += 1
        literal, end = _read_literal(text, cursor)
        if literal is None:
            return None
        kind, decoded = decode_value(literal)
        if kind in ("unknown", "null", "identifier"):
            return None
        values.append(decoded)
        kinds.append(kind)
        cursor = end
        while cursor < n and text[cursor] in " \t\r\n":
            cursor += 1
        if cursor < n and text[cursor] == "+":
            cursor += 1
            continue
        break

    if len(values) == 1:
        return kinds[0], values[0]
    if all(kind == "string" for kind in kinds):
        return "string", "".join(values)
    return None


#: 匹配 const 声明，例如 `    private const string noteStr =`
_CONST_DECL = re.compile(r"\bconst\s+([A-Za-z_][\w.]*(?:\s*<[^;=]*>)?(?:\s*\[\s*\])?)\s+([A-Za-z_]\w*)\s*=\s*")


def collect_constants(text: str) -> dict:
    """收集文件里的 const 声明，供解析特性里的标识符实参使用。

    真实脚本常见写法是先把长文本放进 const，再在特性里引用：

        [ScriptType(..., updateInfo: updateInfoStr)]
        public class X {
            const string updateInfoStr =
                \"\"\"
                第一行
                第二行
                \"\"\";
        }

    注意 const 可能声明在特性之后，所以要先扫全文件再解析特性。
    """
    table: dict = {}
    for match in _CONST_DECL.finditer(text):
        type_text = match.group(1).replace(" ", "")
        name = match.group(2)
        parsed = _read_const_expression(text, match.end())
        if parsed is None:
            continue
        kind, value = parsed
        if kind == "string" and type_text != "string":
            continue
        if kind == "array" and not type_text.endswith("[]"):
            continue
        table[name] = (kind, value)
    return table


def _decode_int_array(text: str):
    """还原 uint 数组字面量；不是数组或元素不是整数字面量则返回 None。"""
    s = text.strip()

    if s.startswith("Array.Empty"):
        return []
    if s == "null":
        return None

    inner = None
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1]
    else:
        match = re.fullmatch(r"new\s+(?:[A-Za-z_][\w.]*\s*)?\[\s*\]\s*\{(.*)\}", s, re.S)
        if not match:
            match = re.fullmatch(r"new\s*\[\s*\]\s*\{(.*)\}", s, re.S)
        if match:
            inner = match.group(1)

    if inner is None:
        return None

    if not inner.strip():
        return []

    values = []
    for part in _split_top_level(inner, ","):
        token = part.strip()
        if not token:
            continue
        # 允许负号：负数虽然对 uint 非法，但先解析出来才能给出「超出 uint 范围」这种精确提示
        if not re.fullmatch(r"-?(?:0[xX][0-9a-fA-F]+|\d+)[uUlL]*", token):
            return None
        token = re.sub(r"[uUlL]+$", "", token)
        negative = token.startswith("-")
        token = token.lstrip("-")
        value = int(token, 16) if token.lower().startswith("0x") else int(token)
        values.append(-value if negative else value)
    return values


def decode_value(raw: str, constants: dict | None = None):
    """返回 (kind, value)，kind ∈ string / int / array / null / identifier / unknown。

    kind 为 identifier 时 value 是标识符名（本文件里没有对应的 const 声明）；
    kind 为 unknown 时 value 是该参数值的原文，便于报错时回显给贡献者。
    """
    s = raw.strip()
    if not s:
        return "unknown", s
    if s in ("null", "default"):
        return "null", None

    if s.startswith('@"') or s.startswith('"'):
        value = _decode_string(s)
        if value is not None:
            return "string", value
        return "unknown", s

    if re.fullmatch(r"-?(?:0[xX][0-9a-fA-F]+|\d+)[uUlL]*", s):
        token = re.sub(r"[uUlL]+$", "", s)
        negative = token.startswith("-")
        token = token.lstrip("-")
        value = int(token, 16) if token.lower().startswith("0x") else int(token)
        return "int", (-value if negative else value)

    array = _decode_int_array(s)
    if array is not None:
        return "array", array

    if re.fullmatch(r"[A-Za-z_]\w*", s):
        if constants and s in constants:
            return constants[s]
        return "identifier", s

    return "unknown", s


def parse_arglist(arglist: str, constants: dict | None = None):
    """把括号内的原文切成 {参数名: (kind, value)}；返回 (params, errors)。"""
    params: dict = {}
    errors: list = []
    positional_index = 0

    for part in _split_top_level(arglist, ","):
        token = part.strip()
        if not token:
            continue

        colon = _find_named_colon(token)
        if colon is not None:
            key = token[:colon].strip()
            if not _IDENT.fullmatch(key):
                errors.append(f"无法识别的具名参数：{token[:60]!r}")
                continue
            if key not in PARAM_ORDER:
                errors.append(f"无法识别的具名参数 {key!r}；可用参数：{', '.join(PARAM_ORDER)}")
                continue
            params[key] = decode_value(token[colon + 1:], constants)
            continue

        if positional_index >= len(PARAM_ORDER):
            errors.append(f"位置参数过多（最多 {len(PARAM_ORDER)} 个）")
            break
        name = PARAM_ORDER[positional_index]
        positional_index += 1
        if name in params:
            errors.append(f"参数 {name} 被重复指定")
            continue
        params[name] = decode_value(token, constants)

    return params, errors


def extract_script_type(text: str):
    """解析源码里的 ScriptType 特性，返回 (params, errors)。

    params: {参数名: (kind, value)}，未出现的参数不在其中。
    """
    stripped = strip_comments(text)
    if not stripped.strip():
        return {}, ["文件内容为空"]
    constants = collect_constants(stripped)
    arglists, errors = find_arglists(stripped)
    if errors:
        return {}, errors
    if not arglists:
        return {}, ["没有找到 [ScriptType(...)] 特性"]
    if len(arglists) > 1:
        return {}, [f"找到 {len(arglists)} 处 [ScriptType(...)]，要求每个 .cs 文件只能有一处"]
    if arglists[0] is None:
        return {}, ["[ScriptType] 缺少参数列表"]
    return parse_arglist(arglists[0], constants)
