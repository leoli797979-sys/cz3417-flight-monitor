"""查看指定航班在库里的当前状态与历史（本地核对用，不联网）。

用法：
    python scripts/check_flights.py                          # 用 config.yaml 里的 watch_flights
    python scripts/check_flights.py --flights CZ3417,3U8736
    python scripts/check_flights.py --date 2026-09-22 -c config.yaml
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser(description="查看指定航班的当前价与历史")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--db", default="")
    ap.add_argument("--date", default="")
    ap.add_argument("--flights", default="", help="逗号分隔；留空则取配置里的 watch_flights")
    args = ap.parse_args()

    cfg = {}
    cfg_path = ROOT / args.config
    if cfg_path.exists():
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    route = (cfg.get("routes") or [{}])[0]
    db = args.db or (cfg.get("output") or {}).get("db_path") or "data/prices.db"
    date = args.date or (route.get("dates") or [""])[0]
    flights = [x.strip().upper() for x in args.flights.split(",") if x.strip()] or \
              [str(w).upper() for w in (route.get("watch_flights") or [])]

    if not Path(db).exists():
        print(f"数据库不存在: {db}", file=sys.stderr)
        return 1
    if not flights:
        print("没有要查的航班号（给 --flights 或在配置里设置 watch_flights）", file=sys.stderr)
        return 1

    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    mx = c.execute("SELECT MAX(fetched_at) AS m FROM flight_prices").fetchone()["m"]
    print(f"数据库: {db}")
    print(f"最新一轮: {mx}    监控日期: {date}")
    print()
    print(f"{'航班':<9}{'航司':<9}{'起飞':>6}{'到达':>6}  {'当前价':>7}  {'出发':<13}{'到达':<13}{'平台'}")
    print("-" * 88)

    missing = []
    for f in flights:
        row = c.execute(
            "SELECT * FROM flight_prices WHERE UPPER(REPLACE(flight_no,' ',''))=? "
            "AND depart_date=? ORDER BY fetched_at DESC, price ASC LIMIT 1",
            (f, date)).fetchone()
        if not row:
            print(f"{f:<9}!! 本轮没有这班（未执飞 / 售罄 / 解析未命中）")
            missing.append(f)
            continue
        try:
            ex = json.loads(row["extra"] or "{}")
        except Exception:
            ex = {}
        dap = f"{ex.get('dep_airport','')}{ex.get('dep_terminal','')}({ex.get('dep_airport_id','')})"
        aap = f"{ex.get('arr_airport','')}{ex.get('arr_terminal','')}({ex.get('arr_airport_id','')})"
        print(f"{f:<9}{row['airline'] or '':<9}{row['depart_time'] or '--:--':>6}"
              f"{row['arrive_time'] or '--:--':>6}  {row['price']:>7.0f}  {dap:<13}{aap:<13}"
              f"{row['platform']}")

    print()
    print(f"{'航班':<9}{'样本(轮)':>9}{'最低':>7}{'最高':>7}{'均价':>7}{'最近变化':>10}")
    print("-" * 56)
    for f in flights:
        rows = c.execute(
            "SELECT fetched_at, MIN(price) AS price FROM flight_prices "
            "WHERE UPPER(REPLACE(flight_no,' ',''))=? AND depart_date=? "
            "GROUP BY fetched_at ORDER BY fetched_at ASC", (f, date)).fetchall()
        if not rows:
            continue
        prices = [float(r["price"]) for r in rows]
        delta = prices[-1] - prices[0]
        print(f"{f:<9}{len(prices):>9}{min(prices):>7.0f}{max(prices):>7.0f}"
              f"{sum(prices)/len(prices):>7.0f}{('+' if delta > 0 else '') + format(delta, '.0f'):>10}")

    if missing:
        print()
        print(f"注意：{', '.join(missing)} 在本轮结果里没有出现。")
        print("  这些班次是晚间班（20:15/20:30）或早班，若配置里的 depart_time_from/to")
        print("  时间窗没覆盖，就会被过滤掉 —— 按航班号监控时应把时间窗留空。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
