# -*- coding: utf-8 -*-
"""数据层：统一 DB-API 访问（SQLite 用于本地/CI，MySQL 用于真实环境）。

测试用例通过这一层做「接口 + 数据库」双端校验，例如：
    assert db.scalar("SELECT stock FROM activity WHERE activity_id=1") == 9
"""
import sqlite3
import threading

from framework.conf import get_config, resolve
from framework.logger import get_logger

log = get_logger("atf.db")


class DBClient:
    """轻量数据库客户端：query / query_one / scalar / execute + 计数辅助。"""

    def __init__(self, driver=None, **options):
        cfg = get_config().section("database")
        self.driver = (driver or cfg.get("driver") or "sqlite").lower()
        self.options = dict(cfg)
        self.options.update({k: v for k, v in options.items() if v is not None})
        self._conn = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 连接
    def connect(self):
        if self._conn is not None:
            return self._conn
        if self.driver == "sqlite":
            path = self.options.get("path") or ":memory:"
            target = str(resolve(path)) if path != ":memory:" else path
            self._conn = sqlite3.connect(target, timeout=float(self.options.get("timeout", 5)),
                                         check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        elif self.driver in ("mysql", "pymysql"):
            import pymysql
            self._conn = pymysql.connect(
                host=self.options.get("host", "127.0.0.1"),
                port=int(self.options.get("port", 3306)),
                user=self.options.get("user", "root"),
                password=self.options.get("password", ""),
                database=self.options.get("database", "test"),
                charset="utf8mb4",
                autocommit=True,
                cursorclass=pymysql.cursors.DictCursor)
        else:
            raise ValueError("不支持的 database.driver：%s" % self.driver)
        safe = {k: ("***" if k == "password" else v) for k, v in self.options.items()}
        log.debug("数据库已连接 driver=%s %s", self.driver, safe)
        return self._conn

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    # ------------------------------------------------------------ 查询
    def query(self, sql, params=()):
        """返回 list[dict]，字段名即列名。"""
        conn = self.connect()
        with self._lock:
            cursor = conn.cursor()
            try:
                cursor.execute(sql, params)
                rows = cursor.fetchall()
            finally:
                cursor.close()
        return [dict(row) for row in rows]

    def query_one(self, sql, params=()):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql, params=(), default=None):
        row = self.query_one(sql, params)
        if not row:
            return default
        return list(row.values())[0]

    def execute(self, sql, params=()):
        conn = self.connect()
        with self._lock:
            cursor = conn.cursor()
            try:
                cursor.execute(sql, params)
                affected = cursor.rowcount
                if not getattr(conn, "autocommit", True):
                    conn.commit()
            finally:
                cursor.close()
        return affected

    # ------------------------------------------------------------ 业务辅助
    def count(self, table, where="", params=()):
        sql = "SELECT COUNT(*) AS c FROM %s" % table
        if where:
            sql += " WHERE " + where
        return int(self.scalar(sql, params, 0) or 0)
