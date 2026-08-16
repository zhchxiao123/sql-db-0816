"""语句执行器:把 AST 变为对内存表的操作。

支持:
- CREATE TABLE:建表(不建索引;PRIMARY KEY 约束被解析器吞掉)
- INSERT:追加行(按列类型宽松强转)
- SELECT:投影 / WHERE 过滤 / ORDER BY 排序;无 FROM 时投影常量

范围外(不实现):连接、聚合、子查询、索引、事务。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from . import ast
from .storage import Column, Database, SqlError, Value


@dataclass
class QueryResult:
    """SELECT 的执行结果:行(每行是原始值列表)。"""

    rows: List[List[Value]] = field(default_factory=list)


def execute(db: Database, stmt: ast.Statement):
    """执行一条语句。

    Returns:
        QueryResult(SELECT)或 None(CREATE/INSERT)。

    Raises:
        SqlError: 执行失败(语句级错误)。
    """
    if isinstance(stmt, ast.CreateTable):
        _exec_create(db, stmt)
        return None
    if isinstance(stmt, ast.Insert):
        _exec_insert(db, stmt)
        return None
    if isinstance(stmt, ast.Select):
        return _exec_select(db, stmt)
    raise SqlError(f"不支持的语句类型 {type(stmt).__name__}")


def _exec_create(db: Database, stmt: ast.CreateTable) -> None:
    columns = [Column(name=c.name, type_name=c.type_name, kind=c.kind) for c in stmt.columns]
    db.create_table(stmt.table, columns)


def _exec_insert(db: Database, stmt: ast.Insert) -> None:
    rows: List[List[Value]] = []
    for row in stmt.rows:
        values: List[Value] = []
        for expr in row:
            if not isinstance(expr, ast.Literal):
                raise SqlError("INSERT VALUES 只支持字面量")
            values.append(expr.value)
        rows.append(values)
    db.insert(stmt.table, stmt.columns, rows)


def _exec_select(db: Database, stmt: ast.Select) -> QueryResult:
    if stmt.table is None:
        # 常量投影:SELECT 1(无 FROM);SELECT * 无 FROM 不合法
        for p in stmt.projections:
            if isinstance(p, ast.Star):
                raise SqlError("SELECT * 需要 FROM 子句")
        cols: List[str] = []
        result = QueryResult()
        result.rows = [[_eval_expr(cols, [], e) for e in stmt.projections]]
        return result

    table = db.get_table(stmt.table)
    cols = [c.name for c in table.columns]

    # 先过滤,再投影;排序键在源行上求值(ORDER BY 可引用未投影列)
    pairs: List[Tuple[List[Value], List[Value]]] = []  # (源行, 投影行)
    for row in table.rows:
        if stmt.where is not None:
            cond = _eval_expr(cols, row, stmt.where)
            if not _is_true(cond):
                continue
        projected: List[Value] = []
        for e in stmt.projections:
            if isinstance(e, ast.Star):
                projected.extend(row)  # 展开为全部列(表列序)
            else:
                projected.append(_eval_expr(cols, row, e))
        pairs.append((row, projected))

    if stmt.order_by:
        pairs.sort(key=_make_sort_key(cols, stmt.order_by))

    result = QueryResult()
    result.rows = [projected for _, projected in pairs]
    return result


def _make_sort_key(cols: List[str], order_by: List[ast.OrderItem]):
    import functools

    def cmp(a: Tuple[List[Value], List[Value]], b: Tuple[List[Value], List[Value]]) -> int:
        row_a, _ = a
        row_b, _ = b
        for item in order_by:
            va = _eval_expr(cols, row_a, item.expr)
            vb = _eval_expr(cols, row_b, item.expr)
            r = _compare_values(va, vb)
            if item.desc:
                r = -r
            if r != 0:
                return r
        return 0

    return functools.cmp_to_key(cmp)


def _compare_values(a: Value, b: Value) -> int:
    """NULL 最小;数值按数值比较,其余按字符串比较。"""
    if a is None and b is None:
        return 0
    if a is None:
        return -1
    if b is None:
        return 1
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return (a > b) - (a < b)
    return (str(a) > str(b)) - (str(a) < str(b))


# ---------------------------------------------------------------- 表达式求值
# 求值上下文:cols 是当前表的列名列表(大写),row 是当前行值;列引用按名字查索引。
# 逻辑真值:int 0 = False,非 0 = True,None = UNKNOWN。


def _eval_expr(cols: List[str], row: List[Value], expr: ast.Expr) -> Value:
    if isinstance(expr, ast.Literal):
        return expr.value
    if isinstance(expr, ast.ColumnRef):
        for i, name in enumerate(cols):
            if name == expr.name:
                return row[i]
        raise SqlError(f"未知列 {expr.name}")
    if isinstance(expr, ast.UnaryOp):
        operand = _eval_expr(cols, row, expr.operand)
        if expr.op == "NOT":
            return _not(operand)
        if expr.op == "-":
            if operand is None:
                return None
            if not isinstance(operand, (int, float)):
                raise SqlError(f"一元负号作用于非数值 {operand!r}")
            return -operand
        raise SqlError(f"未知一元运算符 {expr.op}")
    if isinstance(expr, ast.BinaryOp):
        return _eval_binary(cols, row, expr)
    raise SqlError(f"未知表达式类型 {type(expr).__name__}")


def _eval_binary(cols: List[str], row: List[Value], expr: ast.BinaryOp) -> Value:
    op = expr.op
    # 短路逻辑运算(三元逻辑)
    if op == "AND":
        left = _eval_expr(cols, row, expr.left)
        if _is_false(left):
            return 0
        right = _eval_expr(cols, row, expr.right)
        if _is_false(right):
            return 0
        if left is None or right is None:
            return None
        return 1
    if op == "OR":
        left = _eval_expr(cols, row, expr.left)
        if _is_true(left):
            return 1
        right = _eval_expr(cols, row, expr.right)
        if _is_true(right):
            return 1
        if left is None or right is None:
            return None
        return 0

    left = _eval_expr(cols, row, expr.left)
    right = _eval_expr(cols, row, expr.right)

    if op in ("=", "!=", "<>", "<", "<=", ">", ">="):
        return _compare_op(op, left, right)
    if op in ("+", "-", "*", "/"):
        return _arith(op, left, right)
    raise SqlError(f"未知二元运算符 {op}")


def _arith(op: str, a: Value, b: Value) -> Value:
    if a is None or b is None:
        return None
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        raise SqlError(f"算术运算符 {op} 需要数值,得到 {a!r} 与 {b!r}")
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if op == "/":
        if b == 0:
            raise SqlError("除数为零")
        # SQLite 风格:整数除法整除时得整数,否则浮点
        if isinstance(a, int) and isinstance(b, int):
            if a % b == 0:
                return a // b
            return a / b
        return a / b
    raise SqlError(f"未知算术运算符 {op}")


def _compare_op(op: str, a: Value, b: Value) -> Value:
    if a is None or b is None:
        return None
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        r = (a > b) - (a < b)
    else:
        sa, sb = str(a), str(b)
        r = (sa > sb) - (sa < sb)
    if op == "=":
        return 1 if r == 0 else 0
    if op in ("!=", "<>"):
        return 1 if r != 0 else 0
    if op == "<":
        return 1 if r < 0 else 0
    if op == "<=":
        return 1 if r <= 0 else 0
    if op == ">":
        return 1 if r > 0 else 0
    if op == ">=":
        return 1 if r >= 0 else 0
    raise SqlError(f"未知比较运算符 {op}")


def _not(a: Value) -> Value:
    if a is None:
        return None
    return 0 if _is_true(a) else 1


def _is_true(a: Value) -> bool:
    return a is not None and a != 0


def _is_false(a: Value) -> bool:
    return a is not None and a == 0
