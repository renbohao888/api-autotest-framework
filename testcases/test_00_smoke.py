# -*- coding: utf-8 -*-
"""冒烟用例：提交前 / 上线前必跑的核心链路。"""
import pytest

from framework.assertions import (assert_code, assert_equal, assert_field, assert_status,
                                  assert_subset)


@pytest.mark.smoke
def test_health_check(api):
    """服务健康检查：进程存活、依赖可访问、响应结构统一。"""
    resp = api.get("/api/health", auth=False)
    assert_status(resp, 200)
    assert_code(resp, 0)
    assert_subset(resp, {"status": "UP", "service": "seckill-sut"}, path="data")
    assert_field(resp, "data.version", "1.0.0")


@pytest.mark.smoke
def test_main_flow_login_then_order(api, db):
    """主链路冒烟：登录 -> 下单 -> 查单，并做一次数据库落库 + 扣减校验。"""
    api.post("/api/admin/reset", auth=False, retry=0, json_body={"stock": 10})

    login = api.login("tester", "test123")
    assert_status(login, 200)
    assert_code(login, 0)

    order = api.post("/api/seckill/order", json_body={"activityId": 1})
    assert_status(order, 200)
    assert_code(order, 0)
    order_no = order.get("data.orderNo")
    assert order_no, "下单未返回订单号：%s" % order.body_preview()

    detail = api.get("/api/order/%s" % order_no)
    assert_status(detail, 200)
    assert_subset(detail, {"status": "WAIT_PAY", "userId": "tester", "activityId": 1}, path="data")

    assert_equal("订单已落库", db.count("orders", "order_no=?", (order_no,)), 1)
    assert_equal("库存已扣减", db.scalar("SELECT stock FROM activity WHERE activity_id=1"), 9)
