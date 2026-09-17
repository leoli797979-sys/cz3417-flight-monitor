"""统计"距上次去哪儿请求的间隔"与"本次是否被 1999 拦截"的关系。

零风险：只读 logs/monitor.log。

用途：判断去哪儿的限流是「短窗口规则（如每 5 分钟一次）」还是「长期累计配额/风险评分」。
  * 若拉长间隔能显著降低拦截率 -> 短窗口规则 -> 加密请求必然更糟
  * 若拦截率与间隔无关      -> 累计配额     -> 间隔多长都一样，加密只会更快耗尽额度
两种结论都指向同一点：缩短轮次间隔无法提高取数频率。
"""
from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

LOG = Path(sys.argv[1] if len(sys.argv) > 1 else "logs/monitor.log")
txt = LOG.read_text(encoding="utf-8", errors="replace")

attempts = []
for ln in txt.splitlines():
    if "[qunar] 开始抓取" in ln:
        try:
            t = datetime.strptime(ln[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        attempts.append({"t": t, "httpx": None, "rec": None})
        continue
    if not attempts:
        continue
    a = attempts[-1]
    if "httpx 命中风控(1999)" in ln:
        a["httpx"] = "1999"
    elif "httpx 命中，省去浏览器" in ln:
        a["httpx"] = "ok"
    elif "httpx 未命中，回退浏览器" in ln and a["httpx"] is None:
        a["httpx"] = "no-token"
    m = re.search(r"\[qunar\] 抓取完成，得到 (\d+) 条记录", ln)
    if m:
        a["rec"] = int(m.group(1))

have = [a for a in attempts if a["httpx"] in ("ok", "1999")]
print(f"可用于统计的请求（httpx 直连有明确结果）: {len(have)} 次\n")

buckets = [(0, 10, "<10 分钟"), (10, 60, "10–60 分钟"), (60, 120, "1–2 小时"),
           (120, 360, "2–6 小时"), (360, 10**9, ">6 小时")]
stat = defaultdict(lambda: {"ok": 0, "blocked": 0})

for i, a in enumerate(have):
    if i == 0:
        continue
    gap_min = (a["t"] - have[i - 1]["t"]).total_seconds() / 60
    for lo, hi, label in buckets:
        if lo <= gap_min < hi:
            stat[label]["ok" if a["httpx"] == "ok" else "blocked"] += 1
            break

print("=== 距上次请求的间隔 × 是否被 1999 拦截 ===")
print(f"{'间隔':<12}{'直连成功':>8}{'被拦':>6}{'被拦比例':>10}")
total_ok = total_bl = 0
for _, _, label in buckets:
    s = stat[label]
    n = s["ok"] + s["blocked"]
    if not n:
        continue
    total_ok += s["ok"]
    total_bl += s["blocked"]
    print(f"{label:<12}{s['ok']:>8}{s['blocked']:>6}{100*s['blocked']/n:>9.0f}%")
if total_ok + total_bl:
    print(f"{'合计':<12}{total_ok:>8}{total_bl:>6}{100*total_bl/(total_ok+total_bl):>9.0f}%")

print("\n=== 按小时统计请求量（看是否存在『每小时的额度』）===")
per_hour = Counter(a["t"].strftime("%m-%d %H") for a in have)
ok_per_hour = Counter(a["t"].strftime("%m-%d %H") for a in have if a["httpx"] == "ok")
for h in sorted(per_hour):
    if per_hour[h] >= 2:
        print(f"  {h} 时: 请求 {per_hour[h]} 次, 成功 {ok_per_hour.get(h,0)} 次")

print("\n=== 连续成功/失败串（看是否有『罚站期』）===")
seq = "".join("O" if a["httpx"] == "ok" else "X" for a in have)
print("  " + seq)
runs = re.findall(r"X+", seq)
if runs:
    print(f"  最长连续被拦: {max(len(r) for r in runs)} 次")
