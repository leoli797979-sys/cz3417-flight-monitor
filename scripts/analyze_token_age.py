"""验证真正的瓶颈：是「限流」还是「令牌失效」？

零风险：只读 logs/monitor.log。

思路：把每次请求的「距上次令牌刷新多久」和「是否被 1999 拦」做交叉统计。
  * 若拦截率随令牌年龄上升 -> 瓶颈是令牌新鲜度，与轮次间隔无关
    => 缩短间隔不但无害，反而因为令牌更新鲜而更容易成功
  * 若与令牌年龄无关、只与请求密度相关 -> 才是真限流
另附：被拦之后浏览器兜底的成功率，用于区分两类 1999：
  (a) 令牌过期 -> 握手能刷新 -> 兜底成功
  (b) 真被限流 -> 握手同样被拦 -> 兜底失败
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

txt = Path("logs/monitor.log").read_text(encoding="utf-8", errors="replace")
lines = txt.splitlines()

events = []          # 所有事件（请求/令牌刷新）
cur = None
for ln in lines:
    try:
        t = datetime.strptime(ln[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        continue
    if "token 已刷新" in ln:
        events.append(("token", t))
    elif "[qunar] 开始抓取" in ln:
        events.append(("req_start", t))
        cur = {"t": t, "httpx": None, "rec": None}
        events.append(("req_obj", cur))
    elif cur is not None:
        if "httpx 命中风控(1999)" in ln:
            cur["httpx"] = "1999"
        elif "httpx 命中，省去浏览器" in ln:
            cur["httpx"] = "ok"
        elif "httpx 未命中，回退浏览器" in ln and cur["httpx"] is None:
            cur["httpx"] = "no-token"
        m = re.search(r"\[qunar\] 抓取完成，得到 (\d+) 条记录", ln)
        if m:
            cur["rec"] = int(m.group(1))

reqs = [e[1] for e in events if e[0] == "req_obj" and e[1]["httpx"] in ("ok", "1999")]
tokens = [e[1] for e in events if e[0] == "token"]

print(f"有效请求 {len(reqs)} 次，日志中的令牌刷新事件 {len(tokens)} 次\n")

def last_token_before(t):
    prev = [x for x in tokens if x <= t]
    return max(prev) if prev else None

buckets = [(0, 30, "≤30 分钟"), (30, 90, "30–90 分钟"), (90, 300, "1.5–5 小时"),
           (300, 10**9, ">5 小时"), (None, None, "无刷新记录")]
stat = defaultdict(lambda: {"ok": 0, "bl": 0})
for a in reqs:
    lt = last_token_before(a["t"])
    if lt is None:
        stat["无刷新记录"]["ok" if a["httpx"] == "ok" else "bl"] += 1
        continue
    age = (a["t"] - lt).total_seconds() / 60
    for lo, hi, label in buckets:
        if lo is not None and lo <= age < hi:
            stat[label]["ok" if a["httpx"] == "ok" else "bl"] += 1
            break

print("=== 距上次令牌刷新多久 × 是否被 1999 拦 ===")
print(f"{'令牌年龄':<14}{'成功':>6}{'被拦':>6}{'被拦比例':>10}")
for _, _, label in buckets:
    s = stat[label]
    n = s["ok"] + s["bl"]
    if n:
        print(f"{label:<14}{s['ok']:>6}{s['bl']:>6}{100*s['bl']/n:>9.0f}%")

print("\n=== 状态转移：上一次的结果 -> 这一次的成功率 ===")
trans = defaultdict(lambda: {"ok": 0, "bl": 0})
for i in range(1, len(reqs)):
    prev, cur_ = reqs[i - 1]["httpx"], reqs[i]["httpx"]
    trans[prev]["ok" if cur_ == "ok" else "bl"] += 1
for prev in ("ok", "1999"):
    s = trans[prev]
    n = s["ok"] + s["bl"]
    if n:
        print(f"  上次={'成功' if prev=='ok' else '被拦'} -> 本次成功率 {100*s['ok']/n:>3.0f}%  ({s['ok']}/{n})")

print("\n=== 被拦后浏览器兜底的结果 ===")
fb = Counter()
for a in reqs:
    if a["httpx"] == "1999" and a["rec"] is not None:
        fb["兜底成功" if a["rec"] > 0 else "兜底失败"] += 1
print(" ", dict(fb))
