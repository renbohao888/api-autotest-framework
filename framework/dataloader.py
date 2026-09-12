# -*- coding: utf-8 -*-
"""用例数据层：YAML 用例 -> pytest 参数化（用例与代码分离）。

data/*.yaml 的格式：

    group: seckill_order
    cases:
      - id: SEC-001
        name: 库存充足时下单成功
        method: POST
        path: /api/seckill/order
        auth: true
        json: {activityId: 1}
        expect_status: 200
        expect_code: 0
        expect_data: {status: WAIT_PAY}
        tags: [regression, seckill]

必填字段只有 id / name / path；expect_* 字段按需使用。
"""
from pathlib import Path

import pytest
import yaml

from framework.conf import root_path

KNOWN_MARKS = ("smoke", "regression", "concurrency", "perf", "slow")
REQUIRED = ("id", "name", "path")


def load_cases(path, group=None, tags=None):
    """读取用例文件并按分组 / 标签过滤，同时做字段校验（尽早暴露写错的 YAML）。"""
    node = Path(path)
    if not node.is_absolute():
        node = root_path(path)
    if not node.exists():
        raise FileNotFoundError("用例文件不存在：%s" % node)

    data = yaml.safe_load(node.read_text(encoding="utf-8")) or {}
    if isinstance(data, dict):
        cases = data.get("cases") or []
        file_group = data.get("group")
    else:
        cases, file_group = data, None

    for case in cases:
        missing = [key for key in REQUIRED if not case.get(key)]
        if missing:
            raise ValueError("用例 %r 缺少必填字段 %s" % (case.get("name", case), missing))

    if group:
        cases = [c for c in cases if c.get("group", file_group) == group]
    if tags:
        wanted = set(tags)
        cases = [c for c in cases if wanted & set(c.get("tags") or [])]
    return cases


def case_id(case):
    return str(case.get("id") or case["name"])


def marks_of(case):
    """把用例标签里「框架已注册的标记」转成 pytest mark，避免 strict-markers 报错。"""
    return [getattr(pytest.mark, tag) for tag in (case.get("tags") or []) if tag in KNOWN_MARKS]


def params(path, group=None, tags=None):
    """生成 pytest.param 列表，用例 id 直接用作报告里的用例编号。"""
    result = []
    for case in load_cases(path, group=group, tags=tags):
        marks = marks_of(case)
        if case.get("skip"):
            marks.append(pytest.mark.skip(reason=case.get("skip_reason") or "用例标记为 skip"))
        result.append(pytest.param(case, id=case_id(case), marks=marks))
    return result
