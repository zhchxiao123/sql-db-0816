"""SQL AST 定义。

覆盖 CREATE TABLE / INSERT / SELECT 以及本模块需要的表达式:
字面量、列引用、函数调用、CAST、CASE、IN、BETWEEN、LIKE/GLOB、
IS [NOT] / IS [NOT] NULL、COLLATE、一元/二元算术与比较、||、%。

范围外特性(连接、聚合、子查询、索引、事务、DISTINCT、LIMIT、UPDATE/DELETE)
不在此处建模。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Union

Number = Union[int, float]
Value = Union[Number, str, bytes, None]  # None 表示 NULL;bytes 表示 BLOB


# ---------------------------------------------------------------- 表达式
class Expr:
    """表达式基类。"""


@dataclass
class Literal(Expr):
    value: Optional[Union[Number, str, bytes]]


@dataclass
class ColumnRef(Expr):
    name: str  # 已归一化为大写;可含表限定(t1.A),执行期剥离


@dataclass
class Star(Expr):
    """SELECT * 通配投影(执行期展开为全部列)。"""


@dataclass
class UnaryOp(Expr):
    op: str  # '-' | '+' | 'NOT'
    operand: Expr


@dataclass
class BinaryOp(Expr):
    op: str  # + - * / % || = != <> < <= > >= AND OR
    left: Expr
    right: Expr


@dataclass
class FuncCall(Expr):
    name: str  # 大写归一化
    args: List[Expr] = field(default_factory=list)


@dataclass
class Cast(Expr):
    expr: Expr
    target: str  # 大写归一化类型名:INTEGER/REAL/TEXT/BLOB/NUMERIC/...


@dataclass
class CaseWhen:
    when: Expr
    then: Expr


@dataclass
class Case(Expr):
    base: Optional[Expr]  # None = 搜索式 CASE;否则简单 CASE base
    branches: List[CaseWhen] = field(default_factory=list)
    else_expr: Optional[Expr] = None


@dataclass
class InList(Expr):
    expr: Expr
    items: List[Expr] = field(default_factory=list)
    negate: bool = False  # NOT IN


@dataclass
class Between(Expr):
    expr: Expr
    low: Expr
    high: Expr
    negate: bool = False  # NOT BETWEEN


@dataclass
class Like(Expr):
    expr: Expr
    pattern: Expr
    escape: Optional[Expr] = None
    glob: bool = False  # True=GLOB, False=LIKE
    negate: bool = False  # NOT LIKE / NOT GLOB


@dataclass
class IsOp(Expr):
    left: Expr
    right: Expr
    negate: bool = False  # IS NOT


@dataclass
class IsNull(Expr):
    expr: Expr
    negate: bool = False  # IS NOT NULL


@dataclass
class CollateExpr(Expr):
    expr: Expr
    collation: str  # 大写归一化:BINARY / NOCASE / RTRIM


# ---------------------------------------------------------------- 语句
class Statement:
    """语句基类。"""


@dataclass
class ColumnDef:
    name: str  # 已归一化为大写
    type_name: str  # 原始类型名,如 INTEGER / VARCHAR(10)
    affinity: str  # 'INTEGER' | 'REAL' | 'TEXT' | 'BLOB' | 'NUMERIC' | 'NONE'
    collation: str = "BINARY"  # 列级排序规则


@dataclass
class CreateTable(Statement):
    table: str
    columns: List[ColumnDef] = field(default_factory=list)


@dataclass
class Insert(Statement):
    table: str
    columns: Optional[List[str]]  # None = 未指定列清单
    rows: List[List[Expr]] = field(default_factory=list)


@dataclass
class OrderItem:
    expr: Expr
    desc: bool = False
    collation: Optional[str] = None  # ORDER BY ... COLLATE name


@dataclass
class Select(Statement):
    projections: List[Expr] = field(default_factory=list)
    table: Optional[str] = None  # None = 无 FROM(常量投影)
    where: Optional[Expr] = None
    order_by: List[OrderItem] = field(default_factory=list)
