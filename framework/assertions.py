# -*- coding: utf-8 -*-
"""断言层：把测试结论变成结构化记录，失败信息带「断言项 / 期望 / 实际」。

设计要点
1. 每个用例绑定一个 AssertionContext（contextvars）：断言记录 + 请求流水都写进去，
   报告层直接消费，不需要在每个用例里手写日志。
2. 失败即抛 AssertionError，消息里带差异明细，做到「看到失败信息就知道去哪里查」。
3. 本层不依赖 pytest，可在 pytest / 脚本 / CI / 定时任务中复用。
"""
import contextvars
import time

from framework.utils import deep_contains, dig

_ctx = contextvars.ContextVar("atf_assertion_context", default=None)


class AssertionRecord:
    __slots__ = ("name", "ok", "detail")

    def __init__(self, name, ok, detail=""):
        self.name = name
        self.ok = ok
        self.detail = detail

    def as_dict(self):
        return {"name": self.name, "ok": self.ok, "detail": self.detail}

    def __str__(self):
        return "%s %s%s" % ("[PASS]" if self.ok else "[FAIL]", self.name,
                            ("：" + self.detail) if self.detail else "")


class AssertionContext:
    """一个用例的执行上下文：断言记录 + 请求流水 + 耗时。"""

    def __init__(self, nodeid="", name=""):
        self.nodeid = nodeid
        self.name = name
        self.records = []
        self.exchanges = []
        self.started = time.time()

    def add(self, name, ok, detail=""):
        record = AssertionRecord(name, ok, detail)
        self.records.append(record)
        return record

    def add_exchange(self, response):
        self.exchanges.append({
            "request": response.request,
            "status": response.status_code,
            "code": response.code(),
            "elapsed_ms": response.elapsed_ms,
            "body": response.body_preview(),
        })

    @property
    def failed(self):
        return [r for r in self.records if not r.ok]

    @property
    def elapsed_ms(self):
        return round((time.time() - self.started) * 1000, 2)

    def as_dict(self):
        return {"nodeid": self.nodeid, "name": self.name, "elapsed_ms": self.elapsed_ms,
                "assertions": [r.as_dict() for r in self.records],
                "exchanges": self.exchanges}


def bind(context):
    """绑定当前用例的上下文，返回 token 供 reset 还原。"""
    return _ctx.set(context)


def current(create=False):
    ctx = _ctx.get()
    if ctx is None and create:
        _ctx.set(AssertionContext())
        return _ctx.get()
    return ctx


def reset(token=None):
    if token is None:
        _ctx.set(None)
    else:
        _ctx.reset(token)


def _pass(name, detail=""):
    ctx = current()
    if ctx is not None:
        ctx.add(name, True, detail)
    return True


def _fail(name, detail):
    ctx = current()
    if ctx is not None:
        ctx.add(name, False, detail)
    raise AssertionError("[%s] %s" % (name, detail))



# ------------------------------------------------------------------ 接口断言
def assert_status(resp, expected=200, name=None):
    """HTTP 状态码断言，expected 支持单个值或集合。"""
    name = _name(name, "HTTP 状态码")
    actual = resp.status_code
    if isinstance(expected, (list, tuple, set)):
        ok = actual in expected
        expect_text = "属于 %s" % list(expected)
    else:
        ok = actual == expected
        expect_text = str(expected)
    if ok:
        return _pass(name, resp.brief())
    _fail(name, "%s：期望 %s，实际 %s；响应体 %s"
          % (resp.brief(), expect_text, actual, resp.body_preview(300)))


def assert_code(resp, expected=0, path="code", name=None):
    """业务状态码断言（响应体里的 code 字段）。"""
    name = _name(name, "业务状态码")
    actual = dig(resp.json, path)
    if actual == expected:
        return _pass(name, "code=%s，message=%s" % (actual, resp.message()))
    _fail(name, "%s：期望 code=%s，实际 code=%s（message=%s）；响应体 %s"
          % (resp.brief(), expected, actual, resp.message(), resp.body_preview(300)))


def assert_subset(resp, expected, path="data", name=None):
    """深度部分匹配：只校验期望中出现的字段，返回全部差异明细。"""
    name = _name(name, "响应字段子集")
    actual = dig(resp.json, path)
    diffs = deep_contains(actual, expected)
    if not diffs:
        return _pass(name, "共校验 %d 个字段/元素，全部匹配" % _count_leaves(expected))
    _fail(name, "%s：以下字段不符合预期（%s）：\n    - %s"
          % (resp.brief(), path or "$", "\n    - ".join(diffs)))


def assert_field(resp, path, expected, name=None):
    """按点号路径断言单个字段值，例如 data.stock。"""
    name = _name(name, "字段 %s" % path)
    actual = dig(resp.json, path, "<不存在>")
    if actual == expected:
        return _pass(name, "%s = %r" % (path, actual))
    _fail(name, "%s：期望 %s=%r，实际 %r；响应体 %s"
          % (resp.brief(), path, expected, actual, resp.body_preview(300)))


def assert_not_none(resp, path, name=None):
    name = _name(name, "字段 %s 非空" % path)
    actual = dig(resp.json, path)
    if actual is not None and actual != "":
        return _pass(name, "%s = %r" % (path, actual))
    _fail(name, "%s：期望 %s 有值，实际 %r" % (resp.brief(), path, actual))


def assert_type(resp, path, types, name=None):
    """类型断言，types 支持类型或类型元组，例如 (int, float)。"""
    name = _name(name, "字段 %s 类型" % path)
    actual = dig(resp.json, path)
    expected_types = types if isinstance(types, tuple) else (types,)
    if isinstance(actual, bool) and bool not in expected_types:
        ok = False
    else:
        ok = isinstance(actual, expected_types)
    if ok:
        return _pass(name, "%s 是 %s" % (path, actual.__class__.__name__))
    _fail(name, "%s：期望 %s 为 %s，实际 %r（%s）"
          % (resp.brief(), path, _type_names(expected_types), actual, type(actual).__name__))


def assert_range(resp, path, low=None, high=None, name=None):
    """数值范围断言（含边界），常用于「库存不为负」这类校验。"""
    name = _name(name, "字段 %s 范围" % path)
    actual = dig(resp.json, path)
    ok = isinstance(actual, (int, float)) and not isinstance(actual, bool)
    if ok and low is not None:
        ok = actual >= low
    if ok and high is not None:
        ok = actual <= high
    expect = "%s%s%s" % (low if low is not None else "-∞", " ≤ 值 ≤ ",
                         high if high is not None else "+∞")
    if ok:
        return _pass(name, "%s = %r 满足 %s" % (path, actual, expect))
    _fail(name, "%s：期望 %s 满足 %s，实际 %r" % (resp.brief(), path, expect, actual))


def assert_text_contains(resp, keyword, name=None):
    name = _name(name, "响应文本包含 %r" % keyword)
    if keyword in (resp.text or ""):
        return _pass(name, "命中关键字")
    _fail(name, "%s：响应体未包含 %r" % (resp.brief(), keyword))


def assert_elapsed(resp, max_ms, name=None):
    """单接口耗时断言（性能基线）。"""
    name = _name(name, "接口耗时 ≤ %sms" % max_ms)
    if resp.elapsed_ms <= max_ms:
        return _pass(name, "%s 耗时 %sms" % (resp.request["path"], resp.elapsed_ms))
    _fail(name, "%s：耗时 %sms 超过阈值 %sms" % (resp.brief(), resp.elapsed_ms, max_ms))


# ------------------------------------------------------------------ 数据库断言
def assert_db_value(db, sql, params, expected, name=None):
    name = _name(name, "数据库校验")
    actual = db.scalar(sql, params, default="<无结果>")
    if actual == expected:
        return _pass(name, "实际 %r" % (actual,))
    _fail(name, "期望 %r，实际 %r（SQL：%s，params=%s）" % (expected, actual, sql, params))


def assert_db_count(db, table, where="", params=(), expected=0, name=None):
    name = _name(name, "表 %s 记录数" % table)
    actual = db.count(table, where, params)
    if actual == expected:
        return _pass(name, "%s 共 %d 条" % (table, actual))
    _fail(name, "期望 %s 记录数 %d，实际 %d（where=%s）" % (table, expected, actual, where))


def assert_eventually(probe, name=None, timeout=3.0, interval=0.1, message=""):
    """最终一致断言：probe() 返回 (bool, 描述)，轮询到满足为止。

    典型场景：取消订单后库存回补、异步补偿、缓存删除等「短暂不一致」。
    """
    name = _name(name, "最终一致")
    deadline = time.time() + timeout
    last = (False, "未执行")
    while True:
        last = probe()
        if last[0]:
            return _pass(name, last[1] or message)
        if time.time() >= deadline:
            break
        time.sleep(interval)
    _fail(name, "%.1fs 内未达成最终一致：%s %s" % (timeout, last[1], message))


def assert_db_eventually(db, sql, params, expected, timeout=3.0, interval=0.1, name=None):
    """数据库侧的最终一致断言（异步扣减 / 补偿场景）。"""
    def probe():
        actual = db.scalar(sql, params, default="<无结果>")
        return actual == expected, "实际 %r（期望 %r）" % (actual, expected)

    return assert_eventually(probe, name=_name(name, "数据库最终一致"),
                             timeout=timeout, interval=interval)


# ------------------------------------------------------------------ 内部工具
def _count_leaves(node):
    if isinstance(node, dict):
        return sum(_count_leaves(v) for v in node.values())
    if isinstance(node, list):
        return sum(_count_leaves(v) for v in node)
    return 1


def _type_names(types):
    return "/".join(t.__name__ for t in types)


def _name(name, default):
    return name or default



# ------------------------------------------------------------------ 通用断言
def assert_equal(name, actual, expected):
    """聚合结果的断言（并发统计、性能指标等非响应体场景）。"""
    if actual == expected:
        return _pass(name, "实际 %r" % (actual,))
    _fail(name, "期望 %r，实际 %r" % (expected, actual))


def assert_true(name, condition, detail=""):
    """布尔断言：condition 为假即失败，detail 用于输出实际值。"""
    if condition:
        return _pass(name, detail)
    _fail(name, detail or "条件不成立")
