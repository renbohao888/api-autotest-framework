# -*- coding: utf-8 -*-
"""pytest 根配置。

把 framework/report.py 注册成插件（等价于命令行 -p framework.report）：
放在根 conftest 里可以保证「仓库根目录已进入 sys.path」之后再加载插件，
这样 python testcases/xxx.py 或任意目录下执行 pytest 都不会 import 失败。
"""
pytest_plugins = ["framework.report"]
