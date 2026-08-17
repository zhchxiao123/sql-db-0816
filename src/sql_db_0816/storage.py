"""内存表存储与目录。

Value 表示:
- int    → INTEGER
- float  → REAL
- str    → TEXT
- bytes  → BLOB
- None   → NULL

列类型 → 亲和(affinity)按 SQLite 规则判定;插入时按亲和做存储类转换。
本子集不实现索引(含 PRIMARY KEY 唯一性)、事务与持久化。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union

Value = Union[int, float, str, bytes, None]

# 亲和常量
AFF_INTEGER = "INTEGER"
AFF_REAL = "REAL"
AFF_TEXT = "TEXT"
AFF_BLOB = "BLOB"
AFF_NUMERIC = "NUMERIC"
AFF_NONE = "NONE"

_NUMERIC_AFFINITIES = {AFF_INTEGER, AFF_REAL, AFF_NUMERIC}


class SqlError(Exception):
    """执行期错误(statement error 记录期望的失败类型)。"""


# ---------------------------------------------------------------- 数值文本解析
# SQLite 把文本转数字:跳过前导空白,解析可选符号、数字、小数点、指数,
# 解析到第一个非法字符为止;完全无法解析时返回 0(算术语境)。
_INT_RE = re.compile(r"^\s*[+-]?\d+")
_FLOAT_RE = re.compile(r"^\s*[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
# 完整数字文本(供 sign() 的 numeric_type 判定):允许首尾空白,可含 inf/nan
_FULL_NUM_RE = re.compile(
    r"^\s*[+-]?(?:inf(?:inity)?|nan|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*$",
    re.IGNORECASE,
)


def parse_numeric_prefix(text: str) -> Optional[Value]:
    """按 SQLite sqlite3AtoF 语义解析文本的数字前缀。

    Returns:
        解析出的 int / float;若前缀不是合法数字返回 None(调用方按 0 处理)。
    """
    if not isinstance(text, str):
        return None
    m = _FLOAT_RE.match(text)
    if not m:
        m = _INT_RE.match(text)
    if not m:
        return None
    raw = m.group(0)
    if any(c in raw for c in ".eE"):
        try:
            return float(raw)
        except ValueError:
            return None
    try:
        return int(raw)
    except ValueError:
        return None


def parse_integer_prefix(text: str) -> Optional[int]:
    """按 SQLite sqlite3Atoi64 语义解析文本的整数前缀。

    CAST('4.2e1' AS INTEGER) → 4(在小数点处停止)、CAST('  -42abc' AS INTEGER)
    → -42、CAST('0x10' AS INTEGER) → 0。无数字前缀返回 None。
    """
    if not isinstance(text, str):
        return None
    m = _INT_RE.match(text)
    if not m:
        return None
    try:
        return int(m.group(0))
    except ValueError:
        return None


def is_numeric_text(text: str) -> bool:
    """文本是否整体是一个合法数字(SQLite sqlite3_value_numeric_type 判定)。

    用于 sign() 等按存储类区分 TEXT 数值性的函数:'5x'/'abc'/'' 不是数字,
    '  -3 '、'1e3'、'12.5'、'Inf' 是。
    """
    return isinstance(text, str) and bool(_FULL_NUM_RE.match(text))


def numeric_value(value: Value) -> Value:
    """把值转成数值(算术/比较语境)。文本按前缀解析,失败为 0。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, bytes):
        v = parse_numeric_prefix(value.decode("utf-8", "replace"))
        return 0 if v is None else v
    v = parse_numeric_prefix(value)
    return 0 if v is None else v


# ---------------------------------------------------------------- 亲和判定
def affinity_of_type(type_name: str) -> str:
    """根据声明的列类型判定亲和(SQLite 规则)。"""
    t = (type_name or "").upper()
    if "INT" in t:
        return AFF_INTEGER
    if any(k in t for k in ("CHAR", "CLOB", "TEXT")):
        return AFF_TEXT
    if "BLOB" in t or t == "":
        return AFF_BLOB
    if any(k in t for k in ("REAL", "FLOA", "DOUB")):
        return AFF_REAL
    return AFF_NUMERIC


def apply_affinity(value: Value, affinity: str) -> Value:
    """把值按列亲和做存储类转换(SQLite 规则)。NULL/BLOB 原样保留(除 TEXT 列外)。"""
    if value is None:
        return None
    if affinity in (AFF_NONE, AFF_BLOB):
        return value
    if isinstance(value, bytes):
        return value  # 亲和转换不作用于 BLOB
    if affinity == AFF_TEXT:
        if isinstance(value, str):
            return value
        return text_of_value(value)
    # 数值亲和:INTEGER / REAL / NUMERIC
    if isinstance(value, str):
        num = parse_numeric_prefix(value)
        if num is None:
            return value  # 无法解析的文本保留为 TEXT
        value = num
    if affinity == AFF_REAL:
        return float(value)
    # INTEGER / NUMERIC:整数值 → int,否则保留数值
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def text_of_value(value: Value) -> str:
    """把非文本值转成文本表示(CAST AS TEXT / TEXT 亲和 / || 连接)。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, int):
        return str(value)
    # float → SQLite 的文本表示(%.15g 风格)
    return format_float(value)


def format_float(f: float) -> str:
    """SQLite 浮点转文本:%.15g 风格 + 整数形浮点补 .0。

    SQLite 使用 printf("%!.15g") 并保证结果可 round-trip:
    - 0.1+0.2 → '0.3'
    - 1.0 → '1.0'
    - 1e16 → '1.0e+16'
    - 5e-5 → '5.0e-05'
    """
    if f == 0:
        return "0.0" if f == 0 else "-0.0"
    s = "%.15g" % f
    if "e" in s or "E" in s:
        # 把 1e+16 变成 1.0e+16,1e-05 变成 1.0e-05
        mant, _, exp = s.replace("E", "e").partition("e")
        if "." not in mant:
            mant += ".0"
        exp_sign = "+" if not exp.startswith(("-", "+")) else exp[0]
        exp_digits = exp.lstrip("+-")
        exp_digits = exp_digits.zfill(2) if len(exp_digits) < 2 else exp_digits
        return f"{mant}e{exp_sign}{exp_digits}"
    if "." not in s:
        s += ".0"
    return s


# ---------------------------------------------------------------- 比较
def compare_values(a: Value, b: Value, collation: str = "BINARY") -> int:
    """SQLite 存储类比较:NULL < 数值 < 文本 < BLOB。

    Args:
        a, b: 待比较值(已按亲和转换过)。
        collation: BINARY / NOCASE / RTRIM,仅影响文本 vs 文本。

    Returns:
        -1 / 0 / 1。
    """
    if a is None and b is None:
        return 0
    if a is None:
        return -1
    if b is None:
        return 1
    a_num = isinstance(a, (int, float))
    b_num = isinstance(b, (int, float))
    if a_num and b_num:
        return (a > b) - (a < b)
    a_txt = isinstance(a, str)
    b_txt = isinstance(b, str)
    a_blob = isinstance(a, bytes)
    b_blob = isinstance(b, bytes)
    # 不同大类:NULL < 数值 < 文本 < BLOB
    def _class(v, is_num, is_txt, is_blob):
        return 0 if is_num else (1 if is_txt else 2)

    ca, cb = _class(a, a_num, a_txt, a_blob), _class(b, b_num, b_txt, b_blob)
    if ca != cb:
        return (ca > cb) - (ca < cb)
    if a_txt and b_txt:
        return _compare_text(a, b, collation)
    if a_blob and b_blob:
        return (a > b) - (a < b)
    # 同为数值但至少一个是 bool 等异常情况兜底
    return (str(a) > str(b)) - (str(a) < str(b))


def _compare_text(a: str, b: str, collation: str) -> int:
    if collation == "NOCASE":
        return (a.lower() > b.lower()) - (a.lower() < b.lower())
    if collation == "RTRIM":
        return (a.rstrip() > b.rstrip()) - (a.rstrip() < b.rstrip())
    return (a > b) - (a < b)


# ---------------------------------------------------------------- 目录
@dataclass
class Column:
    name: str  # 大写归一化
    type_name: str  # 原始类型名
    affinity: str = AFF_NONE  # 由类型名推导
    collation: str = "BINARY"  # 列级排序规则


@dataclass
class Table:
    name: str
    columns: List[Column] = field(default_factory=list)
    rows: List[List[Value]] = field(default_factory=list)

    def column_index(self, name: str) -> int:
        for i, col in enumerate(self.columns):
            if col.name == name:
                return i
        raise SqlError(f"未知列 {name}")


@dataclass
class Database:
    """一个数据库实例:持有全部表。每个 sqllogictest 测试文件独立一个实例。"""

    tables: Dict[str, Table] = field(default_factory=dict)

    def create_table(self, name: str, columns: List[Column]) -> None:
        if name in self.tables:
            raise SqlError(f"表 {name} 已存在")
        self.tables[name] = Table(name=name, columns=columns)

    def get_table(self, name: str) -> Table:
        try:
            return self.tables[name]
        except KeyError:
            raise SqlError(f"未知表 {name}") from None

    def insert(self, name: str, columns: Optional[List[str]], rows: List[List[Value]]) -> None:
        table = self.get_table(name)
        if columns is None:
            indexes = list(range(len(table.columns)))
        else:
            indexes = [table.column_index(c) for c in columns]
            if len(set(indexes)) != len(indexes):
                raise SqlError("INSERT 列清单存在重复列")
        for row in rows:
            if len(row) != len(indexes):
                raise SqlError(f"INSERT 值数 {len(row)} 与列数 {len(indexes)} 不一致")
            coerced = [None] * len(table.columns)  # 未指定的列填 NULL
            for i, idx in enumerate(indexes):
                coerced[idx] = apply_affinity(row[i], table.columns[idx].affinity)
            table.rows.append(coerced)
