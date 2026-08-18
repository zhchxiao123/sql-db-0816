"""sqllogictest 运行器:解析 .test 文件、执行、按协议比对。

实现 sqllogictest 记录格式(与官方 gregrahn/sqllogictest 运行器对齐):
- statement ok / statement error [pattern]:执行语句,断言成功或失败
- query <types> [nosort|rowsort|valuesort] [label]:执行查询,按列类型格式化
  结果,与期望比对
  - types:每个结果列一个字符,R=浮点(%.3f)、I=整数(%d)、T=文本
  - sortmode:nosort(默认)/ rowsort / valuesort
  - 期望区:---- 之后每行一个值;支持哈希形式 "N values hashing to <md5>"
  - NULL → "NULL";空串 → "(empty)";控制/不可打印字符 → '@'
- # 为注释,空行分隔记录,SQL 可跨行

用法:
    python3 -m sql_db_0816.sqllogictest [test/*.test ...]
"""

from __future__ import annotations

import hashlib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple, Union

from .executor import QueryResult, execute
from .parser import SqlLexError, SqlParseError, parse
from .storage import Database, SqlError, Value

# 排序模式
SORT_NOSORT = "nosort"
SORT_ROWSORT = "rowsort"
SORT_VALUESORT = "valuesort"

_VALID_SORTS = {SORT_NOSORT, SORT_ROWSORT, SORT_VALUESORT}
_VALID_TYPES = set("RITF")

# 哈希行:如 "5 values hashing to a5e4f2d3..."
_HASH_RE = re.compile(r"^\s*(\d+)\s+values\s+hashing\s+to\s+([0-9a-fA-F]{32})\s*$")


class TestFormatError(Exception):
    """.test 文件格式错误。"""


# ---------------------------------------------------------------- 记录模型
@dataclass
class StatementRecord:
    line: int
    expect_ok: bool  # True=statement ok, False=statement error
    error_pattern: Optional[str] = None
    sql: str = ""


@dataclass
class QueryRecord:
    line: int
    types: str
    sort_mode: str
    label: Optional[str] = None
    sql: str = ""
    expected_rows: List[List[str]] = field(default_factory=list)
    hash_expected: Optional[Tuple[int, str]] = None  # (值数量, md5)


Record = Union[StatementRecord, QueryRecord]


# ---------------------------------------------------------------- 解析 .test
def parse_test_file(path: Path) -> List:
    """解析一个 .test 文件为记录列表。

    Raises:
        TestFormatError: 格式错误。
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    records: List = []
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        stripped = raw.strip()
        # 跳过空行与注释
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        start_line = i + 1
        if stripped.startswith("statement"):
            parts = stripped.split(None, 1)
            if len(parts) == 1:
                raise TestFormatError(f"{path}:{start_line} statement 记录缺少 ok/error")
            verdict = parts[1].strip()
            if verdict == "ok":
                expect_ok, pattern = True, None
            elif verdict.startswith("error"):
                rest = verdict[len("error"):].strip()
                expect_ok, pattern = False, (rest or None)
            else:
                raise TestFormatError(
                    f"{path}:{start_line} statement 状态必须是 ok 或 error,得到 {verdict!r}"
                )
            i += 1
            sql_lines: List[str] = []
            while i < n:
                if not lines[i].strip():
                    break
                if lines[i].strip().startswith("#"):
                    i += 1
                    continue
                sql_lines.append(lines[i])
                i += 1
            records.append(
                StatementRecord(
                    line=start_line,
                    expect_ok=expect_ok,
                    error_pattern=pattern,
                    sql="\n".join(sql_lines).strip(),
                )
            )
            continue
        if stripped.startswith("query"):
            parts = stripped.split(None, 3)
            if len(parts) < 2:
                raise TestFormatError(f"{path}:{start_line} query 记录缺少类型串")
            types = parts[1].strip()
            if not types or any(c not in _VALID_TYPES for c in types):
                raise TestFormatError(f"{path}:{start_line} 非法类型串 {types!r}")
            sort_mode = SORT_NOSORT
            label: Optional[str] = None
            if len(parts) >= 3:
                third = parts[2].strip().lower()
                if third in _VALID_SORTS:
                    sort_mode = third
                    if len(parts) >= 4:
                        label = parts[3].strip()
                else:
                    # 第 3 个 token 不是排序模式 → 是 label
                    label = parts[2].strip()
            i += 1
            sql_lines = []
            while i < n:
                if lines[i].strip() == "----":
                    break
                if lines[i].strip().startswith("#"):
                    i += 1
                    continue
                sql_lines.append(lines[i])
                i += 1
            # 允许 query 记录没有 ----(期望空集)
            has_sep = i < n and lines[i].strip() == "----"
            if has_sep:
                i += 1  # 跳过 ----
                # 期望区:每行一个值(可能跨越多列)
                expected_rows: List[List[str]] = []
                hash_expected: Optional[Tuple[int, str]] = None
                first_expected = True
                while i < n and lines[i].strip():
                    line = lines[i].strip()
                    if line.startswith("#"):
                        i += 1
                        continue
                    m = _HASH_RE.match(line)
                    if first_expected and m:
                        hash_expected = (int(m.group(1)), m.group(2).lower())
                        i += 1
                        break
                    # 每行一个值;按列数切分成行
                    expected_rows.append([line])
                    first_expected = False
                    i += 1
                # 展平并按列数分组
                flat_exp = [v for row in expected_rows for v in row]
                ncol = len(types)
                expected_rows = [
                    flat_exp[k:k + ncol] for k in range(0, len(flat_exp), ncol)
                ]
            else:
                expected_rows = []
                hash_expected = None
                # 没有 ---- 分隔行 → 空结果集
            records.append(
                QueryRecord(
                    line=start_line,
                    types=types,
                    sort_mode=sort_mode,
                    label=label,
                    sql="\n".join(sql_lines).strip(),
                    expected_rows=expected_rows,
                    hash_expected=hash_expected,
                )
            )
            continue
        raise TestFormatError(
            f"{path}:{start_line} 无法识别的记录头 {stripped[:40]!r}"
        )
    return records


# ---------------------------------------------------------------- 执行与比对
def format_value(value: Value, type_char: str) -> str:
    """把结果值按 sqllogictest 列类型格式化为文本(与官方运行器一致)。"""
    if value is None:
        return "NULL"
    if type_char == "I":
        # sqlite3_column_int:%d,浮点向零截断
        return str(int(value))
    if type_char in ("R", "F"):
        return "%.3f" % float(value)
    # T
    s = value if isinstance(value, str) else _value_to_text(value)
    if s == "":
        return "(empty)"
    # 控制/不可打印字符 → '@'
    return "".join(c if " " <= c <= "~" else "@" for c in s)


def _value_to_text(value: Value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, float):
        from .storage import format_float

        return format_float(value)
    return str(value)


def normalize_expected(text: str, type_char: str) -> str:
    """把期望值按列类型归一化,与实际值可比。"""
    t = text.strip()
    if t == "NULL":
        return t
    if type_char == "I":
        try:
            return str(int(float(t)))
        except ValueError:
            return t
    if type_char in ("R", "F"):
        try:
            return "%.3f" % float(t)
        except ValueError:
            return t
    return t


def run_statement(db: Database, rec: StatementRecord) -> Optional[str]:
    """执行 statement 记录;通过返回 None,失败返回原因。"""
    try:
        stmt = parse(rec.sql)
    except (SqlLexError, SqlParseError) as exc:
        if not rec.expect_ok:
            return _check_error_pattern(rec, str(exc))
        return f"statement ok 期望成功,但解析失败:{exc}"
    try:
        execute(db, stmt)
    except SqlError as exc:
        if not rec.expect_ok:
            return _check_error_pattern(rec, str(exc))
        return f"statement ok 期望成功,但执行失败:{exc}"
    except Exception as exc:  # 防御:未预期异常也视为失败
        if not rec.expect_ok:
            return _check_error_pattern(rec, f"{type(exc).__name__}: {exc}")
        return f"statement ok 期望成功,但出现未预期异常:{type(exc).__name__}: {exc}"
    if not rec.expect_ok:
        return "statement error 期望失败,但执行成功"
    return None


def _check_error_pattern(rec: StatementRecord, message: str) -> Optional[str]:
    if rec.error_pattern is None:
        return None
    try:
        if re.search(rec.error_pattern, message):
            return None
        return f"错误信息不匹配期望模式 {rec.error_pattern!r}:{message}"
    except re.error:
        if rec.error_pattern in message:
            return None
        return f"错误信息不包含期望文本 {rec.error_pattern!r}:{message}"


def run_query(db: Database, rec: QueryRecord) -> Optional[str]:
    """执行 query 记录;通过返回 None,失败返回原因。"""
    try:
        stmt = parse(rec.sql)
    except (SqlLexError, SqlParseError) as exc:
        return f"query 解析失败:{exc}"
    try:
        result = execute(db, stmt)
    except SqlError as exc:
        return f"query 执行失败:{exc}"
    except Exception as exc:
        return f"query 未预期异常:{type(exc).__name__}: {exc}"
    assert isinstance(result, QueryResult)
    return compare_query(rec, result)


def compare_query(rec: QueryRecord, result: QueryResult) -> Optional[str]:
    """按协议比对查询结果。"""
    actual_rows = result.rows
    ncols = len(rec.types)
    if actual_rows and len(actual_rows[0]) != ncols:
        return f"结果列数 {len(actual_rows[0])} 与类型串长度 {ncols} 不一致"
    if not actual_rows and rec.expected_rows and len(rec.expected_rows[0]) != ncols:
        return f"期望列数 {len(rec.expected_rows[0])} 与类型串长度 {ncols} 不一致"

    # 实际值格式化
    actual_text = [
        [format_value(v, rec.types[i]) for i, v in enumerate(row)]
        for row in actual_rows
    ]

    # 哈希比对:先按排序模式排好,再对全部值 md5
    if rec.hash_expected is not None:
        flat = [v for row in actual_text for v in row]
        if len(flat) != rec.hash_expected[0]:
            return f"结果值数量 {len(flat)} 与期望 {rec.hash_expected[0]} 不一致"
        ordered = _apply_sort(rec.sort_mode, actual_text, ncols)
        digest = hashlib.md5(
            ("\n".join(ordered) + "\n").encode("utf-8")
        ).hexdigest()
        if digest != rec.hash_expected[1]:
            return f"结果哈希 {digest} 与期望 {rec.hash_expected[1]} 不一致"
        return None

    # 期望值归一化
    expected_text = [
        [normalize_expected(v, rec.types[i]) for i, v in enumerate(row)]
        for row in rec.expected_rows
    ]
    actual_norm = [
        [normalize_expected(v, rec.types[i]) for i, v in enumerate(row)]
        for row in actual_text
    ]

    if rec.sort_mode == SORT_VALUESORT:
        exp_flat = sorted(v for row in expected_text for v in row)
        act_flat = sorted(v for row in actual_norm for v in row)
        if exp_flat != act_flat:
            return _diff_summary(rec, "值集合不一致", act_flat, exp_flat)
        return None

    if rec.sort_mode == SORT_ROWSORT:
        exp_rows = sorted(tuple(r) for r in expected_text)
        act_rows = sorted(tuple(r) for r in actual_norm)
    else:  # nosort
        exp_rows = [tuple(r) for r in expected_text]
        act_rows = [tuple(r) for r in actual_norm]

    if act_rows != exp_rows:
        return _diff_summary(rec, "结果集不一致", act_rows, exp_rows)
    return None


def _apply_sort(sort_mode: str, rows: List[List[str]], ncols: int) -> List[str]:
    """按排序模式生成用于哈希的值序列(与官方运行器一致)。"""
    if sort_mode == SORT_VALUESORT:
        flat = [v for row in rows for v in row]
        return sorted(flat)
    if sort_mode == SORT_ROWSORT:
        sorted_rows = sorted(tuple(r) for r in rows)
        return [v for row in sorted_rows for v in row]
    return [v for row in rows for v in row]


def _diff_summary(rec, why: str, actual, expected) -> str:
    short_a = [str(r) for r in actual[:8]]
    short_e = [str(r) for r in expected[:8]]
    return (
        f"{why}(期望 {len(expected)} 行/值,实际 {len(actual)} 行/值);"
        f"期望前 {len(short_e)}: {short_e};"
        f"实际前 {len(short_a)}: {short_a}"
    )


# ---------------------------------------------------------------- CLI
def run_file(path: Path, verbose: bool = False) -> Tuple[int, int, List[str]]:
    """运行单个 .test 文件。

    Returns:
        (通过数, 总数, 失败原因列表)
    """
    records = parse_test_file(path)
    db = Database()
    passed = 0
    failures: List[str] = []
    for rec in records:
        if isinstance(rec, StatementRecord):
            err = run_statement(db, rec)
        else:
            err = run_query(db, rec)
        if err is None:
            passed += 1
        else:
            label = "statement" if isinstance(rec, StatementRecord) else "query"
            failures.append(f"  行 {rec.line} [{label}] 失败: {err}")
            if verbose:
                print(f"    sql: {rec.sql!r}")
    return passed, len(records), failures


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="sqllogictest",
        description="sql-db-0816 的 sqllogictest 运行器",
    )
    parser.add_argument(
        "files",
        nargs="*",
        help=".test 文件路径;缺省跑 test/ 目录下全部 *.test",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="失败时打印 SQL")
    args = parser.parse_args(argv)

    if args.files:
        files = [Path(f) for f in args.files]
    else:
        test_dir = Path(__file__).resolve().parent.parent.parent / "test"
        files = sorted(test_dir.glob("*.test"))
    if not files:
        print("未找到任何 .test 文件", file=sys.stderr)
        return 2

    total_passed = 0
    total_records = 0
    any_fail = False
    for path in files:
        try:
            passed, count, failures = run_file(path, verbose=args.verbose)
        except TestFormatError as exc:
            print(f"{path}: 格式错误: {exc}")
            any_fail = True
            continue
        total_passed += passed
        total_records += count
        if failures:
            any_fail = True
            print(f"{path}: FAIL ({passed}/{count} 通过)")
            for f in failures:
                print(f)
        else:
            print(f"{path}: PASS ({count} 条记录全部通过)")

    print("-" * 60)
    print(f"汇总: {total_passed}/{total_records} 条记录通过")
    print(f"文件: {'全部通过' if not any_fail else '存在失败'}")
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
