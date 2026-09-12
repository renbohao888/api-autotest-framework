# -*- coding: utf-8 -*-
"""鉴权用例：登录鉴权链路的正向与异常分支（接口层的 401 / 业务码校验）。"""
import pytest

from framework.assertions import (assert_code, assert_status, assert_subset,
                                  assert_text_contains, assert_true)


@pytest.mark.smoke
def test_login_success_returns_usable_token(api):
    """正向：登录成功下发可用 token，带上 token 才能访问受保护接口。"""
    resp = api.post("/api/auth/login", auth=False,
                    json_body={"username": "tester", "password": "test123"})
    assert_status(resp, 200)
    assert_code(resp, 0)
    assert_subset(resp, {"username": "tester", "expiresIn": 7200}, path="data")
    token = resp.get("data.token")
    assert_true("token 已下发", bool(token), "token=%s" % token)
    assert_status(api.get("/api/health"), 200)


@pytest.mark.regression
def test_login_wrong_password(api):
    """异常：密码错误返回 401 / 1001，且不泄露账号是否存在。"""
    resp = api.post("/api/auth/login", auth=False,
                    json_body={"username": "tester", "password": "wrong-password"})
    assert_status(resp, 401)
    assert_code(resp, 1001)
    assert_text_contains(resp, "错误")


@pytest.mark.regression
@pytest.mark.parametrize("body", [{"username": "tester"}, {"password": "test123"}, {}],
                         ids=["缺密码", "缺用户名", "参数全缺"])
def test_login_missing_params(api, body):
    """异常：入参缺失走参数校验分支（400 / 1003），不能当作登录成功。"""
    resp = api.post("/api/auth/login", auth=False, json_body=body)
    assert_status(resp, 400)
    assert_code(resp, 1003)


@pytest.mark.smoke
def test_order_without_token(api):
    """越权保护：未登录访问受保护接口返回 401 / 1002。"""
    resp = api.post("/api/seckill/order", auth=False, json_body={"activityId": 1})
    assert_status(resp, 401)
    assert_code(resp, 1002)


@pytest.mark.regression
def test_order_with_forged_token(api):
    """伪造 token 必须被拒绝（拦截器校验 token 是否在服务端有效）。"""
    resp = api.post("/api/seckill/order", auth=False,
                    headers={"Authorization": "Bearer tk_forged_by_tester"},
                    json_body={"activityId": 1})
    assert_status(resp, 401)
    assert_code(resp, 1002)


@pytest.mark.skip(reason="已知风险：订单详情只校验登录态、未校验订单归属，"
                         "已在 docs/测试方案.md「风险与待确认项」登记，待产品确认后补用例")
def test_order_detail_ownership(api):
    """越权：A 用户不应能查询 B 用户的订单详情。"""
    other = api.clone()
    other.login("u_other", "test123")
    order = other.post("/api/seckill/order", json_body={"activityId": 1})
    order_no = order.get("data.orderNo")
    resp = api.get("/api/order/%s" % order_no)
    assert_status(resp, 403)
