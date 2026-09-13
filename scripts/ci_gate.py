"""CI 闸门：确认本轮**真的抓到了新数据**，否则让工作流失败。

为什么必须要有这一步
--------------------
抓取失败时上游代码只记 warning、不抛错（设计如此，避免一次网络抖动就中断监控），
于是工作流照样变绿，接着把仓库里的**旧快照**重新发布一遍 ——
页面看着完全正常，实际上早就停止更新了。这类"静默失败"比抓取失败本身危险得多。

这道闸门把静默失败变成红色失败；而且它排在部署之前，失败时
`deploy-pages` 会被跳过，上一次的好数据不会被覆盖。

用法::

    python scripts/ci_gate.py --config config.ci.yaml --minutes 20
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import yaml


def main() -> int:
    ap = argparse.ArgumentParser(description="校验本轮抓取是否真的产生了新数据")
    ap.add_argument("-c", "--config", default="", help="读该配置取监控目标（航班号/日期/数据库路径）")
    ap.add_argument("--db", default="", help="SQLite 路径（默认取配置）")
    ap.add_argument("--minutes", type=int, default=20, help="多久内的数据算“本轮新鲜数据”")
    ap.add_argument("--flight", default="", help="要求该航班号也必须出现在新数据里")
    ap.add_argument("--allow-empty", action="store_true", help="只报告不失败（调试用）")
    ap.add_argument("--summary-out", default=os.environ.get("GITHUB_STEP_SUMMARY", ""))
    args = ap.parse_args()

    cfg = {}
    if args.config and Path(args.config).exists():
        cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}

    db_path = args.db or ((cfg.get("output") or {}).get("db_path")) or "data/prices.db"
    flight = args.flight
    date = ""
    if cfg.get("routes"):
        r0 = cfg["routes"][0]
        if not flight:
            w = r0.get("watch_flights") or []
            flight = (w[0] if w else "")
        date = (r0.get("dates") or [""])[0]

    if not Path(db_path).exists():
        print(f"::error::数据库不存在: {db_path}")
        return 1

    cutoff = (datetime.now() - timedelta(minutes=args.minutes)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    fresh = conn.execute(
        "SELECT COUNT(*) AS n FROM flight_prices WHERE fetched_at >= ?", (cutoff,)
    ).fetchone()["n"]

    fresh_target = 0
    if flight:
        sql = ("SELECT COUNT(*) AS n FROM flight_prices "
               "WHERE fetched_at >= ? AND UPPER(REPLACE(flight_no,' ','')) = ?")
        params = [cutoff, flight.upper().replace(" ", "")]
        if date:
            sql += " AND depart_date = ?"
            params.append(date)
        fresh_target = conn.execute(sql, params).fetchone()["n"]

    total = conn.execute("SELECT COUNT(*) AS n FROM flight_prices").fetchone()["n"]
    last = conn.execute("SELECT MAX(fetched_at) AS m FROM flight_prices").fetchone()["m"]

    print("=== CI 闸门：本轮新数据检查 ===")
    print(f"  数据库        : {db_path}")
    print(f"  时间窗口      : 最近 {args.minutes} 分钟（cutoff {cutoff}）")
    print(f"  本轮新记录数  : {fresh}")
    print(f"  目标航班新记录: {flight or '—'} -> {fresh_target}")
    print(f"  库内总记录数  : {total}   最新时间戳: {last}")
    print(f"  监控日期      : {date or '—'}")

    lines = [
        "### 本轮新数据检查",
        "",
        f"- 时间窗口：最近 {args.minutes} 分钟",
        f"- 本轮新记录数：**{fresh}**",
        f"- 目标航班（{flight or '—'}）新记录数：**{fresh_target}**",
        f"- 库内总记录：{total}，最新时间戳 {last}",
    ]

    ok = fresh > 0 and (fresh_target > 0 if flight else True)
    if ok:
        lines.append("\n✅ 抓取有效，继续部署。")
    else:
        reason = ("本轮没有任何新记录（页面可能改版 / 被风控拦截 / 等待窗口不足）"
                  if fresh == 0 else f"本轮没有 {flight} 的新记录（未执飞 / 售罄 / 解析未命中）")
        lines.append(f"\n❌ 抓取无效：{reason}\n\n**已跳过部署**，线上保持上一次的好数据。")
        print(f"::error::抓取未产生有效新数据：{reason}")

    if args.summary_out:
        with open(args.summary_out, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    if ok or args.allow_empty:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
