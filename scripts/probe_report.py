"""汇总"数据源可行性探测"的结果：每个平台各自抓到了几条、有没有目标航班。

配合 .github/workflows/probe-sources.yml 使用（也可本地跑）。

用法::

    python scripts/probe_report.py --db data/probe.db --minutes 20 --flight CZ3417
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    ap = argparse.ArgumentParser(description="汇总各数据源的探测结果")
    ap.add_argument("--db", default="data/probe.db")
    ap.add_argument("--minutes", type=int, default=20)
    ap.add_argument("--flight", default="CZ3417")
    ap.add_argument("--summary-out", default="")
    args = ap.parse_args()

    if not Path(args.db).exists():
        print(f"数据库不存在: {args.db}")
        return 1

    # 用项目自己的存储层初始化一次，触发建表与列迁移（老库缺 route_level 时补列）
    try:
        from core.storage import PriceStorage
        PriceStorage(str(args.db))
    except Exception as e:
        print(f"（存储层初始化告警，忽略：{e}）")

    cutoff = (datetime.now() - timedelta(minutes=args.minutes)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # 列可能不存在（老库/未迁移），缺失时降级为 0，保证脚本永远能跑
    cols = {r[1] for r in conn.execute("PRAGMA table_info(flight_prices)")}
    rl_expr = "route_level" if "route_level" in cols else "0 AS route_level"

    rows = conn.execute(
        "SELECT platform, flight_no, depart_time, arrive_time, price, " + rl_expr +
        " FROM flight_prices WHERE fetched_at >= ?", (cutoff,)
    ).fetchall()

    by_platform: dict = {}
    for r in rows:
        by_platform.setdefault(r["platform"], []).append(r)

    target = args.flight.upper().replace(" ", "")
    lines = ["| 平台 | 抓到记录 | 逐航班 | 目标航班 | 目标价 | 区间 |",
             "|---|---|---|---|---|---|"]
    print("=== 各数据源探测结果（最近 %d 分钟）===" % args.minutes)
    ok_platforms = []
    for plat in sorted(by_platform):
        items = by_platform[plat]
        per_flight = [x for x in items if not x["route_level"] and x["flight_no"]]
        hit = [x for x in per_flight
               if (x["flight_no"] or "").upper().replace(" ", "") == target]
        prices = [float(x["price"]) for x in items]
        tgt_price = f"¥{min(float(x['price']) for x in hit):.0f}" if hit else "—"
        rng = f"¥{min(prices):.0f}–¥{max(prices):.0f}" if prices else "—"
        print(f"  {plat:<10} 共 {len(items):>4} 条 | 逐航班 {len(per_flight):>4} 条 | "
              f"{target} {len(hit)} 条 | {tgt_price:<8} | {rng}")
        lines.append(f"| {plat} | {len(items)} | {len(per_flight)} | {len(hit)} | "
                     f"{tgt_price} | {rng} |")
        if hit:
            ok_platforms.append(plat)
            for h in hit[:3]:
                print(f"      -> {h['flight_no']} {h['depart_time'] or '--:--'}→"
                      f"{h['arrive_time'] or '--:--'} ¥{float(h['price']):.0f}")

    print()
    if not by_platform:
        print("!! 所有数据源都没有抓到任何记录（云端被全部拦截）")
    elif ok_platforms:
        print(f"OK 云端可用且能抓到目标航班的数据源: {', '.join(ok_platforms)}")
        print("   => 电脑关机也能持续更新数据，可行")
    else:
        print("部分平台有数据，但都没有目标航班 CZ3417 的逐航班记录")
        print("   => 只能拿到航线级价格，无法精确监控指定航班")

    if args.summary_out:
        with open(args.summary_out, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
