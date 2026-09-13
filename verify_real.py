"""用真实响应验证去哪儿解析器（离线，读 debug 里存下的原始响应）。

用法: python verify_real.py [debug/probe_CAN_CTU_2026-09-22.txt]
"""
import sys
from pathlib import Path

from crawlers.qunar import QunarCrawler

path = sys.argv[1] if len(sys.argv) > 1 else "debug/probe_CAN_CTU_2026-09-22.txt"
text = Path(path).read_text(encoding="utf-8", errors="replace")
print(f"原始响应: {path}  len={len(text)}")

recs = QunarCrawler._extract_qunar_flights(text)
print(f"\n解析到 {len(recs)} 架航班\n")
print(f"{'航班':<9}{'起飞':>6} {'到达':>6}  {'价格':>7}  {'航司':<8}{'机型':<14}{'出发':<14}{'到达':<14}")
print("-" * 92)
for r in sorted(recs, key=lambda x: x["price"]):
    dep_ap = f"{r.get('dep_airport','')}{r.get('dep_terminal','') or ''}({r.get('dep_airport_id','') or '?'})"
    arr_ap = f"{r.get('arr_airport','')}{r.get('arr_terminal','') or ''}({r.get('arr_airport_id','') or '?'})"
    print(f"{r['flight_no']:<9}{r['depart_time'] or '--:--':>6} {r['arrive_time'] or '--:--':>6}  "
          f"¥{r['price']:>6.0f}  {r.get('airline','') or '':<8}{r.get('aircraft','') or '':<14}"
          f"{dep_ap:<14}{arr_ap:<14}")

cz = [r for r in recs if r["flight_no"] == "CZ3417"]
print("\n" + "=" * 92)
if cz:
    r = cz[0]
    print("目标航班命中：")
    print(f"  {r['flight_no']}  {r['depart_time']} → {r['arrive_time']}   ¥{r['price']:.0f}")
    print(f"  航司={r.get('airline')}  机型={r.get('aircraft')}")
    print(f"  出发={r.get('dep_airport')}({r.get('dep_airport_id')}) T{r.get('dep_terminal')}"
          f"  到达={r.get('arr_airport')}({r.get('arr_airport_id')}) T{r.get('arr_terminal')}")
else:
    print("目标航班 CZ3417 未命中")

# 到双流(CTU) vs 到天府(TFU) 的对比，防止把 TFU 的价当成 CZ3417 的参考
ctu = [r for r in recs if r.get("arr_airport_id") == "CTU"]
tfu = [r for r in recs if r.get("arr_airport_id") == "TFU"]
print(f"\n落到双流 CTU 的航班 {len(ctu)} 架，最低 ¥{min((r['price'] for r in ctu), default=0):.0f}")
print(f"落到天府 TFU 的航班 {len(tfu)} 架，最低 ¥{min((r['price'] for r in tfu), default=0):.0f}")
