# -*- coding: utf-8 -*-
"""性能门禁用例（默认不执行，用 -m perf 触发）。

两条门禁：
    1. 单接口 P95 响应时间（稳定性）；2. 并发吞吐 QPS 与错误率（容量）。
阈值全部来自 config/*.yaml，可按环境调整，避免在共享 CI runner 上误报。
"""
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from framework.assertions import assert_true
from framework.conf import get_config
from framework.logger import banner, get_logger
from framework.utils import percentile

log = get_logger("atf.case.perf")


@pytest.mark.perf
def test_activity_query_p95(api):
    """查询接口连续 100 次调用，P95 耗时必须低于阈值（性能基线回归）。"""
    config = get_config()
    limit = float(config.get("perf.p95_ms", 800))
    samples = []
    for _ in range(100):
        samples.append(api.get("/api/activity/1").elapsed_ms)
    average = sum(samples) / len(samples)
    p95 = percentile(samples, 0.95)
    log.info("查询接口耗时：平均 %.1fms ｜ P95 %.1fms ｜ 最大 %.1fms",
             average, p95, max(samples))
    assert_true("P95 ≤ %.0fms（实际 %.1fms）" % (limit, p95), p95 <= limit)
    assert_true("平均耗时 ≤ %.0fms（实际 %.1fms）" % (limit / 2, average), average <= limit / 2)


@pytest.mark.perf
@pytest.mark.slow
def test_concurrent_throughput_and_error_rate(api):
    """50 并发 × 20 次查询：QPS 必须达标且错误率为 0（容量门禁）。"""
    banner("性能用例：并发吞吐与错误率")
    config = get_config()
    min_qps = float(config.get("perf.min_qps", 50))
    error_limit = float(config.get("perf.error_rate", 0.01))
    workers, per_worker = 50, 20
    clients = [api.clone() for _ in range(workers)]

    def hammer(client):
        errors = 0
        for _ in range(per_worker):
            try:
                if client.get("/api/activity/1").status_code != 200:
                    errors += 1
            except Exception:                      # noqa: BLE001 - 统计错误率
                errors += 1
        return errors

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        errors = sum(pool.map(hammer, clients))
    seconds = time.perf_counter() - started
    total = workers * per_worker
    qps = total / seconds if seconds else 0
    error_rate = errors / float(total)
    log.info("并发压测：%d 请求 / %.2fs → QPS≈%.0f ｜ 错误率 %.2f%%",
             total, seconds, qps, error_rate * 100)

    assert_true("错误率 ≤ %.2f%%" % (error_limit * 100),
                error_rate <= error_limit, "实际错误率 %.2f%%" % (error_rate * 100))
    assert_true("QPS ≥ %.0f" % min_qps, qps >= min_qps, "实际 QPS≈%.0f" % qps)
