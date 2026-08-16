"""SQL AST 定义(最小子集)。

仅覆盖 CREATE TABLE / INSERT / SELECT 基础形态。范围外特性
(连接、聚合、子查询、索引、事务)不在此处建模。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Union

Number = Union[int, float]


# ---------------------------------------------------------------- 表达式
class Expr:
    """表达式基类。"""


@dataclass
class Literal(Expr):
    value: Optional[Union[Number, str]]  # None 表示 NULL


@dataclass
class ColumnRef(Expr):
    name: str  # 已归一化为大写


@dataclass
class Star(Expr):
    """SELECT * 通配投影(执行期展开为全部列)。"""


@dataclass
class UnaryOp(Expr):
    op: str  # '-' 或 'NOT'
    operand: Expr


@dataclass
class BinaryOp(Expr):
    op: str  # + - * / = != <> < <= > >= AND OR
    left: Expr
    right: Expr


# ---------------------------------------------------------------- 语句
class Statement:
    """语句基类。"""


@dataclass
class ColumnDef:
    name: str  # 已归一化为大写
    type_name: str  # 原始类型名,如 INTEGER / VARCHAR(10)
    kind: str  # 归一化类型:'int' | 'float' | 'text'


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


@dataclass
class Select(Statement):
    projections: List[Expr] = field(default_factory=list)
    table: Optional[str] = None  # None = 无 FROM(常量投影)
    where: Optional[Expr] = None
    order_by: List[OrderItem] = field(default_factory=list)
