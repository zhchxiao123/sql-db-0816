#!/usr/bin/env python3
"""sql-db-0816 sqllogictest 运行器 — 一条命令复跑入口。

用法(在仓库根目录):
    python3 run_sqllogictest.py            # 跑 test/*.test 全部
    python3 run_sqllogictest.py test/select1.test test/insert1.test
    python3 run_sqllogictest.py -v         # 失败时打印 SQL

退出码:0 = 全部通过,1 = 存在失败,2 = 没有找到测试文件。
"""

import sys
from pathlib import Path

# 未安装(pip install)时也能从 src 布局导入
_REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from sql_db_0816.sqllogictest import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
