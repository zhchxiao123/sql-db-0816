# sql-db-0816

最小可运行的 SQL 数据库引擎骨架 + sqllogictest 测试运行器。

## 能力范围

| 能力 | 支持 |
|---|---|
| DDL | `CREATE TABLE <name> (col type [约束], ...)` |
| DML | `INSERT INTO <name> [(col,...)] VALUES (...), (...)` |
| 查询 | `SELECT expr[, ...] [FROM t] [WHERE cond] [ORDER BY expr [ASC|DESC], ...]` |
| 表达式 | 字面量、列引用、`+ - * /`、比较、`AND/OR/NOT`、括号、一元负号、无 FROM 常量投影 |
| 类型 | INTEGER/BIGINT/SMALLINT/TINYINT、REAL/FLOAT/DOUBLE/DECIMAL、TEXT/VARCHAR(n)/CHAR(n)、BOOLEAN 等(未知类型宽容为文本) |
| 存储 | 内存表(每进程一个 Database 实例,每个 .test 文件独立实例) |

**明确不支持**(按需求范围):连接(JOIN)、聚合(GROUP BY/COUNT 等)、子查询、
索引(PRIMARY KEY 约束仅解析不建索引、不强制唯一)、事务、持久化、NULL 比较
(`IS NULL`)、UPDATE/DELETE/DROP。

## 快速开始

```bash
# 一条命令复跑 sqllogictest 套件(输出通过/失败统计)
python3 run_sqllogictest.py

# 指定文件
python3 run_sqllogictest.py test/select1.test test/insert1.test

# 引擎单元测试(纯标准库)
python3 -m unittest discover -s tests -v

# 或一键:单元测试 + sqllogictest
make test
```

运行器输出示例:

```
test/insert1.test: PASS (8 条记录全部通过)
test/select1.test: PASS (5 条记录全部通过)
------------------------------------------------------------
汇总: 13/13 条记录通过
文件: 全部通过
```

## 结构

```
src/sql_db_0816/
├── tokenizer.py      # SQL 词法分析
├── ast.py            # AST 定义
├── parser.py         # 递归下降解析器
├── storage.py        # 目录 + 内存表存储
├── executor.py       # CREATE/INSERT/SELECT 执行
└── sqllogictest.py   # sqllogictest 运行器(解析/执行/比对/CLI)
test/                 # sqllogictest 用例(select1.test、insert1.test)
tests/                # 引擎单元测试
run_sqllogictest.py   # 一条命令入口
```

## sqllogictest 协议支持

- `statement ok` / `statement error [pattern]`:执行语句并断言成功/失败;
  `error` 后可带期望错误文本(按正则匹配,非法正则退化为子串匹配)。
- `query <types> [nosort|rowsort|valuesort]`:`----` 分隔期望结果;
  types 每列一个字符(`R`/`I`/`T`/`F`);rowsort 按行排序比较、
  valuesort 按全部值排序比较、nosort 保序比较。
- 期望区支持哈希形式:`N values hashing to <md5>`(对排序后的全部值
  以 `\n` 连接并加尾换行后取 md5)。
- `#` 为注释,空行分隔记录,SQL 可跨行。

## 引擎语义说明

- 标识符大小写不敏感,内部统一大写;字符串字面量支持 `''` 转义。
- 插入值按列类型宽松强转(整数列拒绝非数值文本);未指定列插入 NULL。
- 逻辑运算为三元逻辑(NULL 参与时 AND/OR 按 SQL 语义返回未知)。
- 整数除法:整除得整数,否则浮点(SQLite 风格);除零报错。
- 排序键在源行上求值(ORDER BY 可引用未投影列);NULL 视为最小。

## 开发约定

- 纯标准库,无第三方依赖;`python3 >= 3.9`。
- 自测入口:`make test`(单元测试 + sqllogictest)。
