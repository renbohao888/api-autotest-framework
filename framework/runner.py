# -*- coding: utf-8 -*-
"""通用用例执行器：把 YAML 里描述的「前置步骤 + 请求 + 期望结果」跑起来。

支持三件事，让「新增覆盖只改 YAML」成立：
    1. setup：前置步骤（准备数据 / 造出特定状态），可指定 user 用不同账号执行；
    2. extract：从响应中提取变量（如 orderNo），供后续步骤与主请求用 ${变量} 引用；
    3. 断言：状态码 / 业务码 / 字段子集 / 提示语 / 耗时阈值，全部由 YAML 描述。
"""
import re

from framework.assertions import (assert_code, assert_elapsed, assert_field,
                                  assert_not_none, assert_status, assert_subset,
                                  assert_text_contains)
from framework.conf import get_config
from framework.logger import get_logger

log = get_logger("atf.runner")

REQUEST_KEYS = ("params", "json", "data", "headers", "auth", "timeout", "retry")
VAR_PATTERN = re.compile(r"\$\{(\w+)\}")


# ------------------------------------------------------------------ 变量渲染
def render(node, variables):
    """把 ${var} 替换成运行时变量（支持嵌套 dict / list），例如 /api/order/${order_no}/pay。"""
    if isinstance(node, str):
        return VAR_PATTERN.sub(lambda m: str(variables.get(m.group(1), m.group(0))), node)
    if isinstance(node, dict):
        return {key: render(value, variables) for key, value in node.items()}
    if isinstance(node, list):
        return [render(item, variables) for item in node]
    return node


def build_request(step, variables=None):
    """把用例里的请求描述转成 ApiClient.request 的入参。"""
    variables = variables or {}
    options = {}
    for key in REQUEST_KEYS:
        if step.get(key) is not None:
            options[key] = render(step[key], variables)
    if "json" in options:
        options["json_body"] = options.pop("json")
    options.setdefault("auth", True)
    return options


# ------------------------------------------------------------------ 执行
def client_for(api, username=None):
    """取执行该步骤的客户端：不指定用户就复用当前会话，指定则换账号登录。"""
    if not username:
        return api
    config = get_config()
    client = api.clone()
    client.login(username, config.get("auth.password", "test123"))
    return client


def run_setup(api, case, variables):
    """执行前置步骤并提取变量（不返回响应，只保证前置状态正确）。"""
    for step in case.get("setup") or []:
        client = client_for(api, step.get("user"))
        resp = client.request(step.get("method", "POST"), render(step["path"], variables),
                             **build_request(step, variables))
        assert_status(resp, step.get("expect_status", 200),
                      name="前置步骤 %s 状态码" % step["path"])
        if "expect_code" in step:
            assert_code(resp, step["expect_code"], name="前置步骤 %s 业务码" % step["path"])
        for name, path in (step.get("extract") or {}).items():
            value = resp.get(path)
            if value is None:
                raise AssertionError("前置步骤 %s 未取到变量 %s（path=%s）"
                                     % (step["path"], name, path))
            variables[name] = value
            log.debug("提取变量 %s = %s", name, value)
    return variables


def run_case(api, case, variables=None):
    """执行一条 YAML 用例：前置步骤 -> 主请求 -> 逐项断言 -> 返回响应对象。"""
    variables = run_setup(api, case, dict(variables or {}))
    payload = build_request(case, variables)
    path = render(case["path"], variables)
    log.debug("执行用例 %s：%s %s", case_id_of(case), case.get("method", "GET"), path)
    resp = api.request(case.get("method", "GET"), path, **payload)

    assert_status(resp, case.get("expect_status", 200), name="%s 状态码" % path)
    if "expect_code" in case:
        assert_code(resp, case["expect_code"])
    if case.get("expect_message_contains"):
        assert_text_contains(resp, case["expect_message_contains"], name="提示语包含关键字")
    if "expect_data" in case:
        assert_subset(resp, case["expect_data"])
    for path in case.get("expect_not_none") or []:
        assert_not_none(resp, path)
    for item in case.get("expect_fields") or []:
        assert_field(resp, item["path"], item["value"], name=item.get("name"))
    if case.get("max_elapsed_ms"):
        assert_elapsed(resp, case["max_elapsed_ms"])
    return resp


def case_id_of(case):
    return str(case.get("id") or case.get("name"))

