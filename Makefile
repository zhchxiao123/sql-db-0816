# sql-db-0816 常用命令
.PHONY: test unit sqllogictest

# 默认入口:单元测试 + sqllogictest 套件
test: unit sqllogictest

# 引擎单元测试(纯标准库 unittest)
unit:
	python3 -m unittest discover -s tests -v

# sqllogictest 运行器:跑 test/*.test,输出通过/失败统计
sqllogictest:
	python3 run_sqllogictest.py
