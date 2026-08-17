"""SQL 词法分析器。

支持:
- 标识符与关键字(大小写不敏感,内部统一转大写比较)
- 字符串字面量(单引号,'' 转义)
- blob 字面量(X'ABCD' / x'ab')
- 整数/浮点字面量(含科学计数法)
- 带引号标识符("name")
- 运算符与标点(含 || 与 %)
- `--` 行注释

本模块是纯标准库实现,不引入任何第三方依赖。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Union

# 运算符/标点:多字符优先匹配
SYMBOLS = (
    "<=",
    ">=",
    "!=",
    "<>",
    "||",
    "(",
    ")",
    ",",
    ";",
    "=",
    "<",
    ">",
    "+",
    "-",
    "*",
    "/",
    "%",
    ".",
)

# 数值字面量的解析结果
Number = Union[int, float]


class SqlLexError(Exception):
    """词法错误:无法识别的字符。"""


@dataclass(frozen=True)
class Token:
    kind: str  # 'ident' | 'string' | 'blob' | 'number' | 'symbol'
    text: str  # 原始文本(ident 统一大写)
    value: Optional[Union[str, Number, bytes]] = None  # string/blob/number 的字面值
    pos: int = 0  # 在源文本中的起始偏移


def _scan_quoted(sql: str, i: int, quote: str) -> tuple:
    """扫描引号字面量,返回 (值, 结束位置)。"""
    start = i
    i += 1
    buf: List[str] = []
    n = len(sql)
    closed = False
    while i < n:
        if sql[i] == quote:
            if i + 1 < n and sql[i + 1] == quote:  # 转义
                buf.append(quote)
                i += 2
                continue
            i += 1
            closed = True
            break
        buf.append(sql[i])
        i += 1
    if not closed:
        raise SqlLexError(f"未闭合的字符串字面量,位置 {start}")
    return "".join(buf), i


def tokenize(sql: str) -> List[Token]:
    """把 SQL 文本切成 Token 列表。

    Args:
        sql: SQL 语句文本。

    Returns:
        Token 列表。

    Raises:
        SqlLexError: 遇到无法识别的字符。
    """
    tokens: List[Token] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        # 空白
        if ch.isspace():
            i += 1
            continue
        # 行注释
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        # 字符串字面量
        if ch == "'":
            start = i
            value, i = _scan_quoted(sql, i, "'")
            tokens.append(Token("string", sql[start:i], value, start))
            continue
        # 带引号的标识符
        if ch == '"':
            start = i
            value, i = _scan_quoted(sql, i, '"')
            tokens.append(Token("ident", value.upper(), value, start))
            continue
        # blob 字面量 X'...' / x'...'
        if ch in "xX" and i + 1 < n and sql[i + 1] == "'":
            start = i
            i += 1
            value, i = _scan_quoted(sql, i, "'")
            # 校验十六进制;非法字符按 SQLite 语义抛错
            clean = value.replace(" ", "").replace("\n", "").replace("\t", "")
            if len(clean) % 2 != 0 or any(c not in "0123456789abcdefABCDEF" for c in clean):
                raise SqlLexError(f"非法 blob 字面量,位置 {start}")
            tokens.append(Token("blob", sql[start:i], bytes.fromhex(clean), start))
            continue
        # 数字
        if ch.isdigit() or (ch == "." and i + 1 < n and sql[i + 1].isdigit()):
            start = i
            j = i
            while j < n and sql[j].isdigit():
                j += 1
            is_float = False
            if j < n and sql[j] == ".":
                is_float = True
                j += 1
                while j < n and sql[j].isdigit():
                    j += 1
            if j < n and sql[j] in "eE":
                is_float = True
                j += 1
                if j < n and sql[j] in "+-":
                    j += 1
                while j < n and sql[j].isdigit():
                    j += 1
            raw = sql[start:j]
            tokens.append(
                Token(
                    "number",
                    raw,
                    float(raw) if is_float else int(raw),
                    start,
                )
            )
            i = j
            continue
        # 标识符/关键字
        if ch.isalpha() or ch == "_":
            start = i
            j = i
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            raw = sql[start:j]
            tokens.append(Token("ident", raw.upper(), raw, start))
            i = j
            continue
        # 运算符/标点
        matched = None
        for sym in SYMBOLS:
            if sql.startswith(sym, i):
                matched = sym
                break
        if matched is None:
            raise SqlLexError(f"无法识别的字符 {ch!r},位置 {i}")
        tokens.append(Token("symbol", matched, matched, i))
        i += len(matched)
    return tokens
