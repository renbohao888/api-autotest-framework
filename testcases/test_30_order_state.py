# -*- coding: utf-8 -*-
"""订单状态机用例：状态迁移法（合法迁移 + 非法迁移 + 库存回补 + 查单异常）。

用例内容在 data/order_flow_cases.yaml，主请求路径用 ${order_no} 引用前置步骤提取的变量。
"""
import pytest

from framework.assertions import assert_db_count, assert_db_eventually, assert_equal
from framework.dataloader import params
from framework.runner import run_case

CASES = params("data/order_flow_cases.yaml")

# 非法迁移后，库内状态必须保持原样（说明服务端拒绝时没有改数据）
EXPECT_STATE = {"ODR-006": "CANCELED", "ODR-007": "PAID",
                "ODR-008": "PAID", "ODR-009": "TIMEOUT_CLOSED"}
# 需要回补库存的迁移
RESTOCK_IDS = ("ODR-004", "ODR-005")


@pytest.mark.parametrize("case", CASES)
def test_order_state_machine(api, db, case):
    run_case(api, case)

    if case["id"] in RESTOCK_IDS:
        assert_db_eventually(db, "SELECT stock FROM activity WHERE activity_id=1", (), 5,
                             timeout=2.0, name="取消/超时关单后库存已回补到 5")
    if case["id"] in EXPECT_STATE:
        assert_equal("非法迁移后库内状态保持不变",
                     db.scalar("SELECT status FROM orders WHERE activity_id=1"),
                     EXPECT_STATE[case["id"]])
        assert_db_count(db, "orders", "activity_id=1", (), 1)
