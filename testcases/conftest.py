# -*- coding: utf-8 -*-
"""用例公共夹具：拉起被测服务、准备 HTTP 客户端与数据库连接、用例级数据复位。

夹具层级（scope）：sut（会话）→ api / db（会话）→ case_context（用例）。
把「环境准备」全部收在夹具里，用例只写业务断言。
"""
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest
import requests
from werkzeug.serving import make_server

from framework.assertions import AssertionContext, bind, reset
from framework.conf import get_config
from framework.db import DBClient
from framework.http_client import ApiClient
from framework.logger import banner, get_logger
from service.app import create_app

log = get_logger("atf.fixture")


def free_port():
    """让操作系统分配一个空闲端口，避免本地/CI 端口冲突。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class SutServer:
    """在被测服务上起一个真实 HTTP 服务（threaded），用例通过接口访问它。"""

    def __init__(self, db_path, host, port, limit):
        self.app = create_app(db_path, limit)
        self.host = host
        self.port = port or free_port()
        self.server = make_server(self.host, self.port, self.app, threaded=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.timeout = float(get_config().get("sut.startup_timeout", 20))

    @property
    def base_url(self):
        return "http://%s:%d" % (self.host, self.port)

    def start(self):
        self.thread.start()
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                if requests.get(self.base_url + "/api/health", timeout=1).status_code == 200:
                    log.info("被测服务已就绪：%s", self.base_url)
                    return self
            except requests.RequestException:
                time.sleep(0.1)
        raise RuntimeError("被测服务启动超时：%s" % self.base_url)

    def stop(self):
        self.server.shutdown()
        self.thread.join(timeout=5)


@pytest.fixture(scope="session")
def sut(tmp_path_factory):
    """会话级夹具：自动拉起被测服务（每个会话一个独立临时库）。

    sut.auto_start=false 时直接使用 config/env/<env>.yaml 里配置的 base_url（真实环境回归）。
    """
    config = get_config()
    banner("准备被测环境（env=%s）" % config.env)
    server = None
    if config.get("sut.auto_start", True):
        db_path = config.get("sut.db") or str(Path(tempfile.mkdtemp(prefix="atf_sut_")) / "seckill.db")
        server = SutServer(db_path, config.get("sut.host", "127.0.0.1"),
                           int(config.get("sut.port", 0)),
                           config.get("sut.limit_per_user_per_second", 3)).start()
        base_url, db_path = server.base_url, db_path
    else:
        base_url = config.get("http.base_url")
        db_path = config.get("database.path")
        log.info("使用已有环境：%s（数据库 %s）", base_url, db_path)
    yield {"base_url": base_url, "db_path": db_path}
    if server is not None:
        server.stop()
        log.info("被测服务已停止")


@pytest.fixture(scope="session")
def api(sut):
    """会话级 HTTP 客户端：统一 base_url / 超时 / 重试 / token。"""
    return ApiClient(base_url=sut["base_url"])


@pytest.fixture(scope="session")
def db(sut):
    """数据库客户端：用于「接口 + 数据库」双端校验。"""
    driver = get_config().get("database.driver", "sqlite")
    client = DBClient(driver="sqlite", path=sut["db_path"]) if driver == "sqlite" else DBClient()
    yield client
    client.close()


@pytest.fixture(autouse=True)
def case_context(request, api, sut):
    """每个用例一个执行上下文：数据复位 + 登录 + 断言/请求流水记录（报告层消费）。"""
    config = get_config()
    ctx = AssertionContext(nodeid=request.node.nodeid, name=request.node.name)
    request.node._atf_context = ctx
    token = bind(ctx)

    stock = config.get("concurrency.stock", 10)
    api.clear_token()
    reset_resp = api.post("/api/admin/reset", auth=False, retry=0, json_body={"stock": stock})
    assert reset_resp.code() == 0, "用例前置失败：数据复位 %s" % reset_resp.body_preview(200)
    login_resp = api.login(config.get("auth.username", "tester"),
                           config.get("auth.password", "test123"))
    assert login_resp.code() == 0, "用例前置失败：登录 %s" % login_resp.body_preview(200)
    ctx.add("用例前置", True, "数据复位（stock=%s）+ 登录成功" % stock)

    yield ctx
    reset(token)
