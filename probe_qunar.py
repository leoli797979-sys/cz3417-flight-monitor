"""诊断脚本：用已保存的 token 直接请求去哪儿接口，把原始响应存盘并试解析。

用途：确认真实响应结构，验证 core/flights.py 的逐航班解析在真实数据上是否成立。

用法：
    python probe_qunar.py --date 2026-09-22 --from CAN --to CTU
    python probe_qunar.py --date 2026-09-22 --browser   # 强制走浏览器
"""
import argparse
import json
import os
import sys

from core.logger import setup_logger
from core import flights as F
from crawlers.qunar import QunarCrawler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="fc", default="CAN")
    ap.add_argument("--to", dest="tc", default="CTU")
    ap.add_argument("--date", default="2026-09-22")
    ap.add_argument("--browser", action="store_true", help="强制走浏览器路径")
    ap.add_argument("--headless", default="0", help="浏览器是否无头：1/0（走浏览器时有效）")
    args = ap.parse_args()

    os.makedirs("debug", exist_ok=True)
    logger = setup_logger("logs/probe.log")
    cfg = {"debug": True, "debug_dir": "debug", "user_data_dir": "user_data",
           "headless": args.headless == "1", "timeout_seconds": 45,
           "delay_min": 1, "delay_max": 2}
    c = QunarCrawler(cfg, logger)

    print(f"\n=== 探测 {args.fc}->{args.tc} {args.date} ===")
    tok = c._load_token()
    print(f"token 可用: {'是' if tok else '否'}"
          + (f"（Bella 长度 {len((tok or {}).get('body_template', {}).get('Bella', ''))}）" if tok else ""))

    if args.browser:
        text = c._fetch_via_browser(args.fc, args.tc, args.date)
    else:
        text = c._fetch_via_httpx(args.fc, args.tc, args.date)

    if not text:
        print("结果: 未取到响应（可能命中风控/限流，稍后重试）")
        raw_dir = "debug"
        files = sorted(f for f in os.listdir(raw_dir)) if os.path.isdir(raw_dir) else []
        if files:
            print(f"debug 目录现有 {len(files)} 个文件，最近 5 个：")
            for f in files[-5:]:
                print(f"   {f}  ({os.path.getsize(os.path.join(raw_dir, f))}B)")
        return 1

    out = os.path.join("debug", f"probe_{args.fc}_{args.tc}_{args.date}.txt")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"\n原始响应已存: {out}  (len={len(text)})")

    # 风控判定
    try:
        obj = json.loads(text)
        bstatus = obj.get("bstatus")
        print(f"顶层键: {list(obj.keys())}")
        print(f"bstatus: {bstatus}")
        print(f"ret: {obj.get('ret')}")
        data = obj.get("data")
        print(f"data 类型: {type(data).__name__}")
        if isinstance(data, str):
            print(f"data 内容前 300 字: {data[:300]}")
            try:
                inner = json.loads(data)
                print(f"data 解出后键: {list(inner.keys())[:30] if isinstance(inner, dict) else type(inner).__name__}")
            except Exception as e:
                print(f"data 二次解析失败: {e}")
        elif isinstance(data, dict):
            print(f"data 键: {list(data.keys())[:30]}")
    except Exception as e:
        print(f"顶层解析失败: {e}")
        print(f"响应前 500 字: {text[:500]}")

    print("\n--- 用 core/flights.py 试解析 ---")
    recs = QunarCrawler._extract_qunar_flights(text)
    if recs:
        print(f"解析到 {len(recs)} 架航班：")
        for r in sorted(recs, key=lambda x: x["price"])[:40]:
            print("   {:<8} {:>5}→{:<5} ¥{:<7.0f} {:<10} {}→{}".format(
                r["flight_no"], r["depart_time"] or "--:--", r["arrive_time"] or "--:--",
                r["price"], r.get("airline", "") or "",
                r.get("dep_airport", "")[:8], r.get("arr_airport", "")[:8]))
    else:
        print("未解析到逐航班记录")
        prices = QunarCrawler._extract_qunar_prices(text)
        print(f"老逻辑（全航线最低价）得到: {sorted(set(prices))[:10]}")

    hit = [r for r in recs if r["flight_no"] == "CZ3417"]
    print(f"\n>>> CZ3417 命中: {hit if hit else '未命中'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
