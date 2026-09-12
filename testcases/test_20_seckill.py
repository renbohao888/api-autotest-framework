# -*- coding: utf-8 -*-
"""秒杀下单接口：数据驱动用例（等价类 + 边界值 + 异常 + 鉴权 + 幂等 + 性能基线）。

用例内容全部写在 data/seckill_cases.yaml，这里只负责「取用例 -> 执行 -> 追加库内校验」，
新增场景只需要改 YAML。
"""
import pytest

from framework.assertions import assert_equal, assert_db_count
from framework.dataloader import params
from framework.runner import run_case

CASES = params("data/seckill_cases.yaml")


@pytest.mark.parametrize("case", CASES)
def test_seckill_order_cases(api, db, case):
    """一条 YAML = 一条用例；断言失败信息里会带出期望值 / 实际值 / 响应体。"""
    resp = run_case(api, case)

    # 关键场景追加数据库侧校验：接口返回与库内数据必须一致
    if case["id"] == "SEC-001":
        assert_equal("接口返回的剩余库存与库内一致", resp.get("data.remainStock"),
                     db.scalar("SELECT stock FROM activity WHERE activity_id=1"))
        assert_db_count(db, "orders", "activity_id=1", (), 1)
    elif case["id"] == "SEC-003":
        assert_db_count(db, "orders", "activity_id=1", (), 0)
    elif case["id"] in ("SEC-004", "SEC-005"):
        assert_db_count(db, "orders", "activity_id=1", (), 1)
