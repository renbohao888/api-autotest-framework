# -*- coding: utf-8 -*-
"""被测系统（SUT）：一个简化版的「限量秒杀」服务，用于承载接口自动化测试。

服务本身包含真实业务里的关键规则，测试用例围绕这些规则做等价类 / 边界值 / 状态迁移覆盖：
    - 库存扣减必须原子（不超卖、不为负）
    - 同一用户同一活动限购 1 件（幂等 / 防重复下单）
    - 活动时间窗口校验（未开始 / 已结束）
    - 每人每秒限流（模拟网关限流）
    - 订单状态机：WAIT_PAY -> PAID -> COMPLETED，WAIT_PAY -> CANCELED / TIMEOUT_CLOSED（回补库存）
"""
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS activity (
    activity_id INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    stock       INTEGER NOT NULL DEFAULT 0,
    price       REAL    NOT NULL DEFAULT 0,
    status      TEXT    NOT NULL DEFAULT 'ONLINE',
    start_at    TEXT    NOT NULL,
    end_at      TEXT    NOT NULL,
    version     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS orders (
    order_no    TEXT PRIMARY KEY,
    activity_id INTEGER NOT NULL,
    user_id     TEXT NOT NULL,
    amount      REAL    NOT NULL DEFAULT 0,
    status      TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uk_order_user ON orders(activity_id, user_id);
CREATE INDEX IF NOT EXISTS idx_order_status ON orders(activity_id, status);
"""

ORDER_STATES = ("WAIT_PAY", "PAID", "COMPLETED", "CANCELED", "TIMEOUT_CLOSED")
# 允许的状态迁移：未列出的迁移一律拒绝（测试用状态迁移法覆盖）
LEGAL_TRANSITIONS = {
    "WAIT_PAY": ("PAID", "CANCELED", "TIMEOUT_CLOSED"),
    "PAID": ("COMPLETED",),
    "COMPLETED": (),
    "CANCELED": (),
    "TIMEOUT_CLOSED": (),
}
# 需要回补库存的终态
RESTOCK_STATUSES = ("CANCELED", "TIMEOUT_CLOSED")

# 业务码：与 HTTP 状态码解耦，测试用例同时校验两者
CODE_OK = 0
CODE_BAD_CREDENTIAL = 1001
CODE_UNAUTHORIZED = 1002
CODE_PARAM_MISSING = 1003
CODE_ACTIVITY_NOT_FOUND = 1004
CODE_NOT_STARTED = 1005
CODE_ENDED = 1006
CODE_SOLD_OUT = 1007
CODE_RATE_LIMITED = 1008
CODE_DUPLICATE_ORDER = 1009
CODE_ILLEGAL_STATE = 1010
CODE_ORDER_NOT_FOUND = 1011

TIME_FMT = "%Y-%m-%d %H:%M:%S"


class BusinessError(Exception):
    """业务异常：code（业务码）+ http_status（HTTP 状态码）。"""

    def __init__(self, code, message, http_status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def _fmt(moment):
    return moment.strftime(TIME_FMT)


def _parse(text):
    return datetime.strptime(text, TIME_FMT)


def _safe_rollback(conn):
    """回滚时兜底：事务可能已经不可用，回滚失败不能掩盖原始异常。"""
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


class SqliteStore:
    """SQLite 实现：写操作统一用 BEGIN IMMEDIATE 串行化，保证扣减不超卖。"""

    def __init__(self, db_path, limit_per_user_per_second=3):
        self.db_path = str(db_path)
        self.limit_per_user_per_second = int(limit_per_user_per_second)
        self._rate = {}
        self._rate_lock = threading.Lock()
        self._init_schema()

    # ------------------------------------------------------------ 基础
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def _init_schema(self):
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    def health(self):
        conn = self._connect()
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
        return True

    # ------------------------------------------------------------ 数据准备
    def reset(self, activity_id=1, stock=10, price=99.0, start_offset=-60, end_offset=3600):
        """测试夹具：清库 + 重置活动（时间窗口相对当前时间偏移）。"""
        start = _fmt(datetime.now() + timedelta(seconds=start_offset))
        end = _fmt(datetime.now() + timedelta(seconds=end_offset))
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM orders")
            conn.execute("DELETE FROM activity")
            conn.execute(
                "INSERT INTO activity(activity_id,title,stock,price,status,start_at,end_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (activity_id, "测试秒杀活动", stock, price, "ONLINE", start, end))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        with self._rate_lock:
            self._rate.clear()
        return self.get_activity(activity_id)

    def set_window(self, activity_id, start_offset, end_offset):
        """调整活动时间窗口，用于「未开始 / 已结束」用例。"""
        start = _fmt(datetime.now() + timedelta(seconds=start_offset))
        end = _fmt(datetime.now() + timedelta(seconds=end_offset))
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE activity SET start_at=?, end_at=? WHERE activity_id=?",
                (start, end, activity_id)).rowcount
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        if not changed:
            raise BusinessError(CODE_ACTIVITY_NOT_FOUND, "活动不存在", 404)
        return self.get_activity(activity_id)

    # ------------------------------------------------------------ 查询
    def get_activity(self, activity_id):
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM activity WHERE activity_id=?",
                               (activity_id,)).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None

    def stock_of(self, activity_id):
        activity = self.get_activity(activity_id)
        return None if activity is None else int(activity["stock"])

    def get_order(self, order_no):
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM orders WHERE order_no=?", (order_no,)).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None

    def list_orders(self, activity_id=None, status=None):
        sql = "SELECT * FROM orders"
        params = []
        conditions = []
        if activity_id is not None:
            conditions.append("activity_id=?")
            params.append(activity_id)
        if status is not None:
            conditions.append("status=?")
            params.append(status)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]

    def order_count(self, activity_id=None, status=None):
        return len(self.list_orders(activity_id, status))

    # ------------------------------------------------------------ 限流
    def rate_limited(self, user_id, activity_id):
        """滑动窗口限流：同一用户对同一活动每秒最多 limit_per_user_per_second 次。"""
        key = (str(user_id), int(activity_id))
        now = time.time()
        with self._rate_lock:
            hits = [t for t in self._rate.get(key, []) if now - t < 1.0]
            if len(hits) >= self.limit_per_user_per_second:
                self._rate[key] = hits
                return True
            hits.append(now)
            self._rate[key] = hits
        return False



    # ------------------------------------------------------------ 下单
    def create_order(self, activity_id, user_id):
        """限量抢购：校验活动 -> （同一个写事务内）校验重复 + 条件扣减库存 -> 落订单。

        注意：重复下单校验必须和「扣库存 + 落订单」在同一个 BEGIN IMMEDIATE 事务里，
        否则并发请求会同时通过校验（TOCTOU 竞态），造成同一用户多单、库存多扣。
        """
        activity = self.get_activity(activity_id)
        if activity is None:
            raise BusinessError(CODE_ACTIVITY_NOT_FOUND, "活动不存在", 404)

        now = datetime.now()
        if now < _parse(activity["start_at"]):
            raise BusinessError(CODE_NOT_STARTED, "活动尚未开始", 409)
        if now > _parse(activity["end_at"]):
            raise BusinessError(CODE_ENDED, "活动已结束", 409)
        if int(activity["stock"]) <= 0:
            raise BusinessError(CODE_SOLD_OUT, "库存不足，已售罄", 409)

        conn = self._connect()
        order_no = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            existed = conn.execute(
                "SELECT order_no FROM orders WHERE activity_id=? AND user_id=?",
                (activity_id, str(user_id))).fetchone()
            if existed is not None:
                raise BusinessError(CODE_DUPLICATE_ORDER, "您已参与过该活动，请勿重复下单", 409)

            updated = conn.execute(
                "UPDATE activity SET stock = stock - 1, version = version + 1 "
                "WHERE activity_id=? AND stock > 0", (activity_id,)).rowcount
            if updated != 1:
                raise BusinessError(CODE_SOLD_OUT, "库存不足，已售罄", 409)

            order_no = "SO%s%s" % (now.strftime("%Y%m%d%H%M%S"), uuid.uuid4().hex[:6].upper())
            conn.execute(
                "INSERT INTO orders(order_no, activity_id, user_id, amount, status,"
                " created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                (order_no, activity_id, str(user_id), activity["price"], "WAIT_PAY",
                 _fmt(now), _fmt(now)))
            conn.execute("COMMIT")
        except BusinessError:
            _safe_rollback(conn)
            raise
        except sqlite3.IntegrityError:
            # 唯一索引 uk_order_user 兜底：并发下冲突要转成业务码，而不是 500
            _safe_rollback(conn)
            raise BusinessError(CODE_DUPLICATE_ORDER, "您已参与过该活动，请勿重复下单", 409)
        except Exception:
            _safe_rollback(conn)
            raise
        finally:
            conn.close()
        return self.get_order(order_no)

    # ------------------------------------------------------------ 状态机
    def change_state(self, order_no, target):
        """按状态机流转订单；非法迁移抛 1010，取消/超时关单回补库存。"""
        if target not in ORDER_STATES:
            raise BusinessError(CODE_ILLEGAL_STATE, "非法目标状态：%s" % target, 400)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM orders WHERE order_no=?", (order_no,)).fetchone()
            if row is None:
                raise BusinessError(CODE_ORDER_NOT_FOUND, "订单不存在", 404)
            current = row["status"]
            if target not in LEGAL_TRANSITIONS.get(current, ()):
                raise BusinessError(
                    CODE_ILLEGAL_STATE,
                    "订单状态不允许从 %s 变更为 %s" % (current, target), 409)
            if target in RESTOCK_STATUSES:
                # 取消 / 支付超时关单要把占用的库存还回去，
                # 否则库存会被「已关闭的订单」永久占用（实测缺陷：取消后库存不回补）
                conn.execute("UPDATE activity SET stock = stock + 1, version = version + 1 "
                             "WHERE activity_id=?", (row["activity_id"],))
            conn.execute("UPDATE orders SET status=?, updated_at=? WHERE order_no=?",
                         (target, _fmt(datetime.now()), order_no))
            conn.execute("COMMIT")
        except BusinessError:
            _safe_rollback(conn)
            raise
        except Exception:
            _safe_rollback(conn)
            raise
        finally:
            conn.close()
        return self.get_order(order_no)

    def pay(self, order_no):
        return self.change_state(order_no, "PAID")

    def cancel(self, order_no):
        return self.change_state(order_no, "CANCELED")

    def close_timeout(self, order_no):
        return self.change_state(order_no, "TIMEOUT_CLOSED")

    def complete(self, order_no):
        return self.change_state(order_no, "COMPLETED")
