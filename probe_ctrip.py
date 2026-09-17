"""单独测试携程爬虫（诊断用，只打印不写库）。

用法：
    python probe_ctrip.py --from CAN --to CTU --date 2026-09-22
    python probe_ctrip.py --from CTU --to CAN --date 2026-09-22 --headless 0
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.logger import setup_logger          # noqa: E402
from crawlers.ctrip import CtripCrawler       # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="fc", default="CAN")
    ap.add_argument("--to", dest="tc", default="CTU")
    ap.add_argument("--date", default="2026-09-22")
    ap.add_argument("--headless", default="1", help="1=无头 0=有头(便于人工过验证码)")
    args = ap.parse_args()

    logger = setup_logger("logs/probe_ctrip.log")
    cfg = {
        "headless": args.headless == "1",
        "debug": True,
        "debug_dir": "debug",
        "user_data_dir": "user_data",
        "timeout_seconds": 45,
        "delay_min": 1,
        "delay_max": 2,
    }
    c = CtripCrawler(cfg, logger)
    rows = c.safe_fetch(args.fc, args.tc, [args.date])

    print(f"\n=== 携程 {args.fc} → {args.tc} {args.date} ===")
    print(f"拿到 {len(rows)} 条记录")
    for r in sorted(rows, key=lambda x: x.price)[:20]:
        print(f"  {(r.flight_no or '(无航班号)'):<10}"
              f"{(r.depart_time or '--:--'):>6}→{(r.arrive_time or '--:--'):<6} "
              f"¥{r.price:>7.0f}  {r.airline}")
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
