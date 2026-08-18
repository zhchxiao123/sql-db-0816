"""语句执行器:把 AST 变为对内存表的操作。

支持:
- CREATE TABLE:建表(不建索引;PRIMARY KEY 约束被解析器吞掉)
- INSERT:追加行(按列亲和做存储类转换)
- SELECT:投影 / WHERE 过滤 / ORDER BY 排序(含排序规则);无 FROM 时投影常量;
  隐式 ROWID(rowid/_rowid_/oid)列引用;无 GROUP BY 的单参数 max/min 整表聚合

表达式求值遵循 SQLite 语义:
- 存储类:NULL < INTEGER/REAL < TEXT < BLOB;文本按排序规则比较
- 算术:文本按数字前缀解析(失败为 0);除零 → NULL;% 先截断为整数
- 比较:先按操作数亲和做转换,再按存储类比较;NULL 参与比较结果 NULL
- 逻辑:三元逻辑(AND/OR/NOT)
- CAST / 标量函数 / LIKE / GLOB / IN / BETWEEN / CASE / IS / COLLATE

范围外(不实现):连接、GROUP BY 聚合、子查询、索引、事务、DISTINCT、LIMIT。
"""

from __future__ import annotations

import functools
import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from . import ast
from .functions import call_function
from .storage import (
    AFF_BLOB,
    AFF_INTEGER,
    AFF_NONE,
    AFF_NUMERIC,
    AFF_REAL,
    AFF_TEXT,
    Column,
    Database,
    SqlError,
    Value,
    affinity_of_type,
    apply_affinity,
    compare_values,
    format_float,
    numeric_value,
    parse_integer_prefix,
    text_of_value,
)

# 数值亲和集合(用于比较规则)
_NUMERIC_AFFS = {AFF_INTEGER, AFF_REAL, AFF_NUMERIC}


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
    columns = [
        Column(
            name=c.name,
            type_name=c.type_name,
            affinity=c.affinity,
            collation=c.collation,
        )
        for c in stmt.columns
    ]
    db.create_table(stmt.table, columns)


def _exec_insert(db: Database, stmt: ast.Insert) -> None:
    rows: List[List[Value]] = []
    for row in stmt.rows:
        values: List[Value] = [_eval_expr(None, [], e) for e in row]
        rows.append(values)
    db.insert(stmt.table, stmt.columns, rows)


# ---------------------------------------------------------------- 求值上下文
# cols: 列定义列表(大写归一化);row: 当前行值;None 表示常量语境(无 FROM)。
# rowid: 当前行的隐式 ROWID(1 起);None 表示无行语境。

# SQLite 隐式 rowid 的别名(真实列同名时优先)
_ROWID_NAMES = {"ROWID", "_ROWID_", "OID"}


def _eval_expr(
    cols: Optional[List[Column]],
    row: Optional[List[Value]],
    expr: ast.Expr,
    rowid: Optional[int] = None,
) -> Value:
    if isinstance(expr, ast.Literal):
        return expr.value
    if isinstance(expr, ast.ColumnRef):
        if cols is None:
            raise SqlError(f"未知列 {expr.name}")
        for i, col in enumerate(cols):
            if col.name == expr.name:
                assert row is not None
                return row[i]
        # 隐式 ROWID:SQLite 每个非 WITHOUT ROWID 表都有 rowid/_rowid_/oid
        if expr.name in _ROWID_NAMES and rowid is not None:
            return rowid
        raise SqlError(f"未知列 {expr.name}")
    if isinstance(expr, ast.Star):
        raise SqlError("SELECT * 需要 FROM 子句")
    if isinstance(expr, ast.UnaryOp):
        return _eval_unary(cols, row, expr, rowid)
    if isinstance(expr, ast.BinaryOp):
        return _eval_binary(cols, row, expr, rowid)
    if isinstance(expr, ast.FuncCall):
        args = [_eval_expr(cols, row, a, rowid) for a in expr.args]
        return call_function(expr.name, args)
    if isinstance(expr, ast.Cast):
        v = _eval_expr(cols, row, expr.expr, rowid)
        return _cast_value(v, expr.target)
    if isinstance(expr, ast.Case):
        return _eval_case(cols, row, expr, rowid)
    if isinstance(expr, ast.InList):
        return _eval_in(cols, row, expr, rowid)
    if isinstance(expr, ast.Between):
        return _eval_between(cols, row, expr, rowid)
    if isinstance(expr, ast.Like):
        return _eval_like(cols, row, expr, rowid)
    if isinstance(expr, ast.IsOp):
        return _eval_is(cols, row, expr, rowid)
    if isinstance(expr, ast.IsNull):
        v = _eval_expr(cols, row, expr.expr, rowid)
        is_null = v is None
        return (1 if is_null else 0) if not expr.negate else (0 if is_null else 1)
    if isinstance(expr, ast.CollateExpr):
        # 排序规则只影响比较,值本身不变
        return _eval_expr(cols, row, expr.expr, rowid)
    raise SqlError(f"未知表达式类型 {type(expr).__name__}")


def _eval_unary(cols, row, expr: ast.UnaryOp, rowid: Optional[int] = None) -> Value:
    operand = _eval_expr(cols, row, expr.operand, rowid)
    if expr.op == "NOT":
        return _not(operand)
    if expr.op == "-":
        if operand is None:
            return None
        num = numeric_value(operand)
        return -num
    if expr.op == "+":
        # SQLite 一元 + 不改变值也不做数值转换('5' 保持文本)
        return operand
    raise SqlError(f"未知一元运算符 {expr.op}")


def _eval_binary(cols, row, expr: ast.BinaryOp, rowid: Optional[int] = None) -> Value:
    op = expr.op
    if op == "AND":
        left = _eval_expr(cols, row, expr.left, rowid)
        if _is_false(left):
            return 0
        right = _eval_expr(cols, row, expr.right, rowid)
        if _is_false(right):
            return 0
        if left is None or right is None:
            return None
        return 1
    if op == "OR":
        left = _eval_expr(cols, row, expr.left, rowid)
        if _is_true(left):
            return 1
        right = _eval_expr(cols, row, expr.right, rowid)
        if _is_true(right):
            return 1
        if left is None or right is None:
            return None
        return 0

    left = _eval_expr(cols, row, expr.left, rowid)
    right = _eval_expr(cols, row, expr.right, rowid)

    if op in ("=", "!=", "<>", "<", "<=", ">", ">="):
        return _compare_op(cols, expr, left, right)
    if op in ("+", "-", "*", "/", "%"):
        return _arith(op, left, right)
    if op == "||":
        return _concat(left, right)
    raise SqlError(f"未知二元运算符 {op}")


def _arith(op: str, a: Value, b: Value) -> Value:
    if a is None or b is None:
        return None
    na = numeric_value(a)
    nb = numeric_value(b)
    if op == "+":
        return _num_result(na + nb, na, nb)
    if op == "-":
        return _num_result(na - nb, na, nb)
    if op == "*":
        return _num_result(na * nb, na, nb)
    if op == "/":
        if nb == 0:
            return None
        if isinstance(na, int) and isinstance(nb, int):
            # 整数除法:向零截断
            return int(na / nb)
        return na / nb
    if op == "%":
        # SQLite %:先把操作数截断为整数,C 风格余数
        ia, ib = int(na), int(nb)
        if ib == 0:
            return None
        r = ia - int(ia / ib) * ib
        if isinstance(na, float) or isinstance(nb, float):
            return float(r)
        return r
    raise SqlError(f"未知算术运算符 {op}")


def _num_result(v, a, b) -> Value:
    """算术结果:全 int 得 int,否则 float。"""
    if isinstance(v, int) and isinstance(a, int) and isinstance(b, int):
        return v
    return float(v)


def _concat(a: Value, b: Value) -> Value:
    if a is None or b is None:
        return None
    if isinstance(a, bytes) or isinstance(b, bytes):
        ta = a if isinstance(a, bytes) else text_of_value(a).encode("utf-8")
        tb = b if isinstance(b, bytes) else text_of_value(b).encode("utf-8")
        return ta + tb
    return text_of_value(a) + text_of_value(b)


# ---------------------------------------------------------------- 比较
def _expr_affinity(cols, expr: ast.Expr) -> str:
    """表达式的亲和(用于比较转换规则)。"""
    if isinstance(expr, ast.ColumnRef) and cols is not None:
        for col in cols:
            if col.name == expr.name:
                return col.affinity
        return AFF_NONE
    if isinstance(expr, ast.Cast):
        return affinity_of_type(expr.target)
    return AFF_NONE


def _expr_collation(cols, expr: ast.Expr) -> str:
    """表达式的排序规则:显式 COLLATE > 列级 COLLATE > BINARY。"""
    if isinstance(expr, ast.CollateExpr):
        return expr.collation
    if isinstance(expr, ast.ColumnRef) and cols is not None:
        for col in cols:
            if col.name == expr.name:
                return col.collation
    return "BINARY"


def _compare_op(cols, expr: ast.BinaryOp, a: Value, b: Value) -> Value:
    if a is None or b is None:
        return None
    coll = _expr_collation(cols, expr.left)
    if coll == "BINARY":
        coll = _expr_collation(cols, expr.right)
    aff_a = _expr_affinity(cols, expr.left)
    aff_b = _expr_affinity(cols, expr.right)
    a2, b2 = _apply_cmp_affinity(a, aff_a, b, aff_b)
    r = compare_values(a2, b2, coll)
    op = expr.op
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


def _apply_cmp_affinity(a: Value, aff_a: str, b: Value, aff_b: str) -> Tuple[Value, Value]:
    """SQLite 比较前的亲和转换规则。"""
    if aff_a in _NUMERIC_AFFS and aff_b not in _NUMERIC_AFFS:
        return a, apply_affinity(b, AFF_NUMERIC)
    if aff_b in _NUMERIC_AFFS and aff_a not in _NUMERIC_AFFS:
        return apply_affinity(a, AFF_NUMERIC), b
    if aff_a == AFF_TEXT and aff_b == AFF_NONE:
        return a, apply_affinity(b, AFF_TEXT)
    if aff_b == AFF_TEXT and aff_a == AFF_NONE:
        return apply_affinity(a, AFF_TEXT), b
    return a, b


def _is_true(a: Value) -> bool:
    """SQLite 真值:NULL 未知;文本按数字前缀转数值;非零即真。"""
    if a is None:
        return False
    n = numeric_value(a)
    return n != 0


def _is_false(a: Value) -> bool:
    if a is None:
        return False
    n = numeric_value(a)
    return n == 0


def _not(a: Value) -> Value:
    if a is None:
        return None
    return 1 if _is_false(a) else 0


# ---------------------------------------------------------------- CAST
def _cast_value(v: Value, target: str) -> Value:
    if v is None:
        return None
    t = target.upper()
    if t == "INTEGER":
        return _cast_to_int(v)
    if t == "REAL":
        return _cast_to_real(v)
    if t == "NUMERIC":
        r = _cast_to_real(v)
        if isinstance(r, float) and r.is_integer():
            return int(r)
        return r
    if t == "TEXT":
        return _cast_to_text(v)
    if t == "BLOB":
        return _cast_to_blob(v)
    # 其它类型名按亲和语义:含 INT → INTEGER,含 CHAR/TEXT/CLOB → TEXT 等
    aff = affinity_of_type(t)
    if aff == AFF_INTEGER:
        return _cast_to_int(v)
    if aff == AFF_REAL:
        return _cast_to_real(v)
    if aff == AFF_TEXT:
        return _cast_to_text(v)
    return _cast_to_blob(v)


def _cast_to_int(v: Value) -> int:
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)  # 向零截断
    # TEXT/BLOB → INTEGER:SQLite 用 sqlite3Atoi64 只解析整数前缀,
    # 所以 CAST('4.2e1' AS INTEGER)=4、CAST('12.5' AS INTEGER)=12。
    s = _bytes_or_text(v)
    num = parse_integer_prefix(s)
    if num is None:
        return 0
    return num


def _cast_to_real(v: Value) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    s = _bytes_or_text(v)
    num = numeric_value(s)
    if num is None:
        return 0.0
    return float(num)


def _cast_to_text(v: Value) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return text_of_value(v)


def _cast_to_blob(v: Value) -> bytes:
    if isinstance(v, bytes):
        return v
    return text_of_value(v).encode("utf-8")


def _bytes_or_text(v: Value) -> str:
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return str(v)


# ---------------------------------------------------------------- CASE / IN / BETWEEN / LIKE / IS
def _eval_case(cols, row, expr: ast.Case, rowid: Optional[int] = None) -> Value:
    base = _eval_expr(cols, row, expr.base, rowid) if expr.base is not None else None
    for br in expr.branches:
        when = _eval_expr(cols, row, br.when, rowid)
        if expr.base is not None:
            # 简单 CASE:base = when(带 base 的排序规则与亲和)
            if base is not None and when is not None:
                coll = _expr_collation(cols, expr.base)
                r = compare_values(base, when, coll)
                if r == 0:
                    return _eval_expr(cols, row, br.then, rowid)
        else:
            if _is_true(when):
                return _eval_expr(cols, row, br.then, rowid)
    if expr.else_expr is not None:
        return _eval_expr(cols, row, expr.else_expr, rowid)
    return None


def _eval_in(cols, row, expr: ast.InList, rowid: Optional[int] = None) -> Value:
    v = _eval_expr(cols, row, expr.expr, rowid)
    coll = _expr_collation(cols, expr.expr)
    aff = _expr_affinity(cols, expr.expr)
    found = False
    has_null = False
    for item in expr.items:
        iv = _eval_expr(cols, row, item, rowid)
        if iv is None:
            has_null = True
            continue
        if v is None:
            continue
        iv2 = apply_affinity(iv, aff) if aff != AFF_NONE else iv
        if compare_values(v, iv2, coll) == 0:
            found = True
            break
    if v is None:
        return None if not expr.negate else None  # NULL IN ... → NULL;NOT IN 亦 NULL
    if found:
        return 0 if expr.negate else 1
    if has_null:
        return None  # 未命中但列表含 NULL → NULL
    return 1 if expr.negate else 0


def _eval_between(cols, row, expr: ast.Between, rowid: Optional[int] = None) -> Value:
    v = _eval_expr(cols, row, expr.expr, rowid)
    low = _eval_expr(cols, row, expr.low, rowid)
    high = _eval_expr(cols, row, expr.high, rowid)
    coll = _expr_collation(cols, expr.expr)
    if v is None or low is None or high is None:
        return None
    r = compare_values(v, low, coll) >= 0 and compare_values(v, high, coll) <= 0
    return (0 if r else 1) if expr.negate else (1 if r else 0)


def _eval_like(cols, row, expr: ast.Like, rowid: Optional[int] = None) -> Value:
    v = _eval_expr(cols, row, expr.expr, rowid)
    p = _eval_expr(cols, row, expr.pattern, rowid)
    esc = _eval_expr(cols, row, expr.escape, rowid) if expr.escape is not None else None
    if v is None or p is None:
        return None
    if expr.escape is not None and esc is None:
        return None
    s = _as_text_like(v)
    pat = _as_text_like(p)
    if expr.glob:
        from .functions import _glob_match

        nocase = _expr_collation(cols, expr.expr) == "NOCASE"
        matched = _glob_match(s, pat, nocase)
    else:
        from .functions import _like_match

        esc_c = _as_text_like(esc)
        matched = _like_match(s, pat, esc_c if esc_c else None, nocase=True)
    return (0 if matched else 1) if expr.negate else (1 if matched else 0)


def _as_text_like(v: Value) -> str:
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return text_of_value(v)


def _eval_is(cols, row, expr: ast.IsOp, rowid: Optional[int] = None) -> Value:
    a = _eval_expr(cols, row, expr.left, rowid)
    b = _eval_expr(cols, row, expr.right, rowid)
    coll = _expr_collation(cols, expr.left)
    if coll == "BINARY":
        coll = _expr_collation(cols, expr.right)
    if a is None and b is None:
        eq = True
    elif a is None or b is None:
        eq = False
    else:
        eq = compare_values(a, b, coll) == 0
    return (0 if eq else 1) if expr.negate else (1 if eq else 0)


# ---------------------------------------------------------------- SELECT
# 聚合函数:本实现只支持无 GROUP BY 时单参数 max/min 的整表聚合
_AGG_FUNCS = {"MAX", "MIN"}


def _is_aggregate(expr: ast.Expr) -> bool:
    return (
        isinstance(expr, ast.FuncCall)
        and expr.name in _AGG_FUNCS
        and len(expr.args) == 1
    )


def _exec_select(db: Database, stmt: ast.Select) -> QueryResult:
    if stmt.table is None:
        for p in stmt.projections:
            if isinstance(p, ast.Star):
                raise SqlError("SELECT * 需要 FROM 子句")
        result = QueryResult()
        result.rows = [[_eval_expr(None, [], e) for e in stmt.projections]]
        return result

    table = db.get_table(stmt.table)
    cols = list(table.columns)

    # 先过滤;源行连同其隐式 rowid(1 起)一起保留
    filtered: List[Tuple[int, List[Value]]] = []
    for ridx, row in enumerate(table.rows):
        if stmt.where is not None:
            cond = _eval_expr(cols, row, stmt.where, ridx + 1)
            if not _is_true(cond):
                continue
        filtered.append((ridx + 1, row))

    # 聚合查询:无 GROUP BY,恒返回一行(SQLite 语义)
    if any(_is_aggregate(e) for e in stmt.projections):
        result = QueryResult()
        result.rows = [_exec_aggregate(cols, stmt.projections, filtered)]
        return result

    # 普通查询:投影;排序键在源行上求值(ORDER BY 可引用未投影列)
    pairs: List[Tuple[List[Value], List[Value], int]] = []  # (源行, 投影行, rowid)
    for rid, row in filtered:
        projected: List[Value] = []
        for e in stmt.projections:
            if isinstance(e, ast.Star):
                projected.extend(row)  # 展开为全部列(表列序)
            else:
                projected.append(_eval_expr(cols, row, e, rid))
        pairs.append((row, projected, rid))

    if stmt.order_by:
        pairs.sort(key=_make_sort_key(cols, stmt.order_by))

    result = QueryResult()
    result.rows = [projected for _, projected, _ in pairs]
    return result


def _exec_aggregate(
    cols: List[Column],
    projections: List[ast.Expr],
    filtered: List[Tuple[int, List[Value]]],
) -> List[Value]:
    """无 GROUP BY 的聚合投影:max/min 对过滤后的整表聚合,跳过 NULL。"""
    out: List[Value] = []
    for e in projections:
        if isinstance(e, ast.Star):
            raise SqlError("聚合查询不支持 SELECT *")
        if _is_aggregate(e):
            vals = [_eval_expr(cols, row, e.args[0], rid) for rid, row in filtered]
            non_null = [v for v in vals if v is not None]
            best: Optional[Value] = None
            for v in non_null:
                if e.name == "MAX":
                    if best is None or compare_values(v, best) > 0:
                        best = v
                else:
                    if best is None or compare_values(v, best) < 0:
                        best = v
            out.append(best)
        else:
            # 非聚合表达式:SQLite 取任意一行(此处取第一行);无行 → NULL
            if filtered:
                rid, row = filtered[0]
                out.append(_eval_expr(cols, row, e, rid))
            else:
                out.append(None)
    return out


def _make_sort_key(cols: List[Column], order_by: List[ast.OrderItem]):
    def cmp(
        a: Tuple[List[Value], List[Value], int],
        b: Tuple[List[Value], List[Value], int],
    ) -> int:
        row_a, _, rid_a = a
        row_b, _, rid_b = b
        for item in order_by:
            va = _eval_expr(cols, row_a, item.expr, rid_a)
            vb = _eval_expr(cols, row_b, item.expr, rid_b)
            coll = item.collation or _expr_collation(cols, item.expr)
            r = compare_values(va, vb, coll)
            if item.desc:
                r = -r
            if r != 0:
                return r
        return 0

    return functools.cmp_to_key(cmp)
