# -*- coding: utf-8 -*-
"""协议层：统一 HTTP 客户端（会话复用、超时、重试、耗时统计、请求日志、自动带 token）。"""
import time

import requests
from requests.adapters import HTTPAdapter

from framework.conf import get_config
from framework.logger import get_logger
from framework.utils import dig

log = get_logger("atf.http")
SENSITIVE = ("authorization", "token", "password")


class ApiError(Exception):
    """网络层错误（连接失败 / 超时 / 重试耗尽），区别于业务断言失败。"""


class ApiResponse:
    """requests.Response 的封装：附带耗时与请求快照，支持点号取值。"""

    def __init__(self, response, elapsed_ms, request_info):
        self.status_code = response.status_code
        self.headers = dict(response.headers)
        self.text = response.text
        self.elapsed_ms = round(elapsed_ms, 2)
        self.request = request_info
        self.url = response.url
        try:
            self.json = response.json()
        except ValueError:
            self.json = None

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def code(self):
        return dig(self.json, "code")

    def message(self):
        return dig(self.json, "message")

    def data(self):
        return dig(self.json, "data")

    def get(self, path, default=None):
        return dig(self.json, path, default)

    def brief(self):
        return "%s %s -> %s (%sms)" % (self.request["method"], self.request["path"],
                                       self.status_code, self.elapsed_ms)

    def body_preview(self, limit=2000):
        if self.text is None:
            return ""
        return self.text if len(self.text) <= limit else self.text[:limit] + "...(已截断)"

    def __repr__(self):
        return "<ApiResponse %s code=%s %sms>" % (self.status_code, self.code(), self.elapsed_ms)


def _adapter():
    """连接池参数：并发用例（200 线程）下复用连接、避免重复握手。"""
    return HTTPAdapter(pool_connections=32, pool_maxsize=256, max_retries=0)



class ApiClient:
    """一个实例 = 一个会话：复用连接、统一 token、统一超时与重试策略。"""

    def __init__(self, base_url=None, timeout=None, retries=None, token=None):
        cfg = get_config()
        self.cfg = cfg
        self.base_url = (base_url or cfg.get("http.base_url", "http://127.0.0.1:8899")).rstrip("/")
        self.timeout = float(timeout or cfg.get("http.timeout", 10))
        self.retries = int(cfg.get("http.retries", 2) if retries is None else retries)
        self.retry_on_status = tuple(cfg.get("http.retry_on_status", [502, 503, 504]))
        self.backoff = float(cfg.get("http.backoff", 0.3))
        self.verify_ssl = bool(cfg.get("http.verify_ssl", False))
        self.token = token
        self.exchanges = []                 # 请求 - 响应流水，供报告层使用
        self.session = requests.Session()
        self.session.mount("http://", _adapter())
        self.session.mount("https://", _adapter())

    # ------------------------------------------------------------ 会话
    def set_token(self, token):
        self.token = token
        return self

    def clear_token(self):
        self.token = None
        return self

    def clone(self):
        """复制一个同配置的新会话（并发用例里模拟多个用户）。"""
        return ApiClient(self.base_url, self.timeout, self.retries, token=self.token)

    def trace(self):
        return list(self.exchanges)

    def reset_trace(self):
        self.exchanges = []

    # ------------------------------------------------------------ 核心请求
    def request(self, method, path, params=None, json_body=None, data=None, headers=None,
                auth=True, timeout=None, retry=None):
        method = method.upper()
        url = path if str(path).startswith("http") else self.base_url + path
        request_headers = {"Accept": "application/json"}
        if json_body is not None:
            request_headers["Content-Type"] = "application/json"
        if auth and self.token:
            request_headers["Authorization"] = "Bearer %s" % self.token
        request_headers.update(headers or {})
        info = {
            "method": method,
            "path": path,
            "url": url,
            "params": params or {},
            "body": json_body if json_body is not None else data,
            "headers": {k: ("***" if k.lower() in SENSITIVE else v)
                        for k, v in request_headers.items()},
        }
        log.debug("请求 %s %s params=%s body=%s", method, url, info["params"], info["body"])

        attempts = self.retries if retry is None else int(retry)
        last_error = None
        for attempt in range(attempts + 1):
            started = time.perf_counter()
            try:
                response = self.session.request(
                    method, url, params=params, json=json_body, data=data,
                    headers=request_headers, timeout=timeout or self.timeout,
                    verify=self.verify_ssl)
            except requests.RequestException as exc:
                last_error = exc
                log.warning("网络异常（第 %d/%d 次）%s %s -> %s",
                            attempt + 1, attempts + 1, method, url, exc)
                if attempt < attempts:
                    time.sleep(self.backoff * (2 ** attempt))
                    continue
                raise ApiError("请求失败 %s %s：%s" % (method, url, exc)) from exc

            elapsed_ms = (time.perf_counter() - started) * 1000
            wrapped = ApiResponse(response, elapsed_ms, info)
            if response.status_code in self.retry_on_status and attempt < attempts:
                log.warning("服务端返回 %s，第 %d/%d 次重试：%s",
                            response.status_code, attempt + 1, attempts + 1, url)
                time.sleep(self.backoff * (2 ** attempt))
                continue

            self.exchanges.append(wrapped)
            _record_exchange(wrapped)
            log.info("%s %s -> %s code=%s %sms", method, path, wrapped.status_code,
                     wrapped.code(), wrapped.elapsed_ms)
            return wrapped
        raise ApiError("请求失败 %s %s：%s" % (method, url, last_error))

    # ------------------------------------------------------------ 便捷方法
    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)

    def put(self, path, **kwargs):
        return self.request("PUT", path, **kwargs)

    def delete(self, path, **kwargs):
        return self.request("DELETE", path, **kwargs)

    def login(self, username="tester", password="test123", **kwargs):
        """登录并自动保存 token，后续请求默认带上。"""
        resp = self.request("POST", "/api/auth/login", auth=False,
                            json_body={"username": username, "password": password}, **kwargs)
        token = resp.get("data.token")
        if token:
            self.set_token(token)
        return resp


def _record_exchange(response):
    """把请求流水写入当前用例上下文（断言层维护），供 HTML 报告展示。"""
    try:
        from framework import assertions
        ctx = assertions.current(create=False)
    except Exception:                       # noqa: BLE001 - 报告属于旁路，失败不影响用例
        return
    if ctx is not None:
        ctx.add_exchange(response)
