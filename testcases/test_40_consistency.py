# -*- coding: utf-8 -*-
"""并发与数据一致性用例：多人抢购、单人幂等、取消/超时回补库存。

这一类用例是测试开发的核心竞争力：接口层的「返回码」正确不代表数据正确，
必须用「接口 + 数据库」双端校验 + 不变量校验，才能验证并发下的最终一致性。
"""
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from framework.assertions import (assert_db_count, assert_db_value, assert_equal,
                                  assert_eventually, assert_field, assert_true)
from framework.conf import get_config
from framework.logger import banner, get_logger

log = get_logger("atf.case.concurrency")

PASSWORD = "test123"


def login_batch(api, count):
    """并发登录 count 个不同用户，返回各自的客户端（模拟真实的不同用户）。"""
    def login(index):
        client = api.clone()
        client.login("u_%03d" % index, PASSWORD)
        return client

    with ThreadPoolExecutor(max_workers=min(count, 50)) as pool:
        return list(pool.map(login, range(1, count + 1)))


def seckill_batch(clients, activity_id=1):
    """并发下单，返回 (响应列表, 总耗时秒)。"""
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        results = list(pool.map(
            lambda client: client.post("/api/seckill/order", retry=0,
                                       json_body={"activityId": activity_id}),
            clients))
    return results, time.perf_counter() - started


@pytest.mark.concurrency
@pytest.mark.slow
def test_200_users_race_for_limited_stock(api, db):
    """200 个用户并发抢 10 件：成功数必须等于库存数，不超卖、不出现负数库存。"""
    banner("并发用例：200 用户抢 10 件库存")
    config = get_config()
    workers = int(config.get("concurrency.workers", 200))
    stock = int(config.get("concurrency.stock", 10))
    api.post("/api/admin/reset", auth=False, retry=0, json_body={"stock": stock})

    clients = login_batch(api, workers)
    results, seconds = seckill_batch(clients)
    codes = [resp.code() for resp in results]
    success, sold_out = codes.count(0), codes.count(1007)
    others = sorted({code for code in codes if code not in (0, 1007)})
    qps = len(results) / seconds if seconds else 0
    log.info("并发结果：成功 %d ｜ 售罄 %d ｜ 其它 %s ｜ 耗时 %.2fs ｜ QPS≈%.0f",
             success, sold_out, others, seconds, qps)

    # 1) 接口侧断言
    assert_equal("成功下单数 = 库存数（不超卖）", success, stock)
    assert_equal("被拦截数 = 总请求数 - 库存数", sold_out, workers - stock)
    assert_equal("无其它异常响应（服务不报错、不超时）", others, [])
    assert_true("并发确实打满（QPS > 0）", qps > 0, "QPS≈%.0f，耗时 %.2fs" % (qps, seconds))

    # 2) 数据库侧断言（双端校验：接口说成功，库里必须真扣减、真落单）
    assert_db_value(db, "SELECT stock FROM activity WHERE activity_id=1", (), 0)
    assert_db_count(db, "orders", "activity_id=1 AND status='WAIT_PAY'", (), stock)
    assert_eventually(
        lambda: (db.count("orders", "activity_id=1") == stock,
                 "订单数 %s，库存 %s" % (db.count("orders", "activity_id=1"),
                                         db.scalar("SELECT stock FROM activity WHERE activity_id=1"))),
        name="订单数与库存最终一致（不变量：库存 + 有效订单数 = 初始库存）", timeout=3)

    # 3) 内部接口侧断言（与库内数据互相印证）
    admin = api.get("/api/admin/stock/1", auth=False)
    assert_field(admin, "data.stock", 0)
    assert_field(admin, "data.orders", stock)


@pytest.mark.concurrency
def test_same_user_concurrent_orders_only_one_success(api, db):
    """同一用户在同一秒内并发下单：只能成功 1 单（幂等），库存只扣 1。"""
    stock = 10
    api.post("/api/admin/reset", auth=False, retry=0, json_body={"stock": stock})
    api.login("tester", PASSWORD)
    clients = [api.clone() for _ in range(8)]        # 同一 token 的 8 个并发会话

    results, seconds = seckill_batch(clients)
    codes = [resp.code() for resp in results]
    success = codes.count(0)
    duplicated = codes.count(1009)
    limited = codes.count(1008)
    others = sorted({code for code in codes if code not in (0, 1009, 1008)})
    log.info("单人并发结果：成功 %d ｜ 重复下单 %d ｜ 限流 %d ｜ 其它 %s（%.2fs）",
             success, duplicated, limited, others, seconds)

    assert_equal("同一用户只允许成功 1 单（幂等）", success, 1)
    assert_equal("其余请求只能是「重复下单」或「限流」", success + duplicated + limited, len(codes))
    assert_equal("无其它异常响应", others, [])
    assert_db_value(db, "SELECT stock FROM activity WHERE activity_id=1", (), stock - 1)
    assert_db_count(db, "orders", "activity_id=1 AND user_id='tester'", (), 1)


@pytest.mark.concurrency
def test_stock_restore_invariant(api, db):
    """5 人下单后取消 2 单、超时关单 1 单：库存必须回补，且「库存 + 有效订单数」守恒。"""
    initial_stock = 10
    api.post("/api/admin/reset", auth=False, retry=0, json_body={"stock": initial_stock})
    clients = login_batch(api, 5)
    orders = [client.post("/api/seckill/order", retry=0,
                          json_body={"activityId": 1}).get("data.orderNo")
              for client in clients]
    assert_true("5 单全部下单成功", all(orders), "订单号：%s" % orders)

    clients[0].post("/api/order/%s/cancel" % orders[0], retry=0)
    clients[1].post("/api/order/%s/cancel" % orders[1], retry=0)
    clients[2].post("/api/admin/expire/%s" % orders[2], auth=False, retry=0)

    valid_orders = db.count("orders", "activity_id=1 AND status IN ('WAIT_PAY','PAID')")
    current_stock = db.scalar("SELECT stock FROM activity WHERE activity_id=1")
    assert_db_value(db, "SELECT stock FROM activity WHERE activity_id=1", (), 8)
    assert_db_count(db, "orders", "activity_id=1 AND status='CANCELED'", (), 2)
    assert_db_count(db, "orders", "activity_id=1 AND status='TIMEOUT_CLOSED'", (), 1)
    assert_equal("不变量：当前库存 + 有效订单数 = 初始库存",
                 current_stock + valid_orders, initial_stock)
