"""SQLite 标量函数实现(纯标准库)。

覆盖 func*.test 需要的主要标量函数:abs / length / substr / coalesce /
ifnull / nullif / upper / lower / min / max / round / typeof / quote /
replace / trim 系 / instr / hex / char / unicode / sign / printf / like / glob。

参数数量错误抛 SqlError(与 SQLite 的 "wrong number of arguments to function
xxx()" 一致),供 statement error 记录断言。
"""

from __future__ import annotations

import math
import re
from typing import List, Optional

from .storage import (
    SqlError,
    Value,
    compare_values,
    format_float,
    is_numeric_text,
    numeric_value,
    text_of_value,
)

# ---------------------------------------------------------------- 工具


def _as_text(v: Value) -> Optional[str]:
    """函数语境把值转文本;NULL 返回 None。"""
    if v is None:
        return None
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return text_of_value(v)


def _as_number(v: Value) -> Optional[Value]:
    """函数语境把值转数值;无法解析的文本按 0 处理(NULL 仍为 None)。"""
    if v is None:
        return None
    return numeric_value(v)


# ---------------------------------------------------------------- 函数表
# 每个函数: (实现函数, 参数个数或范围)。arity 用 (min, max),None 表示任意。
_FUNCTIONS = {}


def _register(name: str, arity_min: int, arity_max: Optional[int]):
    def deco(fn):
        _FUNCTIONS[name] = (fn, arity_min, arity_max)
        return fn
    return deco


def call_function(name: str, args: List[Value]) -> Value:
    """调用一个标量函数。

    Raises:
        SqlError: 未知函数或参数数量错误。
    """
    entry = _FUNCTIONS.get(name)
    if entry is None:
        raise SqlError(f"no such function: {name}")
    fn, arity_min, arity_max = entry
    n = len(args)
    if n < arity_min or (arity_max is not None and n > arity_max):
        raise SqlError(f"wrong number of arguments to function {name.lower()}()")
    return fn(args)


# ---------------------------------------------------------------- 实现


@_register("ABS", 1, 1)
def _abs(args):
    v = args[0]
    if v is None:
        return None
    if isinstance(v, int):
        return abs(v)
    if isinstance(v, float):
        return abs(v)
    # 文本/blob:先转 real(与 SQLite 一致,abs('5') → 5.0 real)
    num = numeric_value(v)
    return abs(float(num))


@_register("LENGTH", 1, 1)
def _length(args):
    v = args[0]
    if v is None:
        return None
    if isinstance(v, bytes):
        return len(v)
    s = text_of_value(v)
    # SQLite:对 TEXT 统计首个 NUL 之前的字符数(length(char(0)) → 0)
    nul = s.find("\x00")
    return len(s) if nul < 0 else nul


@_register("SUBSTR", 2, 3)
def _substr(args):
    v = args[0]
    if v is None:
        return None
    s = _as_text(v)
    assert s is not None
    start = int(numeric_value(args[1]))
    n = len(s)
    if start < 0:
        start = n + start + 1  # 从末尾倒数
    if start < 1:
        start = 1
    if len(args) == 2:
        return s[start - 1:] if start <= n else ""
    length = int(numeric_value(args[2]))
    if length < 0:
        # 负长度:返回 start 之前 |length| 个字符(截断到 1)
        end = start - 1
        begin = end + length  # length<0
        if begin < 0:
            begin = 0
        return s[begin:end]
    return s[start - 1:start - 1 + length] if start <= n else ""


@_register("COALESCE", 1, None)
def _coalesce(args):
    for a in args:
        if a is not None:
            return a
    return None


@_register("IFNULL", 2, 2)
def _ifnull(args):
    return args[0] if args[0] is not None else args[1]


@_register("NULLIF", 2, 2)
def _nullif(args):
    a, b = args
    if a is None and b is None:
        return None
    if a is None or b is None:
        return a
    if compare_values(a, b) == 0:
        return None
    return a


@_register("UPPER", 1, 1)
def _upper(args):
    s = _as_text(args[0])
    return None if s is None else _ascii_upper(s)


@_register("LOWER", 1, 1)
def _lower(args):
    s = _as_text(args[0])
    return None if s is None else _ascii_lower(s)


def _ascii_upper(s: str) -> str:
    return "".join(chr(ord(c) - 32) if "a" <= c <= "z" else c for c in s)


def _ascii_lower(s: str) -> str:
    return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in s)


@_register("MIN", 1, None)
def _min(args):
    best = None
    for a in args:
        if a is None:
            return None
        if best is None or compare_values(a, best) < 0:
            best = a
    return best


@_register("MAX", 1, None)
def _max(args):
    best = None
    for a in args:
        if a is None:
            return None
        if best is None or compare_values(a, best) > 0:
            best = a
    return best


@_register("ROUND", 1, 2)
def _round(args):
    v = args[0]
    if v is None:
        return None
    if isinstance(v, str):
        num = numeric_value(v)
    else:
        num = v
    if not isinstance(num, (int, float)):
        num = 0
    n = int(numeric_value(args[1])) if len(args) == 2 else 0
    if n > 30:
        n = 30
    if n < 0:
        n = 0  # 负精度夹具未覆盖;按 0 处理
    r = float(num)
    if n == 0:
        # SQLite n==0 分支:浮点算术,四舍五入远离零(round(2.5)=3)
        if r >= 0:
            return float(math.floor(r + 0.5))
        return float(math.ceil(r - 0.5))
    # n>0:SQLite 用 printf("%.*f") 格式化再解析回 double,
    # 避免浮点乘法放大误差(round(2.675, 2)=2.67 而非 2.68)
    return float(f"{r:.{n}f}")


@_register("TYPEOF", 1, 1)
def _typeof(args):
    v = args[0]
    if v is None:
        return "null"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "real"
    if isinstance(v, bytes):
        return "blob"
    return "text"


@_register("QUOTE", 1, 1)
def _quote(args):
    v = args[0]
    if v is None:
        return "NULL"
    if isinstance(v, bytes):
        return "X'" + v.hex().upper() + "'"
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    return text_of_value(v)


@_register("REPLACE", 3, 3)
def _replace(args):
    s, a, b = args
    if s is None or a is None or b is None:
        return None
    s = _as_text(s)
    a = _as_text(a)
    b = _as_text(b)
    assert s is not None and a is not None and b is not None
    return s.replace(a, b)


@_register("TRIM", 1, 2)
def _trim(args):
    return _trim_impl(args, "both")


@_register("LTRIM", 1, 2)
def _ltrim(args):
    return _trim_impl(args, "left")


@_register("RTRIM", 1, 2)
def _rtrim(args):
    return _trim_impl(args, "right")


def _trim_impl(args, side: str) -> Optional[str]:
    s = _as_text(args[0])
    if s is None:
        return None
    chars = " " if len(args) == 1 else _as_text(args[1]) or " "
    if side in ("both", "left"):
        s = s.lstrip(chars)
    if side in ("both", "right"):
        s = s.rstrip(chars)
    return s


@_register("INSTR", 2, 2)
def _instr(args):
    hay, needle = args
    if hay is None or needle is None:
        return None
    if isinstance(hay, bytes):
        h, n = hay, needle if isinstance(needle, bytes) else text_of_value(needle).encode()
        pos = h.find(n)
        return pos + 1 if pos >= 0 else 0
    h = text_of_value(hay)
    n = text_of_value(needle)
    pos = h.find(n)
    return pos + 1 if pos >= 0 else 0


@_register("HEX", 1, 1)
def _hex(args):
    v = args[0]
    if v is None:
        return None
    if isinstance(v, bytes):
        return v.hex().upper()
    return text_of_value(v).encode("utf-8").hex().upper()


@_register("CHAR", 0, None)
def _char(args):
    out = []
    for a in args:
        code = int(numeric_value(a))
        out.append(chr(code & 0xFFFF))
    return "".join(out)


@_register("UNICODE", 1, 1)
def _unicode(args):
    s = _as_text(args[0])
    if s is None or s == "":
        return None
    return ord(s[0])


@_register("SIGN", 1, 1)
def _sign(args):
    v = args[0]
    if v is None:
        return None
    # SQLite sign() 只对数值存储类(或整体可解析为数字的文本)计算;
    # 'x'/'abc'/'' 这类文本 numeric_type 为 TEXT → NULL(而非按 0 处理)
    if isinstance(v, str):
        if not is_numeric_text(v):
            return None
        num = numeric_value(v)
    elif isinstance(v, bytes):
        return None  # BLOB 永远不是数字
    else:
        num = v
    if num > 0:
        return 1
    if num < 0:
        return -1
    return 0


@_register("PRINTF", 1, None)
def _printf(args):
    fmt = text_of_value(args[0])
    vals = args[1:]
    return _sqlite_printf(fmt, vals)


_PRINTF_RE = re.compile(r"%([-+0 #]*)(\d*)(?:\.(\d+))?([a-zA-Z%])")


def _sqlite_printf(fmt: str, vals: List[Value]) -> str:
    """SQLite 风格 printf:%d %i %s %f %g %e %c %% 与标志/宽度/精度。

    标志支持 - 左对齐、0 零填充、+ 强制符号、空格占位;覆盖 func*.test
    中 printf('%05d', 42) 这类零填充用法。
    """
    out = []
    vi = 0

    def conv(spec, flags, width, prec, ch, v):
        if ch == "%":
            return "%"
        nonlocal vi
        if vi >= len(vals):
            return spec  # 参数不足:原样保留
        val = vals[vi]
        vi += 1
        left = "-" in flags
        zero = "0" in flags and not left
        force_sign = "+" in flags
        space = " " in flags and not force_sign
        w = int(width) if width else 0
        p = int(prec) if prec is not None else None
        if ch in ("d", "i"):
            if val is None:
                s = "0"  # SQLite:printf('%d', NULL) → '0'
            else:
                num = numeric_value(val)
                if isinstance(num, float):
                    num = int(num)
                s = str(int(num)) if num is not None else "0"
            if force_sign and not s.startswith("-"):
                s = "+" + s
            elif space and not s.startswith("-"):
                s = " " + s
            if p is not None and p > 0:
                neg = s.startswith(("-", "+", " "))
                sign = s[0] if neg else ""
                digits = s[1:] if neg else s
                if len(digits) < p:
                    digits = "0" * (p - len(digits)) + digits
                s = sign + digits
            if w > len(s):
                if zero:
                    neg = s.startswith(("-", "+", " "))
                    sign = s[0] if neg else ""
                    digits = s[1:] if neg else s
                    s = sign + "0" * (w - len(s)) + digits
                else:
                    s = s.rjust(w)
        elif ch == "s":
            if val is None:
                s = ""
            else:
                s = _as_text(val) or ""
            if p is not None:
                s = s[:p]
            if w > len(s):
                s = s.ljust(w) if left else s.rjust(w)
        elif ch in ("f", "g", "e"):
            if val is None:
                s = ""
            else:
                num = numeric_value(val)
                num = 0 if num is None else float(num)
                if ch == "f":
                    s = f"{num:.{p if p is not None else 6}f}"
                elif ch == "g":
                    s = format_float(num) if p is None else f"{num:.{p}g}"
                else:  # e
                    s = f"{num:.{p if p is not None else 6}e}"
            if force_sign and not s.startswith("-"):
                s = "+" + s
            if w > len(s):
                if zero:
                    neg = s.startswith(("-", "+", " "))
                    sign = s[0] if neg else ""
                    digits = s[1:] if neg else s
                    s = sign + "0" * (w - len(s)) + digits
                else:
                    s = s.rjust(w)
        elif ch == "c":
            # SQLite %c:取参数的文本表示的第一个字符
            if val is None:
                s = ""
            else:
                t = _as_text(val) or ""
                s = t[0] if t else ""
            if w > len(s):
                s = s.ljust(w) if left else s.rjust(w)
        else:
            return spec
        return s

    i = 0
    while i < len(fmt):
        c = fmt[i]
        if c != "%":
            out.append(c)
            i += 1
            continue
        m = _PRINTF_RE.match(fmt, i)
        if not m:
            out.append(c)
            i += 1
            continue
        flags, width, prec, ch = m.group(1), m.group(2), m.group(3), m.group(4)
        out.append(conv(m.group(0), flags, width, prec, ch, None))
        i = m.end()
    return "".join(out)


@_register("LIKE", 2, 3)
def _like(args):
    """like(X, Y[, E]) ≡ Y LIKE X ESCAPE E(pattern 是第一个参数)。"""
    pattern = _as_text(args[0])
    string = _as_text(args[1])
    escape = _as_text(args[2]) if len(args) == 3 else None
    if pattern is None or string is None or (len(args) == 3 and args[2] is None):
        return None
    return 1 if _like_match(string, pattern, escape, nocase=True) else 0


@_register("GLOB", 2, 2)
def _glob(args):
    """glob(X, Y) ≡ Y GLOB X(pattern 是第一个参数)。"""
    pattern = _as_text(args[0])
    string = _as_text(args[1])
    if pattern is None or string is None:
        return None
    return 1 if _glob_match(string, pattern, nocase=False) else 0


# ---------------------------------------------------------------- LIKE / GLOB 匹配
def _like_match(s: str, p: str, escape: Optional[str], nocase: bool) -> bool:
    """LIKE 模式匹配。% 任意序列,_ 单个字符,ESCAPE 转义。"""
    if escape is not None and len(escape) > 1:
        escape = escape[0]

    def eq(a: str, b: str) -> bool:
        if nocase:
            a, b = _ascii_lower(a), _ascii_lower(b)
        return a == b

    import functools

    @functools.lru_cache(maxsize=None)
    def rec(pi: int, si: int) -> bool:
        while pi < len(p):
            c = p[pi]
            if escape is not None and c == escape:
                # 转义字符位于模式末尾:整体不匹配(SQLite 行为,
                # 如 '%' LIKE '%' ESCAPE '%' → 0)
                if pi + 1 >= len(p):
                    return False
                # 转义下一字符:必须精确匹配(转义后不参与大小写折叠)
                if si >= len(s) or s[si] != p[pi + 1]:
                    return False
                pi += 2
                si += 1
                continue
            if c == "%":
                # % 匹配任意序列(含空);贪心回溯
                while pi < len(p) and p[pi] == "%":
                    pi += 1
                if pi == len(p):
                    return True
                for k in range(si, len(s) + 1):
                    if rec(pi, k):
                        return True
                return False
            if c == "_":
                if si >= len(s):
                    return False
                pi += 1
                si += 1
                continue
            if si >= len(s) or not eq(c, s[si]):
                return False
            pi += 1
            si += 1
        return si == len(s)

    return rec(0, 0)


def _glob_match(s: str, p: str, nocase: bool) -> bool:
    """GLOB 模式匹配。* 任意序列,? 单字符,[abc]/[a-z]/[^a] 字符类。"""

    def eq(a: str, b: str) -> bool:
        if nocase:
            a, b = _ascii_lower(a), _ascii_lower(b)
        return a == b

    import functools

    @functools.lru_cache(maxsize=None)
    def rec(pi: int, si: int) -> bool:
        while pi < len(p):
            c = p[pi]
            if c == "*":
                while pi < len(p) and p[pi] == "*":
                    pi += 1
                if pi == len(p):
                    return True
                for k in range(si, len(s) + 1):
                    if rec(pi, k):
                        return True
                return False
            if c == "?":
                if si >= len(s):
                    return False
                pi += 1
                si += 1
                continue
            if c == "[":
                # 解析字符类
                j = pi + 1
                negate = False
                if j < len(p) and p[j] in ("^", "!"):
                    negate = True
                    j += 1
                chars = []
                while j < len(p) and p[j] != "]":
                    if j + 2 < len(p) and p[j + 1] == "-" and p[j + 2] != "]":
                        chars.append((p[j], p[j + 2]))
                        j += 3
                    else:
                        chars.append((p[j], p[j]))
                        j += 1
                if j >= len(p):
                    return False  # 未闭合
                if si >= len(s):
                    return False
                ch = s[si]
                matched = False
                for lo, hi in chars:
                    if eq(lo, ch) if lo == hi else (ord(lo) <= ord(ch) <= ord(hi)):
                        matched = True
                        break
                if matched == negate:
                    return False
                pi = j + 1
                si += 1
                continue
            if si >= len(s) or not eq(c, s[si]):
                return False
            pi += 1
            si += 1
        return si == len(s)

    return rec(0, 0)
