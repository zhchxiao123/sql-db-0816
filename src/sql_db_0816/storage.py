"""内存表存储与目录。

Value 表示:Python int(整数列)、float(浮点列)、str(文本列)、None(NULL)。
本子集不实现索引(含 PRIMARY KEY 唯一性)、事务与持久化。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union

Value = Union[int, float, str, None]


class SqlError(Exception):
    """执行期错误(statement error 记录期望的失败类型)。"""


@dataclass
class Column:
    name: str  # 大写归一化
    type_name: str  # 原始类型名
    kind: str  # 'int' | 'float' | 'text'


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


def coerce_value(value: Value, kind: str) -> Value:
    """把插入值按列类型做宽松强转。

    - int 列:接受 int / 可解析为 int 的 str(float 会先转整?不,拒绝)
    - float 列:接受 int / float / 可解析为 float 的 str
    - text 列:接受任意标量,统一转 str
    - None(NULL) 直接透传

    Raises:
        SqlError: 值无法强转到目标类型。
    """
    if value is None:
        return None
    if kind == "int":
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if value.is_integer():
                return int(value)
            raise SqlError(f"无法把 {value!r} 存入整数列")
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                raise SqlError(f"无法把 {value!r} 存入整数列") from None
        raise SqlError(f"无法把 {value!r} 存入整数列")
    if kind == "float":
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                raise SqlError(f"无法把 {value!r} 存入浮点列") from None
        raise SqlError(f"无法把 {value!r} 存入浮点列")
    # text
    if isinstance(value, str):
        return value
    return str(value)


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
                coerced[idx] = coerce_value(row[i], table.columns[idx].kind)
            table.rows.append(coerced)
