"""sql-db-0816 引擎单元测试(纯标准库 unittest)。

运行:python3 -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

# 保证在未安装(pip install -e .)时也能从 src 布局导入
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from sql_db_0816 import Database, SqlError, SqlLexError, SqlParseError, execute, parse  # noqa: E402
from sql_db_0816.sqllogictest import (  # noqa: E402
    QueryRecord,
    StatementRecord,
    compare_query,
    parse_test_file,
    run_file,
    run_query,
    run_statement,
)
from sql_db_0816.executor import QueryResult  # noqa: E402


class TokenizerTest(unittest.TestCase):
    def test_basic_tokens(self):
        from sql_db_0816.tokenizer import tokenize

        toks = tokenize("CREATE TABLE t1 (id INTEGER, name VARCHAR(10))")
        kinds = [t.kind for t in toks]
        # CREATE TABLE t1 id INTEGER name VARCHAR = 7 个标识符
        self.assertEqual(kinds.count("ident"), 7)
        self.assertEqual(toks[0].text, "CREATE")
        self.assertEqual(toks[-1].kind, "symbol")

    def test_string_escape(self):
        from sql_db_0816.tokenizer import tokenize

        toks = tokenize("INSERT INTO t VALUES('it''s')")
        self.assertEqual(toks[5].value, "it's")

    def test_number(self):
        from sql_db_0816.tokenizer import tokenize

        toks = tokenize("SELECT 1, 2.5, -3 FROM t")
        self.assertEqual(toks[1].value, 1)
        self.assertEqual(toks[3].value, 2.5)

    def test_line_comment(self):
        from sql_db_0816.tokenizer import tokenize

        toks = tokenize("SELECT 1 -- comment\n, 2")
        self.assertEqual(len(toks), 4)

    def test_unterminated_string(self):
        from sql_db_0816.tokenizer import tokenize

        with self.assertRaises(SqlLexError):
            tokenize("SELECT 'abc")


class ParserTest(unittest.TestCase):
    def test_create(self):
        stmt = parse("CREATE TABLE test1 (id INTEGER PRIMARY KEY, name VARCHAR(10))")
        self.assertEqual(stmt.table, "TEST1")
        self.assertEqual([c.name for c in stmt.columns], ["ID", "NAME"])
        self.assertEqual([c.kind for c in stmt.columns], ["int", "text"])

    def test_insert(self):
        stmt = parse("INSERT INTO test1 (name, id) VALUES('Harry', 1), ('Sally', 2)")
        self.assertEqual(stmt.columns, ["NAME", "ID"])
        self.assertEqual(len(stmt.rows), 2)

    def test_select(self):
        stmt = parse("SELECT id, name FROM test1 WHERE id >= 2 ORDER BY id DESC")
        self.assertEqual(stmt.table, "TEST1")
        self.assertEqual(len(stmt.order_by), 1)
        self.assertTrue(stmt.order_by[0].desc)

    def test_select_no_from(self):
        stmt = parse("SELECT 1")
        self.assertIsNone(stmt.table)

    def test_parse_error(self):
        with self.assertRaises(SqlParseError):
            parse("SELECT FROM WHERE")

    def test_unsupported_statement(self):
        with self.assertRaises(SqlParseError):
            parse("DROP TABLE t")

    def test_trailing_semicolon(self):
        stmt = parse("CREATE TABLE t (a INT);")
        self.assertEqual(stmt.table, "T")


class ExecutorTest(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        execute(self.db, parse("CREATE TABLE t (id INTEGER, name TEXT)"))
        execute(self.db, parse("INSERT INTO t VALUES(1, 'a'), (2, 'b')"))

    def test_insert_select_all(self):
        result = execute(self.db, parse("SELECT * FROM t"))
        self.assertEqual(result.rows, [[1, "a"], [2, "b"]])

    def test_where_filter(self):
        result = execute(self.db, parse("SELECT name FROM t WHERE id = 2"))
        self.assertEqual(result.rows, [["b"]])

    def test_order_by(self):
        execute(self.db, parse("INSERT INTO t VALUES(0, 'z')"))
        result = execute(self.db, parse("SELECT id FROM t ORDER BY id DESC"))
        self.assertEqual(result.rows, [[2], [1], [0]])

    def test_arithmetic_projection(self):
        result = execute(self.db, parse("SELECT id * 2 FROM t"))
        self.assertEqual(result.rows, [[2], [4]])

    def test_create_duplicate_table(self):
        with self.assertRaises(SqlError):
            execute(self.db, parse("CREATE TABLE t (a INT)"))

    def test_unknown_table(self):
        with self.assertRaises(SqlError):
            execute(self.db, parse("SELECT * FROM missing"))

    def test_type_coercion_error(self):
        with self.assertRaises(SqlError):
            execute(self.db, parse("INSERT INTO t VALUES('x', 'y')"))

    def test_insert_column_subset(self):
        # 只给部分列赋值:未指定的列应为 NULL
        execute(self.db, parse("INSERT INTO t (id) VALUES(3)"))
        rows = execute(self.db, parse("SELECT * FROM t ORDER BY id")).rows
        self.assertEqual(rows[-1], [3, None])

    def test_where_and(self):
        execute(self.db, parse("INSERT INTO t VALUES(3, 'a')"))
        result = execute(self.db, parse("SELECT id FROM t WHERE name = 'a' AND id > 1"))
        self.assertEqual(result.rows, [[3]])

    def test_where_or(self):
        result = execute(self.db, parse("SELECT id FROM t WHERE id = 1 OR id = 2"))
        self.assertEqual(len(result.rows), 2)


class SqllogictestTest(unittest.TestCase):
    def _tmp_test(self, content: str) -> Path:
        fd, path = tempfile.mkstemp(suffix=".test")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        return Path(path)

    def test_parse_records(self):
        p = self._tmp_test(
            "statement ok\nCREATE TABLE t (a INT)\n\n"
            "statement error\nSELECT * FROM missing\n\n"
            "query I rowsort\nSELECT a FROM t\n----\n1\n2\n"
        )
        recs = parse_test_file(p)
        self.assertEqual(len(recs), 3)
        self.assertIsInstance(recs[0], StatementRecord)
        self.assertIsInstance(recs[1], StatementRecord)
        self.assertFalse(recs[1].expect_ok)
        self.assertIsInstance(recs[2], QueryRecord)
        self.assertEqual(recs[2].sort_mode, "rowsort")
        self.assertEqual(recs[2].expected_rows, [["1"], ["2"]])

    def test_run_file_pass(self):
        content = (
            "statement ok\nCREATE TABLE t (a INT)\n\n"
            "statement ok\nINSERT INTO t VALUES(1), (2)\n\n"
            "query I rowsort\nSELECT a FROM t\n----\n1\n2\n"
        )
        p = self._tmp_test(content)
        passed, total, failures = run_file(p)
        self.assertEqual((passed, total, failures), (3, 3, []))

    def test_run_file_fail(self):
        content = (
            "statement ok\nCREATE TABLE t (a INT)\n\n"
            "query I rowsort\nSELECT a FROM t\n----\n9\n"
        )
        p = self._tmp_test(content)
        passed, total, failures = run_file(p)
        self.assertEqual(passed, 1)
        self.assertEqual(total, 2)
        self.assertEqual(len(failures), 1)

    def test_statement_error(self):
        content = "statement error\nSELECT * FROM missing\n"
        p = self._tmp_test(content)
        passed, total, failures = run_file(p)
        self.assertEqual((passed, failures), (1, []))

    def test_valuesort(self):
        content = (
            "statement ok\nCREATE TABLE t (a INT)\n\n"
            "statement ok\nINSERT INTO t VALUES(3), (1), (2)\n\n"
            "query I valuesort\nSELECT a FROM t\n----\n1\n2\n3\n"
        )
        p = self._tmp_test(content)
        passed, total, failures = run_file(p)
        self.assertEqual(failures, [])

    def test_hash_comparison(self):
        import hashlib

        digest = hashlib.md5(b"1\n2\n").hexdigest()
        content = (
            "statement ok\nCREATE TABLE t (a INT)\n\n"
            "statement ok\nINSERT INTO t VALUES(2), (1)\n\n"
            "query I valuesort\nSELECT a FROM t\n----\n"
            f"2 values hashing to {digest}\n"
        )
        p = self._tmp_test(content)
        passed, total, failures = run_file(p)
        self.assertEqual(failures, [])

    def test_multiline_sql(self):
        content = (
            "statement ok\nCREATE TABLE t (\n a INT,\n b TEXT\n)\n\n"
            "query I rowsort\nSELECT a\nFROM t\n----\n"
        )
        p = self._tmp_test(content)
        passed, total, failures = run_file(p)
        self.assertEqual(failures, [])


class FixtureSmokeTest(unittest.TestCase):
    def test_select1_fixture(self):
        root = _REPO_ROOT / "test" / "select1.test"
        passed, total, failures = run_file(root)
        self.assertEqual(failures, [])
        self.assertEqual(total, 5)

    def test_insert1_fixture(self):
        root = _REPO_ROOT / "test" / "insert1.test"
        passed, total, failures = run_file(root)
        self.assertEqual(failures, [])
        self.assertEqual(total, 8)


if __name__ == "__main__":
    unittest.main()
