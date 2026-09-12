# -*- coding: utf-8 -*-
"""命令行入口：一条命令跑完「起服务 -> 执行用例 -> 出报告」。

    python scripts/run.py              # 全量用例（不含性能门禁）
    python scripts/run.py smoke        # 只跑冒烟（提交前自检）
    python scripts/run.py regression   # 只跑回归
    python scripts/run.py concurrency  # 只跑并发与一致性
    python scripts/run.py perf         # 只跑性能门禁
    python scripts/run.py all          # 全量 + 性能
    python scripts/run.py serve        # 只启动被测服务，供 Postman / JMeter 手工验证
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUITES = {
    "smoke": ["-m", "smoke"],
    "regression": ["-m", "regression"],
    "concurrency": ["-m", "concurrency"],
    "perf": ["-m", "perf"],
    "all": [],
    "default": ["-m", "not perf"],
}


def run(args):
    print("> pytest %s" % " ".join(args))
    return subprocess.call([sys.executable, "-m", "pytest"] + args, cwd=str(ROOT))


def main():
    suite = (sys.argv[1] if len(sys.argv) > 1 else "default").lower()
    extra = sys.argv[2:]
    if suite == "serve":
        return subprocess.call([sys.executable, "-m", "service"] + extra, cwd=str(ROOT))
    if suite not in SUITES:
        print("未知套件 %r，可选：%s" % (suite, ", ".join(sorted(SUITES))))
        return 2
    code = run(SUITES[suite] + extra)
    report = ROOT / "reports" / "report.html"
    print("\n执行结束（退出码 %d），HTML 报告：%s" % (code, report))
    return code


if __name__ == "__main__":
    sys.exit(main())
