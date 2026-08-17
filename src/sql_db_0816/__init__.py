"""sql-db-0816:SQL 引擎 + sqllogictest 运行器。

子模块:
- tokenizer:SQL 词法分析
- ast / parser:AST 与递归下降解析器(CREATE TABLE / INSERT / SELECT + 表达式)
- storage:目录与内存表存储(亲和转换、排序规则、比较)
- functions:SQLite 标量函数
- executor:语句执行与表达式求值
- sqllogictest:官方 sqllogictest .test 运行器

范围:无连接、聚合、子查询、索引、事务、DISTINCT、LIMIT。纯标准库实现。
"""

from .executor import QueryResult, execute
from .parser import SqlParseError, parse
from .storage import Database, SqlError
from .tokenizer import SqlLexError

__all__ = [
    "Database",
    "QueryResult",
    "SqlError",
    "SqlLexError",
    "SqlParseError",
    "execute",
    "parse",
]

__version__ = "0.3.0"
