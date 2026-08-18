"""SQL 递归下降解析器。

支持语句:
- CREATE TABLE <name> ( <col> <type> [COLLATE name] [列约束] , ... )
- INSERT INTO <name> [ ( <col>, ... ) ] VALUES ( <expr>, ... ) , ...
- SELECT <expr> [AS alias] , ... [ FROM <name> ] [ WHERE <expr> ]
  [ ORDER BY <expr> [COLLATE name] [ASC|DESC] , ... ]

表达式支持(按 SQLite 优先级,从低到高):
OR < AND < NOT < IS/IS NOT/IS NULL < IN/BETWEEN/LIKE/GLOB(与 = 同级左结合)
< = != <> < < <= > >= < + - < * / % < || < COLLATE < 一元 - + < 原子。
原子:数字/字符串/blob 字面量、NULL、列引用(可带表限定)、函数调用、
CAST、CASE、括号分组。

不支持:连接、聚合、子查询、索引、事务、DISTINCT、LIMIT。
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from . import ast
from .storage import affinity_of_type
from .tokenizer import SqlLexError, Token, tokenize

# 比较运算符(= 与同层 IN/BETWEEN/LIKE/GLOB 均左结合)
_CMP_OPS = {"=", "!=", "<>"}
_ORDER_CMP_OPS = {"<", "<=", ">", ">="}

# 列约束关键字:本子集不建索引,解析后忽略(PRIMARY KEY 不产生索引)。
_CONSTRAINT_KEYWORDS = {"PRIMARY", "NOT", "NULL", "UNIQUE", "CHECK", "DEFAULT", "REFERENCES"}

# SELECT 投影后不作为别名的子句关键字
_CLAUSE_KEYWORDS = {"FROM", "WHERE", "ORDER", "GROUP", "HAVING", "LIMIT", "OFFSET", "UNION", "EXCEPT", "INTERSECT"}


class SqlParseError(Exception):
    """语法错误:无法解析的 SQL。"""


class Parser:
    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.pos = 0

    # ------------------------------------------------------------ 基础
    def peek(self, offset: int = 0) -> Optional[Token]:
        idx = self.pos + offset
        if idx < len(self.tokens):
            return self.tokens[idx]
        return None

    def next(self) -> Token:
        tok = self.peek()
        if tok is None:
            raise SqlParseError("SQL 意外结束")
        self.pos += 1
        return tok

    def expect_symbol(self, sym: str) -> Token:
        tok = self.next()
        if tok.kind != "symbol" or tok.text != sym:
            raise SqlParseError(f"期望 {sym!r},实际得到 {tok.text!r}(位置 {tok.pos})")
        return tok

    def at_keyword(self, keyword: str) -> bool:
        tok = self.peek()
        return tok is not None and tok.kind == "ident" and tok.text == keyword

    def eat_keyword(self, keyword: str) -> bool:
        if self.at_keyword(keyword):
            self.pos += 1
            return True
        return False

    def expect_ident(self, what: str = "标识符") -> str:
        tok = self.next()
        if tok.kind != "ident":
            raise SqlParseError(f"期望{what},实际得到 {tok.text!r}(位置 {tok.pos})")
        return tok.text

    # ------------------------------------------------------------ 语句
    def parse_statement(self) -> ast.Statement:
        tok = self.peek()
        if tok is None:
            raise SqlParseError("空语句")
        if tok.kind == "ident":
            if tok.text == "CREATE":
                return self.parse_create()
            if tok.text == "INSERT":
                return self.parse_insert()
            if tok.text == "SELECT":
                return self.parse_select()
        raise SqlParseError(f"不支持的语句开头 {tok.text!r}(位置 {tok.pos})")

    # CREATE TABLE
    def parse_create(self) -> ast.CreateTable:
        self.expect_ident("CREATE")
        self.expect_ident("TABLE")
        table = self.expect_ident("表名")
        self.expect_symbol("(")
        columns: List[ast.ColumnDef] = []
        while True:
            col = self.parse_column_def()
            columns.append(col)
            if self.peek() is not None and self.peek().kind == "symbol" and self.peek().text == ",":
                self.next()
                continue
            break
        self.expect_symbol(")")
        # 允许可选的表级约束(如 PRIMARY KEY (...)),本子集解析后忽略
        self.skip_table_constraints()
        return ast.CreateTable(table=table, columns=columns)

    def parse_column_def(self) -> ast.ColumnDef:
        name = self.expect_ident("列名")
        # 无类型列(SQLite 允许 CREATE TABLE t (v))→ 空类型名
        type_name = ""
        nxt = self.peek()
        if (
            nxt is not None
            and nxt.kind == "ident"
            and nxt.text not in _CONSTRAINT_KEYWORDS
            and nxt.text != "COLLATE"
        ):
            type_name, _ = self.parse_type()
        collation = "BINARY"
        if self.at_keyword("COLLATE"):
            self.next()
            collation = self.expect_ident("排序规则名")
        self.skip_column_constraints()
        return ast.ColumnDef(
            name=name,
            type_name=type_name,
            affinity=affinity_of_type(type_name),
            collation=collation,
        )

    def parse_type(self) -> Tuple[str, str]:
        """解析类型名,如 INTEGER、VARCHAR(10)、DOUBLE PRECISION、DECIMAL(5,2)。"""
        first = self.expect_ident("类型名")
        parts = [first]
        # DOUBLE PRECISION / CHARACTER VARYING 等双词类型
        if first == "DOUBLE" and self.at_keyword("PRECISION"):
            parts.append(self.next().text)
        elif first == "CHARACTER" and self.at_keyword("VARYING"):
            parts.append(self.next().text)
        full = " ".join(parts)
        # 可选长度/精度参数,如 VARCHAR(10)、DECIMAL(5,2)
        if self.peek() is not None and self.peek().kind == "symbol" and self.peek().text == "(":
            depth = 0
            while self.peek() is not None:
                t = self.next()
                if t.kind == "symbol" and t.text == "(":
                    depth += 1
                elif t.kind == "symbol" and t.text == ")":
                    depth -= 1
                    if depth == 0:
                        break
            if depth != 0:
                raise SqlParseError("类型参数括号不匹配")
        return full, affinity_of_type(full)

    def skip_column_constraints(self) -> None:
        """跳过列级约束(PRIMARY KEY / NOT NULL / UNIQUE / DEFAULT ...),本子集不实现语义。"""
        while self.peek() is not None and self.peek().kind == "ident":
            kw = self.peek().text
            if kw not in _CONSTRAINT_KEYWORDS:
                break
            if kw == "PRIMARY":
                self.next()
                self.expect_ident("KEY")
            elif kw == "NOT":
                self.next()
                self.expect_ident("NULL")
            elif kw == "UNIQUE":
                self.next()
            elif kw == "CHECK":
                self.next()
                self.skip_parenthesized()
            elif kw == "DEFAULT":
                self.next()
                if self.peek() is not None and self.peek().kind == "symbol" and self.peek().text == "(":
                    self.skip_parenthesized()
                else:
                    self.next()  # 字面量/关键字
            elif kw == "REFERENCES":
                self.next()
                self.expect_ident("引用表名")
                if self.peek() is not None and self.peek().kind == "symbol" and self.peek().text == "(":
                    self.skip_parenthesized()

    def skip_table_constraints(self) -> None:
        """跳过表级约束(如 PRIMARY KEY (col)),本子集解析后忽略。"""
        while self.peek() is not None and self.peek().kind == "ident":
            kw = self.peek().text
            if kw == "PRIMARY":
                self.next()
                self.expect_ident("KEY")
                self.skip_parenthesized()
            elif kw in ("UNIQUE", "CHECK", "FOREIGN"):
                self.next()
                if kw == "FOREIGN":
                    self.expect_ident("KEY")
                self.skip_parenthesized()
            else:
                break

    def skip_parenthesized(self) -> None:
        """消费一个括号组(用于 CHECK/PRIMARY KEY 等约束)。"""
        self.expect_symbol("(")
        depth = 1
        while depth > 0:
            t = self.next()
            if t.kind == "symbol" and t.text == "(":
                depth += 1
            elif t.kind == "symbol" and t.text == ")":
                depth -= 1

    # INSERT
    def parse_insert(self) -> ast.Insert:
        self.expect_ident("INSERT")
        self.expect_ident("INTO")
        table = self.expect_ident("表名")
        columns: Optional[List[str]] = None
        if self.peek() is not None and self.peek().kind == "symbol" and self.peek().text == "(":
            self.next()
            columns = []
            while True:
                columns.append(self.expect_ident("列名"))
                if self.peek() is not None and self.peek().kind == "symbol" and self.peek().text == ",":
                    self.next()
                    continue
                break
            self.expect_symbol(")")
        self.expect_ident("VALUES")
        rows: List[List[ast.Expr]] = []
        while True:
            self.expect_symbol("(")
            row: List[ast.Expr] = []
            while True:
                row.append(self.parse_expr())
                if self.peek() is not None and self.peek().kind == "symbol" and self.peek().text == ",":
                    self.next()
                    continue
                break
            self.expect_symbol(")")
            rows.append(row)
            if self.peek() is not None and self.peek().kind == "symbol" and self.peek().text == ",":
                self.next()
                continue
            break
        return ast.Insert(table=table, columns=columns, rows=rows)

    # SELECT
    def parse_select(self) -> ast.Select:
        self.expect_ident("SELECT")
        projections: List[ast.Expr] = []
        while True:
            expr = self.parse_expr()
            self.skip_alias()
            projections.append(expr)
            if self.peek() is not None and self.peek().kind == "symbol" and self.peek().text == ",":
                self.next()
                continue
            break
        table: Optional[str] = None
        if self.eat_keyword("FROM"):
            table = self.expect_ident("表名")
        where: Optional[ast.Expr] = None
        if self.eat_keyword("WHERE"):
            where = self.parse_expr()
        order_by: List[ast.OrderItem] = []
        if self.eat_keyword("ORDER"):
            self.expect_ident("BY")
            while True:
                expr = self.parse_expr()
                collation: Optional[str] = None
                if self.at_keyword("COLLATE"):
                    self.next()
                    collation = self.expect_ident("排序规则名")
                desc = False
                if self.eat_keyword("DESC"):
                    desc = True
                else:
                    self.eat_keyword("ASC")
                order_by.append(ast.OrderItem(expr=expr, desc=desc, collation=collation))
                if self.peek() is not None and self.peek().kind == "symbol" and self.peek().text == ",":
                    self.next()
                    continue
                break
        return ast.Select(
            projections=projections,
            table=table,
            where=where,
            order_by=order_by,
        )

    def skip_alias(self) -> None:
        """消费 SELECT 投影后的别名(AS x 或裸标识符)。"""
        if self.at_keyword("AS"):
            self.next()
            self.expect_ident("别名")
            return
        tok = self.peek()
        if tok is not None and tok.kind == "ident" and tok.text not in _CLAUSE_KEYWORDS:
            self.next()  # 裸别名

    # ------------------------------------------------------------ 表达式
    # 优先级(低 → 高):
    # OR < AND < NOT < IS 族 < =/!=/<> 与 IN/BETWEEN/LIKE/GLOB(同级左结合)
    # < < <= > >= < + - < * / % < || < COLLATE < 一元 - + < 原子

    def parse_expr(self) -> ast.Expr:
        return self.parse_or()

    def parse_or(self) -> ast.Expr:
        left = self.parse_and()
        while self.at_keyword("OR"):
            self.next()
            right = self.parse_and()
            left = ast.BinaryOp("OR", left, right)
        return left

    def parse_and(self) -> ast.Expr:
        left = self.parse_not()
        while self.at_keyword("AND"):
            self.next()
            right = self.parse_not()
            left = ast.BinaryOp("AND", left, right)
        return left

    def parse_not(self) -> ast.Expr:
        if self.at_keyword("NOT"):
            self.next()
            operand = self.parse_not()
            return ast.UnaryOp("NOT", operand)
        return self.parse_is()

    def parse_is(self) -> ast.Expr:
        left = self.parse_equality()
        while True:
            tok = self.peek()
            if tok is None or tok.kind != "ident":
                break
            if tok.text == "IS":
                self.next()
                negate = False
                if self.at_keyword("NOT"):
                    self.next()
                    negate = True
                if self.at_keyword("NULL"):
                    self.next()
                    left = ast.IsNull(expr=left, negate=negate)
                else:
                    right = self.parse_equality()
                    left = ast.IsOp(left=left, right=right, negate=negate)
                continue
            break
        return left

    def parse_equality(self) -> ast.Expr:
        """= != <> 以及同级左结合的 IN / BETWEEN / LIKE / GLOB。"""
        left = self.parse_order_cmp()
        while True:
            tok = self.peek()
            if tok is None:
                break
            if tok.kind == "symbol" and tok.text in _CMP_OPS:
                self.next()
                right = self.parse_order_cmp()
                left = ast.BinaryOp(tok.text, left, right)
                continue
            if tok.kind == "ident" and tok.text in ("IN", "BETWEEN", "LIKE", "GLOB"):
                left = self.parse_special_operator(left)
                continue
            if tok.kind == "ident" and tok.text == "NOT":
                nxt = self.peek(1)
                if nxt is not None and nxt.kind == "ident" and nxt.text in ("IN", "BETWEEN", "LIKE", "GLOB"):
                    self.next()  # NOT
                    left = self.parse_special_operator(left, negate=True)
                    continue
            break
        return left

    def parse_special_operator(self, left: ast.Expr, negate: bool = False) -> ast.Expr:
        tok = self.next()  # IN / BETWEEN / LIKE / GLOB
        op = tok.text
        if op == "IN":
            self.expect_symbol("(")
            items: List[ast.Expr] = []
            if self.peek() is not None and self.peek().kind == "symbol" and self.peek().text == ")":
                self.next()  # 空 IN () → 结果恒 0
            else:
                while True:
                    items.append(self.parse_equality())
                    if self.peek() is not None and self.peek().kind == "symbol" and self.peek().text == ",":
                        self.next()
                        continue
                    break
                self.expect_symbol(")")
            return ast.InList(expr=left, items=items, negate=negate)
        if op == "BETWEEN":
            low = self.parse_order_cmp()
            if not self.at_keyword("AND"):
                raise SqlParseError("BETWEEN 缺少 AND")
            self.next()
            high = self.parse_order_cmp()
            return ast.Between(expr=left, low=low, high=high, negate=negate)
        if op in ("LIKE", "GLOB"):
            pattern = self.parse_order_cmp()
            escape: Optional[ast.Expr] = None
            if self.at_keyword("ESCAPE"):
                self.next()
                escape = self.parse_order_cmp()
            return ast.Like(
                expr=left,
                pattern=pattern,
                escape=escape,
                glob=(op == "GLOB"),
                negate=negate,
            )
        raise SqlParseError(f"未知特殊运算符 {op}")

    def parse_order_cmp(self) -> ast.Expr:
        left = self.parse_additive()
        while True:
            tok = self.peek()
            if tok is not None and tok.kind == "symbol" and tok.text in _ORDER_CMP_OPS:
                self.next()
                right = self.parse_additive()
                left = ast.BinaryOp(tok.text, left, right)
                continue
            break
        return left

    def parse_additive(self) -> ast.Expr:
        left = self.parse_multiplicative()
        while True:
            tok = self.peek()
            if tok is not None and tok.kind == "symbol" and tok.text in ("+", "-"):
                self.next()
                right = self.parse_multiplicative()
                left = ast.BinaryOp(tok.text, left, right)
                continue
            break
        return left

    def parse_multiplicative(self) -> ast.Expr:
        left = self.parse_concat()
        while True:
            tok = self.peek()
            if tok is not None and tok.kind == "symbol" and tok.text in ("*", "/", "%"):
                self.next()
                right = self.parse_concat()
                left = ast.BinaryOp(tok.text, left, right)
                continue
            break
        return left

    def parse_concat(self) -> ast.Expr:
        left = self.parse_collate()
        while True:
            tok = self.peek()
            if tok is not None and tok.kind == "symbol" and tok.text == "||":
                self.next()
                right = self.parse_collate()
                left = ast.BinaryOp("||", left, right)
                continue
            break
        return left

    def parse_collate(self) -> ast.Expr:
        left = self.parse_unary()
        while self.at_keyword("COLLATE"):
            self.next()
            name = self.expect_ident("排序规则名")
            left = ast.CollateExpr(expr=left, collation=name)
        return left

    def parse_unary(self) -> ast.Expr:
        tok = self.peek()
        if tok is not None and tok.kind == "symbol" and tok.text in ("-", "+"):
            self.next()
            operand = self.parse_unary()
            return ast.UnaryOp(tok.text, operand)
        return self.parse_primary()

    def parse_primary(self) -> ast.Expr:
        tok = self.peek()
        if tok is None:
            raise SqlParseError("表达式意外结束")
        if tok.kind == "number":
            self.next()
            return ast.Literal(tok.value)
        if tok.kind == "string":
            self.next()
            return ast.Literal(tok.value)
        if tok.kind == "blob":
            self.next()
            return ast.Literal(tok.value)
        if tok.kind == "symbol" and tok.text == "(":
            self.next()
            expr = self.parse_expr()
            self.expect_symbol(")")
            return expr
        if tok.kind == "symbol" and tok.text == "*":
            # SELECT * 通配;在乘法语境里 '*' 只会作为右操作数出现,不会走到这里
            self.next()
            return ast.Star()
        if tok.kind == "ident":
            text = tok.text
            if text == "NULL":
                self.next()
                return ast.Literal(None)
            if text in ("TRUE", "FALSE"):
                self.next()
                return ast.Literal(1 if text == "TRUE" else 0)
            if text == "CAST":
                return self.parse_cast()
            if text == "CASE":
                return self.parse_case()
            # 函数调用:标识符后紧跟 (
            nxt = self.peek(1)
            if nxt is not None and nxt.kind == "symbol" and nxt.text == "(":
                return self.parse_func_call()
            self.next()
            # 限定列名 t1.a → 取最后一节
            if self.peek() is not None and self.peek().kind == "symbol" and self.peek().text == ".":
                self.next()
                col = self.expect_ident("列名")
                return ast.ColumnRef(col)
            return ast.ColumnRef(text)
        raise SqlParseError(f"意外的表达式元素 {tok.text!r}(位置 {tok.pos})")

    def parse_cast(self) -> ast.Cast:
        self.expect_ident("CAST")
        self.expect_symbol("(")
        expr = self.parse_expr()
        self.expect_ident("AS")
        target = self.expect_ident("目标类型")
        self.expect_symbol(")")
        return ast.Cast(expr=expr, target=target)

    def parse_case(self) -> ast.Case:
        self.expect_ident("CASE")
        base: Optional[ast.Expr] = None
        if not self.at_keyword("WHEN"):
            base = self.parse_expr()
        branches: List[ast.CaseWhen] = []
        else_expr: Optional[ast.Expr] = None
        while True:
            if self.at_keyword("WHEN"):
                self.next()
                when = self.parse_expr()
                if not self.at_keyword("THEN"):
                    raise SqlParseError("CASE WHEN 缺少 THEN")
                self.next()
                then = self.parse_expr()
                branches.append(ast.CaseWhen(when=when, then=then))
                continue
            if self.at_keyword("ELSE"):
                self.next()
                else_expr = self.parse_expr()
                continue
            if self.at_keyword("END"):
                self.next()
                break
            raise SqlParseError("CASE 未正确结束")
        return ast.Case(base=base, branches=branches, else_expr=else_expr)

    def parse_func_call(self) -> ast.FuncCall:
        name = self.next().text
        self.expect_symbol("(")
        args: List[ast.Expr] = []
        if self.peek() is not None and self.peek().kind == "symbol" and self.peek().text == ")":
            self.next()
        else:
            while True:
                args.append(self.parse_expr())
                if self.peek() is not None and self.peek().kind == "symbol" and self.peek().text == ",":
                    self.next()
                    continue
                break
            self.expect_symbol(")")
        return ast.FuncCall(name=name, args=args)


def parse(sql: str) -> ast.Statement:
    """解析一条 SQL 语句。

    Args:
        sql: SQL 文本(允许前后空白与结尾分号)。

    Returns:
        对应语句的 AST。

    Raises:
        SqlLexError: 词法错误。
        SqlParseError: 语法错误。
    """
    sql = sql.strip()
    if sql.endswith(";"):
        sql = sql[:-1]
    tokens = tokenize(sql)
    parser = Parser(tokens)
    stmt = parser.parse_statement()
    # 语句结束后不应再有多余 token
    rest = parser.peek()
    if rest is not None:
        raise SqlParseError(f"语句结束后出现多余内容 {rest.text!r}(位置 {rest.pos})")
    return stmt
