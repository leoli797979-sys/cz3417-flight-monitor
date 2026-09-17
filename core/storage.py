"""SQLite 价格存储"""
import os
import sqlite3
from contextlib import contextmanager
from typing import List, Optional

from .models import FlightPrice


SCHEMA = """
CREATE TABLE IF NOT EXISTS flight_prices (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform    TEXT NOT NULL,
    from_city   TEXT NOT NULL,
    to_city     TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    price       REAL NOT NULL,
    airline     TEXT,
    flight_no   TEXT,
    depart_time TEXT,
    arrive_time TEXT,
    fetched_at  TEXT NOT NULL,
    extra       TEXT,
    -- 1 = 这条只是"全航线当天最低价"，无法归属到某一架航班（读库方也需知道这点）
    route_level INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_route_date
    ON flight_prices (from_city, to_city, depart_date, platform);
CREATE INDEX IF NOT EXISTS idx_fetched_at
    ON flight_prices (fetched_at);

CREATE TABLE IF NOT EXISTS alert_state (
    route_key   TEXT PRIMARY KEY,   -- from-to-date 组合
    last_price  REAL NOT NULL,
    last_sent_at TEXT NOT NULL
);

-- 每个平台"上次尝试抓取"的时间。注意不能拿 flight_prices 里的最新时间当判据：
-- 平台被风控挡住时它一条都存不下来，用最新数据会把"刚抓过"误判成"很久没抓"，
-- 于是每轮都重试（携程每次要开一次浏览器 ~40 秒，正是这条坑）。
CREATE TABLE IF NOT EXISTS crawl_attempts (
    platform         TEXT PRIMARY KEY,
    last_attempt_at  TEXT NOT NULL
);
"""


class PriceStorage:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.db_path = db_path
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self):
        with self._conn() as c:
            c.executescript(SCHEMA)
            # 迁移：老库没有 route_level 列。CREATE TABLE IF NOT EXISTS 不会补列，
            # 所以这里显式检查并 ALTER，否则"是否是全航线最低价"这个标志会在落库后丢失。
            cols = {r["name"] for r in c.execute("PRAGMA table_info(flight_prices)")}
            if "route_level" not in cols:
                c.execute("ALTER TABLE flight_prices "
                          "ADD COLUMN route_level INTEGER DEFAULT 0")

    def save(self, fp: FlightPrice):
        with self._conn() as c:
            c.execute(
                """INSERT INTO flight_prices
                (platform, from_city, to_city, depart_date, price,
                 airline, flight_no, depart_time, arrive_time, fetched_at, extra,
                 route_level)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (fp.platform, fp.from_city, fp.to_city, fp.depart_date, fp.price,
                 fp.airline, fp.flight_no, fp.depart_time, fp.arrive_time,
                 fp.fetched_at, fp.extra, 1 if fp.route_level else 0),
            )

    def save_many(self, prices: List[FlightPrice]):
        for p in prices:
            self.save(p)

    def last_lowest(self, from_city: str, to_city: str, depart_date: str,
                    platform: Optional[str] = None) -> Optional[sqlite3.Row]:
        sql = ("SELECT * FROM flight_prices WHERE from_city=? AND to_city=? "
               "AND depart_date=?")
        args = [from_city, to_city, depart_date]
        if platform:
            sql += " AND platform=?"
            args.append(platform)
        sql += " ORDER BY price ASC LIMIT 1"
        with self._conn() as c:
            return c.execute(sql, args).fetchone()

    # ---- alert_state: 记录上次已推送价格，用于去抖 ----
    def get_alert_state(self, route_key: str) -> Optional[float]:
        with self._conn() as c:
            row = c.execute(
                "SELECT last_price FROM alert_state WHERE route_key=?",
                (route_key,),
            ).fetchone()
            return float(row["last_price"]) if row else None

    def set_alert_state(self, route_key: str, price: float):
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._conn() as c:
            c.execute(
                "INSERT INTO alert_state (route_key, last_price, last_sent_at) "
                "VALUES (?,?,?) ON CONFLICT(route_key) DO UPDATE SET "
                "last_price=excluded.last_price, last_sent_at=excluded.last_sent_at",
                (route_key, price, now),
            )

    def clear_alert_state(self, route_key: str):
        with self._conn() as c:
            c.execute("DELETE FROM alert_state WHERE route_key=?", (route_key,))

    def last_batch_age_minutes(self, platform: str) -> float:
        """该平台"上次尝试抓取"距今多少分钟（没有记录则返回一个很大的数）。"""
        with self._conn() as c:
            row = c.execute(
                "SELECT last_attempt_at FROM crawl_attempts WHERE platform=?",
                (platform,)).fetchone()
        if not row or not row["last_attempt_at"]:
            return 1e9
        try:
            from datetime import datetime
            t = datetime.strptime(str(row["last_attempt_at"])[:19], "%Y-%m-%d %H:%M:%S")
            return (datetime.now() - t).total_seconds() / 60.0
        except Exception:
            return 1e9

    def mark_attempt(self, platform: str):
        """记录一次抓取尝试（不管成没成功）。"""
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._conn() as c:
            c.execute(
                "INSERT INTO crawl_attempts (platform, last_attempt_at) VALUES (?,?) "
                "ON CONFLICT(platform) DO UPDATE SET last_attempt_at=excluded.last_attempt_at",
                (platform, now))

    def prune(self, keep_batches: int = 3, watch: Optional[List[str]] = None) -> int:
        """删掉"非监控班次"的旧批次，只保留最近 keep_batches 批；监控班次的历史全留。

        为什么需要：页面只用到"最近一批全量航班 + 监控班次的完整价格曲线"，而每轮会写进
        ~150 行。定时任务要把 prices.db 推给云端触发 GitHub Pages 重建（那一路没有额度
        限制，是刷新最快的一路），但库越大每个 git 对象越大 —— 2 MB 的库按 10 分钟推一次，
        8 天就能堆出 1 GB 以上的仓库历史。清掉用不到的旧批次后，库稳定在几百 KB。
        """
        watch = [str(w).upper().replace(" ", "") for w in (watch or []) if str(w).strip()]
        marks = ",".join("?" for _ in watch)
        not_watch = (f"UPPER(REPLACE(flight_no,' ','')) NOT IN ({marks})" if watch
                     else "1=1")
        with self._conn() as c:
            cut = c.execute(
                f"SELECT MIN(fetched_at) FROM (SELECT DISTINCT fetched_at "
                f"FROM flight_prices WHERE {not_watch} "
                f"ORDER BY fetched_at DESC LIMIT ?)",
                (*watch, keep_batches)).fetchone()[0]
            if not cut:
                return 0
            removed = c.execute(
                f"DELETE FROM flight_prices WHERE {not_watch} AND fetched_at < ?",
                (*watch, cut)).rowcount
        if removed:
            # VACUUM 必须在事务之外执行，所以单独开一个不做隐式事务的连接
            conn = sqlite3.connect(self.db_path, isolation_level=None)
            try:
                conn.execute("VACUUM")
            finally:
                conn.close()
        return removed
