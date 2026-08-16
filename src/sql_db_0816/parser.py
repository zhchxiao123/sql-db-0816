"""SQL 递归下降解析器(最小子集)。

支持语句:
- CREATE TABLE <name> ( <col> <type> [列约束] , ... )
- INSERT INTO <name> [ ( <col>, ... ) ] VALUES ( <expr>, ... ) , ...
- SELECT <expr> , ... [ FROM <name> ] [ WHERE <expr> ] [ ORDER BY <expr> [ASC|DESC] , ... ]

表达式支持:字面量、列引用、一元负号/NOT、四则运算、比较、
AND/OR,以及括号分组。不支持:连接、聚合、子查询、索引、事务。
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from . import ast
from .tokenizer import SqlLexError, Token, tokenize

# 类型名 → 归一化 kind
_TYPE_KINDS = {
    "INT": "int",
    "INTEGER": "int",
    "BIGINT": "int",
    "SMALLINT": "int",
    "TINYINT": "int",
    "MEDIUMINT": "int",
    "INT2": "int",
    "INT4": "int",
    "INT8": "int",
    "SERIAL": "int",
    "REAL": "float",
    "FLOAT": "float",
    "DOUBLE": "float",
    "DOUBLE PRECISION": "float",
    "NUMERIC": "float",
    "DECIMAL": "float",
    "TEXT": "text",
    "VARCHAR": "text",
    "CHAR": "text",
    "CHARACTER": "text",
    "CLOB": "text",
    "STRING": "text",
    "BOOLEAN": "int",
    "BOOL": "int",
    "BLOB": "text",
    "DATE": "text",
    "DATETIME": "text",
    "TIMESTAMP": "text",
}

# 列约束关键字:本子集不建索引,解析后忽略(PRIMARY KEY 不产生索引)。
_CONSTRAINT_KEYWORDS = {"PRIMARY", "NOT", "NULL", "UNIQUE", "CHECK", "DEFAULT", "REFERENCES"}

# 比较运算符
_CMP_OPS = {"=", "!=", "<>", "<", "<=", ">", ">="}


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
        type_name, kind = self.parse_type()
        self.skip_column_constraints()
        return ast.ColumnDef(name=name, type_name=type_name, kind=kind)

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
        kind = _TYPE_KINDS.get(full, "text")  # 未知类型按 text 宽容处理
        return full, kind

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
            projections.append(self.parse_expr())
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
                desc = False
                if self.eat_keyword("DESC"):
                    desc = True
                else:
                    self.eat_keyword("ASC")
                order_by.append(ast.OrderItem(expr=expr, desc=desc))
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

    # ------------------------------------------------------------ 表达式
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
        return self.parse_comparison()

    def parse_comparison(self) -> ast.Expr:
        left = self.parse_additive()
        tok = self.peek()
        if tok is not None and tok.kind == "symbol" and tok.text in _CMP_OPS:
            self.next()
            right = self.parse_additive()
            return ast.BinaryOp(tok.text, left, right)
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
        left = self.parse_unary()
        while True:
            tok = self.peek()
            if tok is not None and tok.kind == "symbol" and tok.text in ("*", "/"):
                self.next()
                right = self.parse_unary()
                left = ast.BinaryOp(tok.text, left, right)
                continue
            break
        return left

    def parse_unary(self) -> ast.Expr:
        tok = self.peek()
        if tok is not None and tok.kind == "symbol" and tok.text == "-":
            self.next()
            operand = self.parse_unary()
            return ast.UnaryOp("-", operand)
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
            # NULL 字面量
            if tok.text == "NULL":
                self.next()
                return ast.Literal(None)
            self.next()
            return ast.ColumnRef(tok.text)
        raise SqlParseError(f"意外的表达式元素 {tok.text!r}(位置 {tok.pos})")


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
