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

文件结构：常量 -> 字面量读取 -> 注释屏蔽 -> 定位特性 -> 切分参数 -> 还原取值 -> 对外入口。
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: ScriptTypeAttribute 构造函数的参数顺序（见 Interface/ScriptAttribute.cs）
PARAM_ORDER = ("guid", "name", "territorys", "version", "author", "note", "updateInfo")

#: 能出现在特性名末尾的两种写法
ATTRIBUTE_NAMES = ("ScriptType", "ScriptTypeAttribute")

#: 简单转义序列，以及 \u / \x / \U 的位数
SIMPLE_ESCAPES = {
    "n": "\n", "r": "\r", "t": "\t", "0": "\0", "a": "\a",
    "b": "\b", "f": "\f", "v": "\v", "\\": "\\", '"': '"', "'": "'",
}
ESCAPE_WIDTHS = {"u": 4, "x": 2, "U": 8}

IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
INT_LITERAL_RE = re.compile(r"-?(?:0[xX][0-9a-fA-F]+|\d+)[uUlL]*")
HEX_RE = re.compile(r"[0-9a-fA-F]+")
INT_SUFFIX_RE = re.compile(r"[uUlL]+$")

#: 匹配 const 声明，例如 `    private const string noteStr =`
CONST_DECL_RE = re.compile(
    r"\bconst\s+([A-Za-z_][\w.]*(?:\s*<[^;=]*>)?(?:\s*\[\s*\])?)\s+([A-Za-z_]\w*)\s*=\s*"
)

#: 字符串 / uint 数组字面量在 C# 里的几种写法
NEW_ARRAY_RE = re.compile(r"new\s+(?:[A-Za-z_][\w.]*\s*)?\[\s*\]\s*\{(.*)\}", re.S)
NEW_IMPLICIT_ARRAY_RE = re.compile(r"new\s*\[\s*\]\s*\{(.*)\}", re.S)


# --------------------------------------------------------------------------- #
# 字面量读取：原样复制 / 跳过
# --------------------------------------------------------------------------- #


def _copy_quoted(text: str, i: int, out: list, quote: str) -> int:
    """复制被 quote 包裹的字面量（"..." 或 '...'，支持 \\ 转义），返回结束后的下标。"""
    out.append(quote)
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
        if ch == quote:
            break
    return i


def _copy_verbatim_body(text: str, i: int, out: list) -> int:
    """从开引号处复制逐字字符串的内容（"" 表示一个引号），返回结束下标。"""
    out.append('"')
    i += 1
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


def _copy_interpolated(text: str, i: int, out: list):
    """复制 $ / @ 前缀字符串的原文（插值串、逐字串、插值原始串），返回结束下标。

    前缀一起复制进去，插值与转义语义交给 _decode_string 判断。
    不是字符串字面量（例如单独的 $ 或 @）时返回 None，且不修改 out。
    """
    j = i
    while j < len(text) and text[j] in "$@":
        j += 1
    if j == i or j >= len(text) or text[j] != '"':
        return None

    out.append(text[i:j])
    if text.startswith('"""', j):
        return _copy_raw(text, j, out)
    if "@" in text[i:j]:
        return _copy_verbatim_body(text, j, out)
    return _copy_quoted(text, j, out, '"')


def _copy_raw(text: str, i: int, out: list) -> int:
    """复制三引号及以上的原始字符串字面量。"""
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


def _read_literal(text: str, i: int):
    """从 i 开始读一个字面量的原文，返回 (原文, 结束下标)；读不到则返回 (None, i)。

    支持普通字符串、@ 逐字字符串、$ 插值字符串（含插值原始字符串）、三引号原始字符串、
    单引号字符字面量、整数字面量。
    """
    if i >= len(text):
        return None, i

    if text[i] in "$@":
        scratch: list = []
        end = _copy_interpolated(text, i, scratch)
        if end is not None:
            return "".join(scratch), end

    if text[i] in '"\'':
        scratch = []
        if text[i] == '"':
            end = _copy_raw(text, i, scratch) if text.startswith('"""', i) else _copy_quoted(
                text, i, scratch, '"'
            )
        else:
            end = _copy_quoted(text, i, scratch, "'")
        return "".join(scratch), end

    match = INT_LITERAL_RE.match(text[i:])
    if match:
        return match.group(0), i + match.end()
    return None, i


def _skip(text: str, i: int) -> int:
    """跳过当前位置的字面量，返回结束后的下标；i 不在字面量上则原样返回。"""
    literal, end = _read_literal(text, i)
    return end if literal is not None else i


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

        if ch in "$@":
            end = _copy_interpolated(text, i, out)
            if end is not None:
                i = end
                continue

        if ch in '"\'':
            i = _copy_raw(text, i, out) if text.startswith('"""', i) else _copy_quoted(text, i, out, ch)
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
        match = IDENT_RE.match(text, start)
        if not match:
            i += 1
            continue

        end = match.end()
        while end < n and text[end] == ".":
            nxt = IDENT_RE.match(text, end + 1)
            if not nxt:
                break
            end = nxt.end()

        if text[start:end].split(".")[-1] not in ATTRIBUTE_NAMES:
            i += 1
            continue

        # 排除 ScriptTypeHelper 这类同前缀的长标识符
        trailing = IDENT_RE.match(text, end)
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
# 切分参数
# --------------------------------------------------------------------------- #


def _split_top_level(text: str, separator: str) -> list:
    """按 separator 切分，忽略括号内与字符串内的分隔符。"""
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


# --------------------------------------------------------------------------- #
# 还原取值
# --------------------------------------------------------------------------- #


def _parse_int_literal(token: str):
    """解析 C# 整数字面量（负号 / 十六进制 / uUlL 后缀）；不是整数字面量则返回 None。"""
    if not INT_LITERAL_RE.fullmatch(token):
        return None
    body = INT_SUFFIX_RE.sub("", token)
    negative = body.startswith("-")
    body = body.lstrip("-")
    value = int(body, 16) if body.lower().startswith("0x") else int(body)
    return -value if negative else value


def _decode_raw_string(body: str) -> str:
    """按 C# 原始字符串字面量的语义还原内容：去掉首行换行，并按结束分隔符的缩进左对齐。"""
    content = body.replace("\r\n", "\n").replace("\r", "\n")
    if content.startswith("\n"):
        content = content[1:]
    lines = content.split("\n")
    indent = lines[-1]
    if indent.strip() == "":
        lines = [ln[len(indent):] if ln.startswith(indent) else ln for ln in lines[:-1]]
    return "\n".join(lines)


def _decode_escapes(body: str):
    """还原普通字符串里的转义序列；遇到不认识的转义返回 None。"""
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
        if esc in SIMPLE_ESCAPES:
            out.append(SIMPLE_ESCAPES[esc])
            i += 1
            continue
        if esc in ESCAPE_WIDTHS:
            width = ESCAPE_WIDTHS[esc]
            digits = body[i + 1:i + 1 + width]
            if len(digits) != width or not HEX_RE.fullmatch(digits):
                return None
            out.append(chr(int(digits, 16)))
            i += 1 + width
            continue
        return None
    return "".join(out)


def _split_string_prefix(s: str):
    """拆出字符串字面量的 $ / @ 前缀，返回 (前缀, 余下部分)。"""
    i = 0
    while i < len(s) and s[i] in "$@":
        i += 1
    return s[:i], s[i:]


def _resolve_hole(expression: str, constants: dict | None) -> str:
    """把插值 {标识符} 换成同文件里对应 const 的字符串值；换不了就原样保留。"""
    name = expression.strip()
    if constants and IDENT_RE.fullmatch(name):
        entry = constants.get(name)
        if entry and entry[0] == "string":
            return entry[1]
    return "{" + expression + "}"


def _decode_interpolation(content: str, dollars: int, constants: dict | None) -> str:
    """处理插值字符串的花括号：连续两组花括号表示字面花括号，{标识符} 尝试取值替换。

    C# 用 $ 的个数决定插值定界符：一个 $ 时是 {expr}，两个 $ 时则是 {{expr}}。
    这里只认「单个标识符」的插值——复杂表达式原样留在文本里，总好过整段 note 丢失。
    """
    if dollars <= 0:
        return content

    open_brace = "{" * dollars
    close_brace = "}" * dollars
    out: list = []
    i, n = 0, len(content)
    while i < n:
        if content.startswith(open_brace * 2, i):
            out.append(open_brace)
            i += len(open_brace) * 2
            continue
        if content.startswith(close_brace * 2, i):
            out.append(close_brace)
            i += len(close_brace) * 2
            continue
        if content.startswith(open_brace, i):
            close = content.find(close_brace, i + len(open_brace))
            if close == -1:
                out.append(content[i:])
                break
            out.append(_resolve_hole(content[i + len(open_brace):close], constants))
            i = close + len(close_brace)
            continue
        out.append(content[i])
        i += 1
    return "".join(out)


def _decode_string(text: str, constants: dict | None = None):
    """把整串字符串字面量还原成 Python 字符串；不是单个字面量则返回 None。

    支持 @ 逐字字符串、$ 插值字符串、三引号原始字符串（含插值原始字符串）。
    传入 constants 时，插值里的 {标识符} 会尝试用同文件 const 的值替换；替换不了的原样
    保留（形如 {Version}），避免整段 note 因为含插值就被判成「解析不了」而丢弃。
    """
    s = text.strip()
    if not s:
        return None

    prefix, rest = _split_string_prefix(s)
    if not rest.startswith('"'):
        return None
    dollars = prefix.count("$")
    verbatim = "@" in prefix

    if rest.startswith('"""'):
        quotes = 0
        while quotes < len(rest) and rest[quotes] == '"':
            quotes += 1
        closing = '"' * quotes
        if not rest.endswith(closing) or len(rest) < quotes * 2:
            return None
        content = _decode_raw_string(rest[quotes:len(rest) - quotes])
        return content if dollars == 0 else _decode_interpolation(content, dollars, constants)

    if not rest.endswith('"') or len(rest) < 2:
        return None

    if verbatim:
        content = rest[1:-1].replace('""', '"')
    else:
        content = _decode_escapes(rest[1:-1])
        if content is None:
            return None
    return content if dollars == 0 else _decode_interpolation(content, dollars, constants)


def _decode_int_array(text: str):
    """还原 uint 数组字面量；不是数组或元素不是整数字面量则返回 None。"""
    s = text.strip()

    if s.startswith("Array.Empty"):
        return []
    if s == "null":
        return None

    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1]
    else:
        match = NEW_ARRAY_RE.fullmatch(s) or NEW_IMPLICIT_ARRAY_RE.fullmatch(s)
        if not match:
            return None
        inner = match.group(1)

    if not inner.strip():
        return []

    values = []
    for part in _split_top_level(inner, ","):
        token = part.strip()
        if not token:
            continue
        # 负数虽然对 uint 非法，但先解析出来才能给出「超出 uint 范围」这种精确提示
        value = _parse_int_literal(token)
        if value is None:
            return None
        values.append(value)
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

    # 字面量拼接：note: "第一行" + "第二行" / note: $"v{Version}" + NoteTail
    parts = _split_top_level(s, "+")
    if len(parts) > 1:
        pieces: list = []
        for part in parts:
            kind, value = decode_value(part, constants)
            if kind != "string":
                return "unknown", s
            pieces.append(value)
        return "string", "".join(pieces)

    _, rest = _split_string_prefix(s)
    if rest.startswith('"'):
        value = _decode_string(s, constants)
        return ("string", value) if value is not None else ("unknown", s)

    number = _parse_int_literal(s)
    if number is not None:
        return "int", number

    array = _decode_int_array(s)
    if array is not None:
        return "array", array

    if IDENT_RE.fullmatch(s):
        if constants and s in constants:
            return constants[s]
        return "identifier", s

    return "unknown", s


# --------------------------------------------------------------------------- #
# const 声明（特性里常用标识符引用长文本）
# --------------------------------------------------------------------------- #


def _read_const_expression(text: str, i: int, constants: dict | None = None):
    """读取 const 的初值：字面量、同文件里的另一个 const，或由 + 连接的若干项。"""
    values: list = []
    kinds: list = []
    cursor, n = i, len(text)

    while True:
        while cursor < n and text[cursor] in " \t\r\n":
            cursor += 1
        literal, end = _read_literal(text, cursor)
        if literal is None:
            # const string UpdateInfo = UpdateStr;  —— 引用同文件里的另一个 const
            reference = IDENT_RE.match(text, cursor)
            entry = constants.get(reference.group(0)) if reference and constants else None
            if entry is None:
                return None
            kind, decoded = entry
            end = reference.end()
        else:
            kind, decoded = decode_value(literal, constants)
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


def collect_constants(text: str) -> dict:
    """收集文件里的 const 声明，供解析特性里的标识符实参使用。

    真实脚本常见写法是先把长文本放进 const，再在特性里引用：

        [ScriptType(..., updateInfo: updateInfoStr)]
        public class X {
            const string updateInfoStr = <插值原始字符串>;
        }

    注意 const 可能声明在特性之后，也可能引用另一个 const（const string A = B;）或用
    插值（形如 $"v{Version}"），所以这里反复解析直到取值不再变化；解不出来的环引用被丢弃。
    """
    decls = [
        (match.group(2), match.group(1).replace(" ", ""), match.end())
        for match in CONST_DECL_RE.finditer(text)
    ]

    table: dict = {}
    for _ in range(len(decls) + 1):
        changed = False
        for name, type_text, start in decls:
            result = _read_const_expression(text, start, table)
            if result is None:
                continue
            kind, value = result
            if kind == "string" and type_text != "string":
                continue
            if kind == "array" and not type_text.endswith("[]"):
                continue
            if table.get(name) != (kind, value):
                table[name] = (kind, value)
                changed = True
        if not changed:
            break
    return table


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #


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
            if not IDENT_RE.fullmatch(key):
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

    arglists, errors = find_arglists(stripped)
    if errors:
        return {}, errors
    if not arglists:
        return {}, ["没有找到 [ScriptType(...)] 特性"]
    if len(arglists) > 1:
        return {}, [f"找到 {len(arglists)} 处 [ScriptType(...)]，要求每个 .cs 文件只能有一处"]
    if arglists[0] is None:
        return {}, ["[ScriptType] 缺少参数列表"]

    return parse_arglist(arglists[0], collect_constants(stripped))
