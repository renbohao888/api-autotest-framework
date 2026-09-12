# -*- coding: utf-8 -*-
"""被测服务（SUT）的 HTTP 入口：Flask + 统一响应结构。

统一响应：{"code": 0, "message": "success", "data": {...}, "traceId": "..."}
业务失败也返回结构化 JSON（HTTP 状态码与业务码解耦），便于用例同时校验「协议层 + 业务层」。

这是「被测系统」，不是框架的一部分：它提供与真实业务一致的规则，
测试框架（framework/）与用例（testcases/）对它是黑盒 + 白盒（数据库）双重校验。
"""
import uuid

from flask import Flask, jsonify, request

from service.store import (CODE_BAD_CREDENTIAL, CODE_OK, CODE_ORDER_NOT_FOUND,
                           CODE_PARAM_MISSING, CODE_RATE_LIMITED, CODE_UNAUTHORIZED,
                           BusinessError, SqliteStore)

TEST_PASSWORD = "test123"


def _ok(data=None, message="success"):
    return jsonify({"code": CODE_OK, "message": message, "data": data,
                    "traceId": uuid.uuid4().hex[:12]})


def _fail(code, message, http_status=400):
    return jsonify({"code": code, "message": message, "data": None,
                    "traceId": uuid.uuid4().hex[:12]}), http_status


def _order_payload(order):
    return {"orderNo": order["order_no"], "status": order["status"],
            "amount": order["amount"], "activityId": order["activity_id"],
            "userId": order["user_id"], "createdAt": order["created_at"],
            "updatedAt": order["updated_at"]}


def create_app(db_path, limit_per_user_per_second=3):
    """构建 Flask 应用（数据库路径由调用方决定，方便用临时库做隔离）。"""
    app = Flask(__name__)
    app.json.ensure_ascii = False
    store = SqliteStore(db_path, limit_per_user_per_second)
    tokens = {}                     # token -> user_id（内存态，重启即失效）
    app.extensions["store"] = store
    app.extensions["tokens"] = tokens

    def current_user():
        header = request.headers.get("Authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else header.strip()
        if not token or token not in tokens:
            raise BusinessError(CODE_UNAUTHORIZED, "未登录或登录态已失效", 401)
        return tokens[token]

    @app.errorhandler(BusinessError)
    def _handle_business(exc):
        return _fail(exc.code, exc.message, exc.http_status)

    @app.errorhandler(404)
    def _handle_not_found(exc):
        return _fail(404, "接口不存在：%s" % request.path, 404)

    @app.errorhandler(Exception)
    def _handle_error(exc):          # noqa: BLE001 - 兜底，保证响应结构统一
        return _fail(500, "服务内部错误：%s" % exc, 500)


    # ---------------------------------------------------------- 健康检查
    @app.get("/api/health")
    def health():
        store.health()
        return _ok({"status": "UP", "service": "seckill-sut", "version": "1.0.0"})

    # ---------------------------------------------------------- 登录鉴权
    @app.post("/api/auth/login")
    def login():
        body = request.get_json(silent=True) or {}
        username, password = body.get("username"), body.get("password")
        if not username or not password:
            return _fail(CODE_PARAM_MISSING, "用户名或密码不能为空", 400)
        if password != TEST_PASSWORD:
            return _fail(CODE_BAD_CREDENTIAL, "用户名或密码错误", 401)
        token = "tk_" + uuid.uuid4().hex
        tokens[token] = str(username)
        return _ok({"token": token, "username": str(username), "expiresIn": 7200})

    # ---------------------------------------------------------- 活动
    @app.get("/api/activity/<int:activity_id>")
    def activity_detail(activity_id):
        activity = store.get_activity(activity_id)
        if activity is None:
            return _fail(404, "活动不存在", 404)
        return _ok({"activityId": activity_id, "title": activity["title"],
                    "stock": activity["stock"], "price": activity["price"],
                    "status": activity["status"], "startAt": activity["start_at"],
                    "endAt": activity["end_at"]})

    # ---------------------------------------------------------- 秒杀下单
    @app.post("/api/seckill/order")
    def seckill_order():
        user_id = current_user()
        body = request.get_json(silent=True) or {}
        activity_id = body.get("activityId")
        if activity_id is None:
            return _fail(CODE_PARAM_MISSING, "activityId 不能为空", 400)
        try:
            activity_id = int(activity_id)
        except (TypeError, ValueError):
            return _fail(CODE_PARAM_MISSING, "activityId 必须为数字", 400)
        if store.rate_limited(user_id, activity_id):
            return _fail(CODE_RATE_LIMITED, "请求过于频繁，请稍后再试", 429)
        order = store.create_order(activity_id, user_id)
        return _ok({"orderNo": order["order_no"], "status": order["status"],
                    "amount": order["amount"], "remainStock": store.stock_of(activity_id)})

    # ---------------------------------------------------------- 订单与状态机
    @app.get("/api/order/<order_no>")
    def order_detail(order_no):
        current_user()
        order = store.get_order(order_no)
        if order is None:
            return _fail(CODE_ORDER_NOT_FOUND, "订单不存在", 404)
        return _ok(_order_payload(order))

    @app.post("/api/order/<order_no>/pay")
    def order_pay(order_no):
        current_user()
        order = store.pay(order_no)
        return _ok({"orderNo": order["order_no"], "status": order["status"],
                    "remainStock": store.stock_of(order["activity_id"])})

    @app.post("/api/order/<order_no>/cancel")
    def order_cancel(order_no):
        current_user()
        order = store.cancel(order_no)
        return _ok({"orderNo": order["order_no"], "status": order["status"],
                    "remainStock": store.stock_of(order["activity_id"])})

    @app.post("/api/order/<order_no>/complete")
    def order_complete(order_no):
        current_user()
        order = store.complete(order_no)
        return _ok({"orderNo": order["order_no"], "status": order["status"]})

    # ---------------------------------------------------------- 内部接口（测试夹具 / 校验）
    @app.post("/api/admin/expire/<order_no>")
    def admin_expire(order_no):
        """模拟「支付超时关单」：正常应回补库存。"""
        order = store.close_timeout(order_no)
        return _ok({"orderNo": order["order_no"], "status": order["status"],
                    "remainStock": store.stock_of(order["activity_id"])})

    @app.get("/api/admin/stock/<int:activity_id>")
    def admin_stock(activity_id):
        return _ok({"activityId": activity_id, "stock": store.stock_of(activity_id),
                    "orders": store.order_count(activity_id)})

    @app.post("/api/admin/reset")
    def admin_reset():
        body = request.get_json(silent=True) or {}
        activity = store.reset(activity_id=int(body.get("activityId", 1)),
                               stock=int(body.get("stock", 10)),
                               start_offset=int(body.get("startOffset", -60)),
                               end_offset=int(body.get("endOffset", 3600)))
        return _ok({"activityId": activity["activity_id"], "stock": activity["stock"],
                    "startAt": activity["start_at"], "endAt": activity["end_at"]})

    @app.post("/api/admin/window")
    def admin_window():
        body = request.get_json(silent=True) or {}
        activity = store.set_window(int(body.get("activityId", 1)),
                                    int(body.get("startOffset", -60)),
                                    int(body.get("endOffset", 3600)))
        return _ok({"activityId": activity["activity_id"], "startAt": activity["start_at"],
                    "endAt": activity["end_at"]})

    return app
