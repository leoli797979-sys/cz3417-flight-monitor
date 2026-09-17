"""分析抓取日志，判断去哪儿限流到底卡在哪一环。

零风险：只读 logs/monitor.log，不发起任何请求。

关心的问题：
  1. 一轮里 qunar 会请求几次（航向数）？
  2. 同轮内的第 1 次 vs 第 2 次请求，成功率差多少？
     —— 如果第 2 次明显更容易被 1999 拦截，说明真正的约束是
        "请求之间的间隔（约 5 分钟全局限流）"，而不是"轮次之间的 90 分钟"。
  3. httpx 直连成功 / 撞限流后浏览器兜底的成率分别是多少？
"""
from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

LOG = Path(sys.argv[1] if len(sys.argv) > 1 else "logs/monitor.log")
txt = LOG.read_text(encoding="utf-8", errors="replace")
lines = txt.splitlines()

rounds = []
cur = None
for ln in lines:
    if "===== 开始一轮抓取 =====" in ln:
        cur = {"attempts": [], "t": ln[:19]}
        rounds.append(cur)
        continue
    if cur is None:
        continue
    m = re.search(r"\[qunar\] 开始抓取 (\w+)->(\w+) 日期=\[(.*?)\]", ln)
    if m:
        cur["attempts"].append({"route": f"{m.group(1)}->{m.group(2)}", "date": m.group(3),
                                "httpx": None, "records": None})
        continue
    if not cur["attempts"]:
        continue
    a = cur["attempts"][-1]
    if "httpx 命中，省去浏览器" in ln:
        a["httpx"] = "ok"
    elif "httpx 命中风控(1999)" in ln:
        a["httpx"] = "1999"
    elif "httpx 未命中，回退浏览器" in ln:
        if a["httpx"] is None:
            a["httpx"] = "no-token"
    m = re.search(r"\[qunar\] 抓取完成，得到 (\d+) 条记录", ln)
    if m:
        a["records"] = int(m.group(1))

# 只看有完整尝试记录的轮次（含新航向之后的日志）
valid = [r for r in rounds if r["attempts"]]
print(f"日志: {LOG}")
print(f"含抓取尝试的轮次: {len(valid)}")

pos_stat = defaultdict(lambda: {"ok": 0, "fail": 0})
httpx_stat = Counter()
browser_stat = Counter()
route_stat = defaultdict(lambda: {"ok": 0, "fail": 0})
total = 0

for r in valid:
    for idx, a in enumerate(r["attempts"]):
        if a["records"] is None:
            continue
        total += 1
        pos = idx + 1
        good = a["records"] > 0
        pos_stat[pos]["ok" if good else "fail"] += 1
        route_stat[a["route"]]["ok" if good else "fail"] += 1
        httpx_stat[a["httpx"] or "?"] += 1
        if a["httpx"] == "ok":
            browser_stat["httpx直连:" + ("成功" if good else "失败")] += 1
        else:
            browser_stat["浏览器兜底:" + ("成功" if good else "失败")] += 1

print(f"\n有效抓取尝试总数: {total}")
print("\n按『同一轮内的第几次请求』统计成功率:")
for pos in sorted(pos_stat):
    s = pos_stat[pos]
    n = s["ok"] + s["fail"]
    print(f"  第 {pos} 次: 成功 {s['ok']:>3} / 失败 {s['fail']:>3}  -> {100*s['ok']/max(1,n):.0f}%")

print("\n按航向统计:")
for rt, s in sorted(route_stat.items()):
    n = s["ok"] + s["fail"]
    print(f"  {rt}: 成功 {s['ok']:>3} / 失败 {s['fail']:>3}  -> {100*s['ok']/max(1,n):.0f}%")

print("\nhttpx 结果分布:", dict(httpx_stat))
print("路径成率:", dict(browser_stat))

# 两次请求之间的实际间隔（同轮内）
gaps = []
for r in valid:
    if len(r["attempts"]) >= 2:
        gaps.append(len(r["attempts"]))
print("\n每轮请求次数分布:", dict(Counter(len(r['attempts']) for r in valid)))
