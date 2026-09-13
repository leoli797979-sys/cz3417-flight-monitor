"""查看已抓取的价格历史（直接读 SQLite，不联网）

用法：
    python show.py                                   # 最近一轮全部记录
    python show.py --flight CZ3417                   # 只看指定航班
    python show.py --flight CZ3417 --history         # 该航班的抓取历史 + 最低/最高
    python show.py --date 2026-09-22 --limit 100
"""
import argparse
import sqlite3
import sys
from pathlib import Path

import yaml


def load_db_path(cfg_path: str = "config.yaml") -> str:
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        cfg = {}
    return (cfg.get("output") or {}).get("db_path", "data/prices.db")


def connect(db_path: str) -> sqlite3.Connection:
    if not Path(db_path).exists():
        print(f"数据库不存在: {db_path}（先跑一次 python main.py --once）", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def latest_round(conn, date: str = "", flight: str = "", limit: int = 60):
    """取最近一次抓取时间的记录。"""
    where, args = [], []
    if date:
        where.append("depart_date = ?")
        args.append(date)
    if flight:
        where.append("UPPER(REPLACE(flight_no,' ','')) = ?")
        args.append(flight.upper().replace(" ", ""))
    sql = "SELECT * FROM flight_prices"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY fetched_at DESC LIMIT ?"
    args.append(limit)
    return conn.execute(sql, args).fetchall()


def print_rows(rows, title: str):
    print(f"\n{title}")
    if not rows:
        print("  （无记录）")
        return
    print(f"  {'抓取时间':<20} {'平台':<9} {'航班':<9} {'时刻':<13} {'价格':>8}  {'舱/航司'}")
    print("  " + "-" * 78)
    for r in rows:
        dep = r["depart_time"] or "--:--"
        arr = r["arrive_time"] or "--:--"
        mark = "  [全航线最低价]" if not r["flight_no"] else ""
        print(f"  {r['fetched_at']:<20} {r['platform']:<9} "
              f"{(r['flight_no'] or '-'):<9} {dep + '→' + arr:<13} "
              f"{r['price']:>8.0f}  {r['airline'] or ''}{mark}")


def print_history(conn, date: str, flight: str, limit: int = 200):
    sql = ("SELECT fetched_at, platform, flight_no, airline, price, "
           "depart_time, arrive_time "
           "FROM flight_prices WHERE UPPER(REPLACE(flight_no,' ','')) = ?")
    args = [flight.upper().replace(" ", "")]
    if date:
        sql += " AND depart_date = ?"
        args.append(date)
    sql += " ORDER BY fetched_at DESC LIMIT ?"
    args.append(limit)
    rows = conn.execute(sql, args).fetchall()
    print_rows(rows, f"{flight} 抓取历史（最近 {limit} 条，新→旧）")
    if rows:
        prices = [r["price"] for r in rows]
        print("\n  统计：")
        print(f"    样本数     : {len(prices)}")
        print(f"    当前/最新价 : ¥{prices[0]:.0f}（{rows[0]['fetched_at']} / {rows[0]['platform']}）")
        print(f"    区间       : ¥{min(prices):.0f} ~ ¥{max(prices):.0f}")
        print(f"    均值       : ¥{sum(prices) / len(prices):.0f}")


def main():
    ap = argparse.ArgumentParser(description="查看已抓取的机票价格")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--date", default="", help="日期 YYYY-MM-DD")
    ap.add_argument("--flight", default="", help="航班号，如 CZ3417")
    ap.add_argument("--history", action="store_true", help="显示该航班的全部抓取历史与统计")
    ap.add_argument("--limit", type=int, default=60)
    args = ap.parse_args()

    conn = connect(load_db_path(args.config))
    if args.history:
        if not args.flight:
            print("--history 需要同时指定 --flight", file=sys.stderr)
            sys.exit(2)
        print_history(conn, args.date, args.flight, args.limit if args.limit != 60 else 200)
    else:
        rows = latest_round(conn, args.date, args.flight, args.limit)
        title = "最近记录"
        if args.flight:
            title += f"（航班 {args.flight.upper()}）"
        if args.date:
            title += f"（日期 {args.date}）"
        print_rows(rows, title)


if __name__ == "__main__":
    main()
