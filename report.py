"""生成自包含的 HTML 监控报告（无外部依赖，可离线打开、可被定时任务反复覆盖刷新）

支持单班次与多班次：
    python report.py -c config.yaml       # 单班次（CZ3417 页面）
    python report.py -c config.w5.yaml    # 多班次（晚间 5 班页面）
    python report.py -o 我的报告.html
    python report.py --flight CZ3417      # 覆盖要重点展示的航班号

设计要点：
* 单文件、无 CDN、无 JS 依赖 —— 定时任务生成后可直接双击打开，断网也能看
* 内联 SVG 画价格走势（多班次多序列，自己算坐标，不引图表库）
* 页面可配自动刷新（默认 300 秒），适合挂在副屏当监控面板
* 单班次时自动隐藏"班次对照表"，页面外观与单班次版本一致
"""
from __future__ import annotations

import argparse
import html
import json
import sqlite3
import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import yaml

try:
    from core.flights import carrier_name as _carrier_name
except Exception:                      # 只装了 PyYAML 的环境（如发布用 CI）也能单独跑
    def _carrier_name(flight_no):
        return ""

CARD_CSS = """
* { box-sizing: border-box; }
body {
  margin: 0; padding: 0 0 48px;
  font-family: -apple-system, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
  background: #f5f6f8; color: #1f2328; line-height: 1.55;
}
.wrap { max-width: 1180px; margin: 0 auto; padding: 0 20px; }
header {
  background: linear-gradient(135deg, #0b3d91 0%, #1f6feb 55%, #2da44e 100%);
  color: #fff; padding: 26px 0 22px; margin-bottom: 22px;
}
header h1 { margin: 0 0 6px; font-size: 24px; letter-spacing: .5px; }
header .sub { margin: 0; font-size: 15px; opacity: .95; }
header .meta { margin: 10px 0 0; font-size: 12.5px; opacity: .82; }
.banner {
  background: #fff8c5; border: 1px solid #d4a72c; color: #7d4e00;
  border-radius: 8px; padding: 10px 14px; margin: 0 0 18px; font-size: 13.5px;
}
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 14px; margin-bottom: 22px; }
.card {
  background: #fff; border: 1px solid #e3e6ea; border-radius: 10px;
  padding: 16px 18px; box-shadow: 0 1px 2px rgba(27,31,36,.04);
}
.card.primary { border-color: #1f6feb; box-shadow: 0 0 0 1px #1f6feb inset, 0 2px 6px rgba(31,111,235,.12); }
.card .label { display: block; font-size: 12.5px; color: #656d76; margin-bottom: 6px; }
.card .value { display: block; font-size: 28px; font-weight: 650; letter-spacing: -.5px; }
.card .note, .card .delta { display: block; font-size: 12px; margin-top: 4px; color: #656d76; }
.delta.up { color: #cf222e; }
.delta.down { color: #1a7f37; }
section {
  background: #fff; border: 1px solid #e3e6ea; border-radius: 10px;
  padding: 18px 20px 20px; margin-bottom: 20px;
}
section h2 { margin: 0 0 4px; font-size: 17px; }
section .hint { margin: 0 0 14px; font-size: 12.5px; color: #656d76; }
table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid #eaeef2; white-space: nowrap; }
th { background: #f6f8fa; color: #424a53; font-weight: 600; font-size: 12.5px; position: sticky; top: 0; }
tbody tr:hover { background: #f6f8fa; }
tr.target { background: #ddf4ff !important; font-weight: 600; }
tr.target td:first-child { box-shadow: inset 3px 0 0 #1f6feb; }
.mono { font-variant-numeric: tabular-nums; }
.badge { display: inline-block; font-size: 11px; padding: 1px 7px; border-radius: 20px; margin-left: 6px; vertical-align: 1px; }
.badge.warn { background: #fff1e5; color: #bc4c00; border: 1px solid #ffd8b5; }
.badge.info { background: #eef2ff; color: #3b41c5; border: 1px solid #d5dbff; }
.badge.ok { background: #dafbe1; color: #1a7f37; border: 1px solid #aceebb; }
.scroll { max-height: 620px; overflow: auto; border: 1px solid #eaeef2; border-radius: 8px; }
.grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px 26px; font-size: 13.5px; }
.grid2 div span { color: #656d76; display: inline-block; min-width: 74px; }
footer { color: #656d76; font-size: 12px; text-align: center; margin-top: 26px; }
.empty { padding: 26px; text-align: center; color: #656d76; }
code { background: #f6f8fa; padding: 1px 5px; border-radius: 4px; font-size: 12.5px; }
.toolbar { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin: 0 0 12px; }
.toolbar input[type=search] {
  flex: 1 1 260px; min-width: 200px; padding: 8px 11px; font-size: 13.5px;
  border: 1px solid #d0d7de; border-radius: 7px; background: #fff;
}
.toolbar input[type=search]:focus { outline: 2px solid #1f6feb33; border-color: #1f6feb; }
.toolbar select {
  padding: 8px 10px; font-size: 13.5px; border: 1px solid #d0d7de;
  border-radius: 7px; background: #fff;
}
.toolbar label { font-size: 13px; color: #424a53; display: inline-flex; align-items: center; gap: 5px; cursor: pointer; }
.toolbar .cnt { font-size: 12.5px; color: #656d76; margin-left: auto; }
"""


# 页面内查询 UI：纯原生 JS，不依赖任何 CDN/框架（离线、静态托管都可用）
# 注意：工具栏与脚本必须分开 —— 脚本要放在表格之后执行，否则取不到 #tbl 会静默失效。
# 用普通字符串常量而不是 f-string —— 里面的 {} 是 JS 语法，放进 f-string 会炸。
# {examples} / {arr_label} 由 build_html 按航向填充：成都→广州的页面不该出现
# "只看到达双流"这种写死给广州→成都的过滤条件（点了永远是空表）。
QUERY_TOOLBAR_TPL = """
    <div class="toolbar">
      <input type="search" id="q" placeholder="搜索航班号 / 航司 / 机场 / 时刻，例如 {examples}、15:15">
      <select id="sort">
        <option value="price-asc">价格 低 → 高</option>
        <option value="price-desc">价格 高 → 低</option>
        <option value="dep-asc">起飞时刻 早 → 晚</option>
        <option value="dep-desc">起飞时刻 晚 → 早</option>
      </select>
      <label><input type="checkbox" id="cheap"> 只看已低于阈值</label>
      <label><input type="checkbox" id="dest"> {arr_label}</label>
      <span class="cnt" id="cnt"></span>
    </div>
"""

QUERY_SCRIPT = """
<script>
(function () {
  var q = document.getElementById('q'),
      sort = document.getElementById('sort'),
      cheap = document.getElementById('cheap'),
      dest = document.getElementById('dest'),
      tbody = document.getElementById('tbl'),
      cnt = document.getElementById('cnt');
  if (!tbody) { return; }
  var all = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
  var threshold = parseFloat(tbody.getAttribute('data-threshold') || '0');

  function visible(tr) {
    var term = (q.value || '').trim().toLowerCase();
    if (term && tr.innerText.toLowerCase().indexOf(term) < 0) { return false; }
    if (cheap.checked && threshold > 0 && parseFloat(tr.dataset.price) > threshold) { return false; }
    // 目的地过滤按航向生成（data-arr=1 表示落在本页的目的地机场）
    if (dest && dest.checked && tr.dataset.arr !== '1') { return false; }
    return true;
  }
  function apply() {
    var shown = 0;
    all.forEach(function (tr) {
      var ok = visible(tr);
      tr.style.display = ok ? '' : 'none';
      if (ok) { shown++; }
    });
    cnt.textContent = '显示 ' + shown + ' / ' + all.length + ' 个航班';
  }
  function sortRows() {
    var mode = sort.value;
    var arr = all.slice();
    if (mode === 'price-asc') { arr.sort(function (a, b) { return a.dataset.price - b.dataset.price; }); }
    else if (mode === 'price-desc') { arr.sort(function (a, b) { return b.dataset.price - a.dataset.price; }); }
    else if (mode === 'dep-asc') { arr.sort(function (a, b) { return (a.dataset.dep || '99:99').localeCompare(b.dataset.dep || '99:99'); }); }
    else if (mode === 'dep-desc') { arr.sort(function (a, b) { return (b.dataset.dep || '').localeCompare(a.dataset.dep || ''); }); }
    arr.forEach(function (tr) { tbody.appendChild(tr); });
  }
  q.addEventListener('input', apply);
  cheap.addEventListener('change', apply);
  if (dest) { dest.addEventListener('change', apply); }
  sort.addEventListener('change', function () { sortRows(); apply(); });
  apply();
})();
</script>
"""


# --------------------------------------------------------------------------
# 数据读取
# --------------------------------------------------------------------------
def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def latest_round_rows(conn, window_minutes: int = 5,
                      from_code: str = "", to_code: str = "") -> tuple:
    """取最近一轮的记录：以最后一条抓取时间往前 window_minutes 分钟为界。

    from_code/to_code 用于**按航向过滤**：同一轮里可能同时抓了广州→成都与成都→广州，
    不过滤的话"当日全部航班价目"会把两个方向的航班混在一张表里。
    """
    mx = conn.execute("SELECT MAX(fetched_at) AS m FROM flight_prices").fetchone()["m"]
    if not mx:
        return [], None
    sql = "SELECT * FROM flight_prices WHERE fetched_at >= datetime(?, ?)"
    args = [mx, f"-{window_minutes} minutes"]
    if from_code:
        sql += " AND UPPER(from_city) = ?"
        args.append(from_code.upper())
    if to_code:
        sql += " AND UPPER(to_city) = ?"
        args.append(to_code.upper())
    sql += " ORDER BY price ASC"
    rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows], mx


def target_history(conn, flights: list, date: str) -> list:
    """目标航班的价格历史：按抓取时刻聚合成时间序列。"""
    if not flights:
        return []
    marks = ",".join("?" for _ in flights)
    sql = (f"SELECT fetched_at, MIN(price) AS price, COUNT(*) AS n "
           f"FROM flight_prices WHERE UPPER(REPLACE(flight_no,' ','')) IN ({marks})")
    args = [f.upper().replace(" ", "") for f in flights]
    if date:
        sql += " AND depart_date = ?"
        args.append(date)
    sql += " GROUP BY fetched_at ORDER BY fetched_at ASC"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def all_positions(conn, flights: list, date: str) -> list:
    """目标航班的所有抓取明细（含平台），用于说明每个平台各自的价格。"""
    if not flights:
        return []
    marks = ",".join("?" for _ in flights)
    sql = (f"SELECT * FROM flight_prices WHERE UPPER(REPLACE(flight_no,' ','')) IN ({marks})")
    args = [f.upper().replace(" ", "") for f in flights]
    if date:
        sql += " AND depart_date = ?"
        args.append(date)
    sql += " ORDER BY fetched_at DESC LIMIT 200"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


# --------------------------------------------------------------------------
# 内联 SVG 折线图
# --------------------------------------------------------------------------
def _nice_bounds(lo: float, hi: float) -> tuple[float, float]:
    if hi <= lo:
        hi = lo + max(20.0, lo * 0.06)
    pad = (hi - lo) * 0.18
    return lo - pad, hi + pad


def all_flights_history(conn, flights: list, date: str) -> dict:
    """一次查出所有监控班次的价格历史：{航班号: [{'fetched_at','price'}, ...]}"""
    if not flights:
        return {}
    marks = ",".join("?" for _ in flights)
    sql = (f"SELECT UPPER(REPLACE(flight_no,' ','')) AS fno, fetched_at, MIN(price) AS price "
           f"FROM flight_prices WHERE UPPER(REPLACE(flight_no,' ','')) IN ({marks})")
    args = [f.upper().replace(" ", "") for f in flights]
    if date:
        sql += " AND depart_date = ?"
        args.append(date)
    sql += " GROUP BY fno, fetched_at ORDER BY fetched_at ASC"
    out: dict = {}
    for r in conn.execute(sql, args):
        out.setdefault(r["fno"], []).append(
            {"fetched_at": r["fetched_at"], "price": float(r["price"])})
    return out


# 多班次折线图的配色（最多支持 8 个班次，超出后循环使用）
CHART_COLORS = ["#1f6feb", "#cf222e", "#1a7f37", "#8250df",
                "#bc4c00", "#0969da", "#bf3989", "#6e7781"]

LEGEND_CSS = """
.legend { display: flex; flex-wrap: wrap; gap: 6px 18px; margin: 10px 0 0; font-size: 12.5px; }
.legend .lg { display: inline-flex; align-items: center; gap: 6px; color: #424a53; }
.legend .lg i { width: 14px; height: 3px; border-radius: 2px; display: inline-block; }
.legend .lg b { font-weight: 600; }
"""


def svg_multi_chart(series: dict, threshold: float = 0, width: int = 1060,
                    height: int = 360) -> str:
    """多班次价格走势图。series: {航班号: [{'fetched_at','price'}, ...]}（各按时间升序）"""
    series = {k: v for k, v in (series or {}).items() if v}
    if not series:
        return '<div class="empty">暂无历史样本。跑够两轮抓取后这里会出现价格走势。</div>'

    times = sorted({p["fetched_at"] for v in series.values() for p in v})
    all_prices = [p["price"] for v in series.values() for p in v]
    lo, hi = _nice_bounds(min(all_prices), max(all_prices))
    if threshold and threshold > 0:
        lo = min(lo, threshold - (hi - lo) * 0.05)

    pad_l, pad_r, pad_t, pad_b = 68, 30, 30, 48
    inner_w, inner_h = width - pad_l - pad_r, height - pad_t - pad_b
    n = len(times)

    def x_at(i: int) -> float:
        return pad_l + (inner_w / 2 if n == 1 else inner_w * i / (n - 1))

    def y_at(v: float) -> float:
        return pad_t + inner_h * (1 - (v - lo) / (hi - lo))

    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
             f'role="img" aria-label="多班次价格走势" style="display:block">',
             f'<rect x="0" y="0" width="{width}" height="{height}" fill="#fff"/>']

    ticks = 4
    for k in range(ticks + 1):
        v = lo + (hi - lo) * k / ticks
        y = y_at(v)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
                     f'stroke="#eaeef2" stroke-width="1"/>')
        parts.append(f'<text x="{pad_l - 10}" y="{y + 4:.1f}" text-anchor="end" '
                     f'font-size="11.5" fill="#656d76">¥{v:.0f}</text>')

    if threshold and lo <= threshold <= hi:
        y = y_at(threshold)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
                     f'stroke="#cf222e" stroke-width="1.4" stroke-dasharray="6 5"/>')
        parts.append(f'<text x="{width - pad_r}" y="{y - 7:.1f}" text-anchor="end" '
                     f'font-size="11.5" fill="#cf222e">告警阈值 ¥{threshold:.0f}</text>')

    ordered = sorted(series.items(), key=lambda kv: kv[1][-1]["price"])
    for idx, (fno, pts) in enumerate(ordered):
        color = CHART_COLORS[idx % len(CHART_COLORS)]
        coords = [(x_at(times.index(p["fetched_at"])), y_at(p["price"])) for p in pts]
        if len(coords) > 1:
            line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
            parts.append(f'<polyline points="{line}" fill="none" stroke="{color}" '
                         f'stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round"/>')
        for x, y in coords:
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.8" fill="#fff" '
                         f'stroke="{color}" stroke-width="2.2"/>')
        lx, ly = coords[-1]
        dy = -12 if idx % 2 == 0 else 20
        label = f"{fno[2:] if len(fno) > 2 else fno} ¥{pts[-1]['price']:.0f}"
        parts.append(f'<text x="{lx + 7:.1f}" y="{ly + dy:.1f}" font-size="11" '
                     f'font-weight="600" fill="{color}">{html.escape(label)}</text>')

    for i in sorted({0, n - 1} | ({n // 2} if n >= 3 else set())):
        parts.append(f'<text x="{x_at(i):.1f}" y="{height - 16}" text-anchor="middle" '
                     f'font-size="11.5" fill="#656d76">{html.escape(str(times[i])[5:16])}</text>')
    parts.append('</svg>')

    items = []
    for idx, (fno, pts) in enumerate(ordered):
        color = CHART_COLORS[idx % len(CHART_COLORS)]
        items.append(f'<span class="lg"><i style="background:{color}"></i>'
                     f'{html.escape(fno)} <b>¥{pts[-1]["price"]:.0f}</b></span>')
    parts.append('<div class="legend">' + "".join(items) + '</div>')
    return "".join(parts)

def _minutes(t: str) -> int:
    try:
        hh, mm = t.split(":")
        return int(hh) * 60 + int(mm)
    except Exception:
        return -1


def _plausible_pair(dep: str, arr: str, lo: int = 80, hi: int = 220) -> bool:
    """起飞/到达是否构成一段合理的航程（默认 1h20m–3h40m）。

    广蓉航段实际约 2h20m。去哪儿的字段拼接会把邻座的起飞时刻串到某一行
    （例如 MF1192 出现 07:05→22:40，时长 15 小时），用时长区间就能识别出来。
    """
    if not dep or not arr:
        return False
    d, a = _minutes(dep), _minutes(arr)
    if d < 0 or a < 0:
        return False
    if a < d:
        a += 24 * 60                      # 跨零点
    return lo <= (a - d) <= hi


def _plausible_times(row: dict) -> bool:
    """时刻是否自洽：两者都有时要求航程合理；只有一个时无从判断，放行。"""
    d = (row.get("depart_time") or "").strip()
    a = (row.get("arrive_time") or "").strip()
    if d and a:
        return _plausible_pair(d, a)
    return bool(d or a)


def _pick_field(group: list, field: str) -> str:
    """从同一班次的若干行里挑一个最可信的字段值。

    时刻字段先用"航程合理"过滤（排除被拼接串到的值），再按出现次数投票取众数；
    机型/航司等其他字段直接投票。抗单条脏数据。
    """
    def collect(rows):
        vals = []
        for r in rows:
            if field == "aircraft":
                v = _aircraft_of(r)
            else:
                v = (r.get(field) or "").strip()
            if v:
                vals.append(v)
        return vals

    if field in ("depart_time", "arrive_time"):
        good = collect([r for r in group if _plausible_times(r)])
        if good:
            return Counter(good).most_common(1)[0][0]
    vals = collect(group)
    if not vals:
        return ""
    return Counter(vals).most_common(1)[0][0]


def compute_stats(rows: list, series: dict, watch: list, is_suspect, threshold: float = 0.0) -> dict:
    """按班次汇总当前价与历史统计。

    页面与 JSON 接口共用这一份逻辑 —— 否则两边很容易算出不同的数（曾经踩过）。
    """
    grouped: dict = {}
    for r in rows:
        fno = (r.get("flight_no") or "").upper().replace(" ", "")
        if fno not in watch or r.get("route_level"):
            continue
        grouped.setdefault(fno, []).append(r)

    current = {}
    for fno, group in grouped.items():
        # 价格取最低；同价时优先时刻自洽的那条（避免选中被污染的记录）
        sane = [r for r in group if not is_suspect(r["price"])]
        pool = sane or group
        current[fno] = min(pool, key=lambda r: (r["price"], 0 if _plausible_times(r) else 1))

    stats = []
    for fno in watch:
        pts = series.get(fno) or []
        prices = [p["price"] for p in pts]
        cur = current.get(fno)
        group = grouped.get(fno, [])
        cur_price = float(cur["price"]) if cur else (prices[-1] if prices else None)
        # 展示用字段跨行补齐：单个字段脏了也能从别的行拿到干净值
        dep_t = (cur.get("depart_time") or "").strip() if cur else ""
        if not _plausible_times(cur or {}):
            dep_t = _pick_field(group, "depart_time")
        cur_price = float(cur["price"]) if cur else (prices[-1] if prices else None)
        stats.append({
            "flight_no": fno,
            "airline": _carrier_name(fno) or _pick_field(group, "airline")
                       or (cur or {}).get("airline", ""),
            "aircraft": _pick_field(group, "aircraft") or _aircraft_of(cur),
            "depart_time": dep_t,
            "arrive_time": _pick_field(group, "arrive_time")
                           or (cur or {}).get("arrive_time", "") or "",
            "dep_airport": _airport_of(cur)[0] if cur else "",
            "arr_airport": _airport_of(cur)[1] if cur else "",
            "current": cur_price,
            "min": min(prices) if prices else None,
            "max": max(prices) if prices else None,
            "avg": (sum(prices) / len(prices)) if prices else None,
            "samples": len(prices),
            "first": prices[0] if prices else None,
            "delta": (cur_price - prices[0]) if (cur_price is not None and prices) else None,
            "fetched": (cur or {}).get("fetched_at", ""),
            "platform": (cur or {}).get("platform", ""),
            "below_threshold": bool(threshold > 0 and cur_price is not None
                                    and cur_price <= threshold),
        })
    ranked = sorted([s for s in stats if s["current"] is not None],
                    key=lambda s: s["current"])
    return {
        "stats": stats,
        "current": current,
        "ranked": ranked,
        "cheapest": ranked[0] if ranked else None,
        "missing": [s["flight_no"] for s in stats if s["current"] is None],
        "total_samples": max([s["samples"] for s in stats] or [0]),
    }

def _esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


def _airport_of(row: dict) -> tuple[str, str]:
    try:
        d = json.loads(row.get("extra") or "{}")
    except Exception:
        d = {}
    dep = d.get("dep_airport", "")
    arr = d.get("arr_airport", "")
    dep_id = d.get("dep_airport_id", "")
    arr_id = d.get("arr_airport_id", "")
    dep_t = d.get("dep_terminal", "")
    arr_t = d.get("arr_terminal", "")
    dep_s = f"{dep}{dep_t}" + (f"({dep_id})" if dep_id else "")
    arr_s = f"{arr}{arr_t}" + (f"({arr_id})" if arr_id else "")
    return dep_s, arr_s


def price_guard(rows: list, ratio: float = 0.35) -> tuple[float, callable]:
    """识别"疑似被拼接截断的价格"。

    去哪儿的字段拼接会偶尔把 `"minPrice":"1270"` 截成 `127` 这类假低价
    （实测见过 53 / 59 / 112 / 127，都对应真实存在的四位数价格）。
    这里用整轮价格的中位数做锚：低于中位数 35% 的视为可疑，
    只在展示与"当日最低"里标注/排除，不直接丢掉原始数据。
    返回 (阈值, 判定函数)。
    """
    prices = sorted(float(r["price"]) for r in rows if r.get("price"))
    if len(prices) < 8:
        return 0.0, (lambda p: False)
    med = statistics.median(prices)
    cut = med * ratio
    return cut, (lambda p: float(p) < cut)


def _aircraft_of(row: dict) -> str:
    """从 extra 里取机型。"""
    if not row:
        return ""
    try:
        return json.loads(row.get("extra") or "{}").get("aircraft", "") or ""
    except Exception:
        return ""


def _badge(text: str, kind: str = "info") -> str:
    return f'<span class="badge {kind}">{html.escape(text)}</span>'


def build_html(cfg: dict, conn, out_path: str) -> str:
    out = cfg.get("output") or {}
    route = (cfg.get("routes") or [{}])[0]
    watch = [str(w).upper().replace(" ", "") for w in (route.get("watch_flights") or [])]
    date = (route.get("dates") or [""])[0]
    threshold = float(route.get("alert_threshold") or 0)
    from_name = route.get("from_name", route.get("from", ""))
    to_name = route.get("to_name", route.get("to", ""))
    from_code = route.get("from", "")
    to_code = route.get("to", "")
    line_label = f"{from_name}({from_code}) → {to_name}({to_code})"
    refresh = int(out.get("report_refresh_seconds", 300) or 0)
    multi = len(watch) > 1
    flight_word = "监控班次" if multi else "目标航班"
    primary_label = (watch[0] if watch else "机票")

    latest = latest_round_rows(conn, from_code=from_code, to_code=to_code)
    rows, last_fetch = latest if isinstance(latest, tuple) else ([], None)
    series = all_flights_history(conn, watch, date)
    positions = all_positions(conn, watch, date)
    suspect_cut, is_suspect = price_guard(rows)

    # ---- 各班次当前价与历史统计（与 JSON 接口共用 compute_stats，避免两处算法分叉）----
    st = compute_stats(rows, series, watch, is_suspect, threshold)
    stats = st["stats"]
    ranked = st["ranked"]
    cheapest = st["cheapest"]
    missing = st["missing"]
    total_samples = st["total_samples"]

    # ---- 陈旧提醒 ----
    stale = ""
    if last_fetch:
        try:
            age = (datetime.now() - datetime.strptime(
                last_fetch, "%Y-%m-%d %H:%M:%S")).total_seconds() / 60
            interval = float((cfg.get("schedule") or {}).get("interval_minutes", 90) or 90)
            # 阈值取 max(3×间隔, 240 分钟)：抓取本身约有一半轮次会被风控拦掉，
            # 用 3×间隔 判"任务停了"在 30 分钟节奏下会误报（90 分钟没数据很常见）。
            limit = max(interval * 3, 240)
            if age > limit:
                stale = (f'<div class="banner">⚠️ 最近一次抓取是 {age:.0f} 分钟前'
                         f'（配置间隔 {interval:.0f} 分钟，告警阈值 {limit:.0f} 分钟），'
                         f'定时任务可能已停止或持续抓取失败，'
                         f'请检查计划任务与 <code>logs/monitor.log</code>。</div>')
        except Exception:
            pass

    if missing:
        stale += (f'<div class="banner">本轮未抓到：'
                  f'{"、".join(html.escape(m) for m in missing)}'
                  f'（可能未执飞 / 售罄 / 平台未返回该班次）。</div>')

    # ---- 卡片 ----
    cards = []
    if multi:
        summary_bits = []
        if cheapest:
            summary_bits.append(f"最低 <b>{html.escape(cheapest['flight_no'])}</b> "
                                f"¥{cheapest['current']:.0f}")
        summary_bits.append(f"{len(watch)} 个班次")
        summary_bits.append(f"{total_samples} 轮样本")
        cards.append(f"""
  <div class="card">
    <span class="label">本轮概览</span>
    <span class="value" style="font-size:20px">{' · '.join(summary_bits)}</span>
    <span class="note">最近抓取 {html.escape(last_fetch or '—')} · 数据源
      {html.escape(' / '.join(cfg.get('platforms') or []) or '—')}</span>
  </div>""")

    for s in stats:
        is_best = cheapest is not None and s["flight_no"] == cheapest["flight_no"]
        if s["current"] is None:
            cards.append(f"""
  <div class="card">
    <span class="label">{html.escape(s['flight_no'])} {html.escape(s['airline'])}</span>
    <span class="value" style="font-size:19px;color:#8c959f">本轮未抓到</span>
    <span class="note">{html.escape(s['depart_time'] or '--:--')} → {html.escape(s['arrive_time'] or '--:--')}</span>
  </div>""")
            continue
        delta = s["delta"]
        cls = "" if not delta else ("down" if delta < 0 else "up")
        delta_txt = ("较首次样本 持平" if (delta is not None and abs(delta) < 0.5)
                     else (f"较首次 {'+' if delta > 0 else ''}¥{delta:.0f}"
                           if delta is not None else "样本不足"))
        rng = (f"区间 ¥{s['min']:.0f}–¥{s['max']:.0f}"
               if s["min"] is not None else "无历史")
        below = threshold > 0 and s["current"] <= threshold
        cards.append(f"""
  <div class="card{' primary' if is_best else ''}">
    <span class="label">{html.escape(s['flight_no'])} {html.escape(s['airline'])}
      {'<b style="color:#1f6feb">当前最低</b>' if is_best and multi else ''}</span>
    <span class="value" style="font-size:26px">¥{s['current']:.0f}</span>
    <span class="note">{html.escape(s['depart_time'] or '--:--')} → {html.escape(s['arrive_time'] or '--:--')}
      {'· ' + html.escape(s['arr_airport']) if s['arr_airport'] else ''}</span>
    <span class="delta {cls}">{html.escape(delta_txt)} · {html.escape(rng)}</span>
    {('<span class="delta down">已低于阈值 ¥%.0f</span>' % threshold) if below else ''}
  </div>""")
    cards_html = "\n".join(cards)

    # ---- 走势图 ----
    chart = svg_multi_chart(series, threshold)

    # ---- 班次对照表（多班次时才有意义）----
    compare = ""
    if multi and stats:
        trs = []
        for i, s in enumerate(ranked):
            gap = ""
            if cheapest and s["current"] is not None and i > 0:
                gap = f"+¥{s['current'] - cheapest['current']:.0f}"
            elif i == 0:
                gap = "最低"
            state = []
            if i == 0:
                state.append(_badge("当前最低", "ok"))
            if threshold > 0 and s["current"] is not None and s["current"] <= threshold:
                state.append(_badge("已低于阈值", "info"))
            if s["depart_time"] and s["arrive_time"] \
                    and not _plausible_pair(s["depart_time"], s["arrive_time"]):
                state.append(_badge("时刻待核实", "warn"))
            trs.append(f"""<tr data-flight="{html.escape(s['flight_no'])}" data-price="{s['current']:.0f}">
      <td class="mono">{html.escape(s['flight_no'])}</td>
      <td>{html.escape(s['airline'])}</td>
      <td>{html.escape(s['aircraft'])}</td>
      <td class="mono">{html.escape(s['depart_time'] or '--:--')} → {html.escape(s['arrive_time'] or '--:--')}</td>
      <td>{html.escape(s['arr_airport'])}</td>
      <td class="mono"><b>¥{s['current']:.0f}</b></td>
      <td class="mono">{gap}</td>
      <td class="mono">{('¥%.0f–¥%.0f' % (s['min'], s['max'])) if s['min'] is not None else '—'}</td>
      <td class="mono">{('¥%.0f' % s['avg']) if s['avg'] is not None else '—'}</td>
      <td class="mono">{s['samples']}</td>
      <td>{''.join(state)}</td>
    </tr>""")
        for s in stats:
            if s["current"] is None:
                trs.append(f"""<tr>
      <td class="mono">{html.escape(s['flight_no'])}</td>
      <td>{html.escape(s['airline'])}</td><td>—</td>
      <td class="mono">{html.escape(s['depart_time'] or '--:--')} → {html.escape(s['arrive_time'] or '--:--')}</td>
      <td>{html.escape(s['arr_airport'])}</td><td>—</td><td>—</td><td>—</td><td>—</td>
      <td class="mono">{s['samples']}</td><td>{_badge('本轮未抓到', 'warn')}</td>
    </tr>""")
        compare = f"""
  <section>
    <h2>{flight_word}对照</h2>
    <p class="hint">同一航线同一天的各班次横向对比，按当前价升序。价格均为平台展示的经济舱最低价。</p>
    <div class="scroll"><table>
      <thead><tr>
        <th>航班</th><th>航司</th><th>机型</th><th>时刻</th><th>到达</th>
        <th>当前价</th><th>较最低</th><th>历史区间</th><th>均价</th><th>样本</th><th>状态</th>
      </tr></thead>
      <tbody>{''.join(trs)}</tbody>
    </table></div>
  </section>"""

    # ---- 当日全部航班价目 ----
    by_flight: dict = {}
    for r in rows:
        by_flight.setdefault(r.get("flight_no") or "（全航线最低价）", []).append(r)
    table_rows = []
    for key, group in by_flight.items():
        best = min(group, key=lambda x: x["price"])
        platforms = " / ".join(f"{g['platform']}¥{float(g['price']):.0f}" for g in
                               sorted(group, key=lambda x: x["price"]))
        dep_s, arr_s = _airport_of(best)
        notes = ""
        if best.get("depart_time") and best.get("arrive_time") \
                and best["depart_time"] > best["arrive_time"]:
            notes += _badge("起飞时刻可疑", "warn")
        if is_suspect(best["price"]):
            notes += _badge("价格可疑", "warn")
        if not best.get("flight_no"):
            notes += _badge("仅全航线最低价", "info")
        is_watched = key in watch
        if is_watched:
            notes += _badge(flight_word, "ok")
        try:
            arr_id = (json.loads(best.get("extra") or "{}").get("arr_airport_id") or "").upper()
        except Exception:
            arr_id = ""
        table_rows.append((best["price"], f"""<tr class="{'target' if is_watched else ''}" data-price="{float(best['price']):.0f}" data-dep="{html.escape(best.get('depart_time') or '')}" data-arr="{1 if arr_id == to_code.upper() else 0}">
      <td class="mono">{html.escape(key)}</td>
      <td>{html.escape(best.get('airline') or '')}</td>
      <td>{html.escape(_aircraft_of(best))}</td>
      <td class="mono">{html.escape(best.get('depart_time') or '--:--')} → {html.escape(best.get('arrive_time') or '--:--')}</td>
      <td>{html.escape(dep_s)}</td>
      <td>{html.escape(arr_s)}</td>
      <td class="mono"><b>¥{float(best['price']):.0f}</b></td>
      <td style="font-size:12.5px;color:#656d76">{html.escape(platforms)}</td>
      <td>{notes}</td>
    </tr>"""))
    table_rows.sort(key=lambda t: t[0])
    table_body = "\n".join(h for _, h in table_rows) or \
        '<tr><td colspan="9" class="empty">暂无数据</td></tr>'

    ctu = [r for r in rows
           if json.loads(r.get("extra") or "{}").get("arr_airport_id") == "CTU"
           and not is_suspect(r["price"])]
    tfu = [r for r in rows
           if json.loads(r.get("extra") or "{}").get("arr_airport_id") == "TFU"
           and not is_suspect(r["price"])]
    market = ""
    if ctu or tfu:
        ctu_min = f"¥{min(r['price'] for r in ctu):.0f}" if ctu else "—"
        tfu_min = f"¥{min(r['price'] for r in tfu):.0f}" if tfu else "—"
        market = (f'<p class="hint">同日市场参考：落 <b>双流 CTU</b> 最低 {ctu_min}'
                  f'（{len(ctu)} 班） · 落 <b>天府 TFU</b> 最低 {tfu_min}（{len(tfu)} 班）。'
                  f'平台会把同一物理航班按多个共享航班号重复列示，价格相同。</p>')

    # ---- 抓取明细 ----
    detail = ""
    if positions:
        trs = []
        for p in positions[:60]:
            trs.append(f"""<tr>
      <td class="mono">{html.escape(p['fetched_at'])}</td>
      <td class="mono">{html.escape(p.get('flight_no') or '')}</td>
      <td>{html.escape(p['platform'])}</td>
      <td class="mono">{html.escape(p.get('depart_time') or '--:--')} → {html.escape(p.get('arrive_time') or '--:--')}</td>
      <td class="mono">¥{float(p['price']):.0f}</td>
      <td>{html.escape(p.get('airline') or '')}</td>
    </tr>""")
        detail = f"""
  <section>
    <h2>{flight_word}抓取明细</h2>
    <p class="hint">每次抓到这些班次的原始记录（最新在前，最多 60 条）。</p>
    <div class="scroll"><table>
      <thead><tr><th>抓取时间</th><th>航班</th><th>平台</th><th>时刻</th><th>价格</th><th>航司</th></tr></thead>
      <tbody>{''.join(trs)}</tbody>
    </table></div>
  </section>"""

    # ---- 数据源实际状态：配置里写了"携程+去哪儿"，但某个源本轮可能没数据
    #      （携程有 Whale Guard 风控，未过人机验证时会返回 whaleguard block）。
    #      页面必须如实呈现，不能让读者以为两边的价都拿到了。
    configured = list(cfg.get("platforms") or [])
    present: dict = {}
    for r in rows:
        present[r.get("platform", "?")] = present.get(r.get("platform", "?"), 0) + 1
    plat_bits = []
    for p in configured:
        n = present.get(p, 0)
        if n:
            plat_bits.append(f'{html.escape(p)} <span class="badge ok">有效 {n} 条</span>')
        else:
            plat_bits.append(f'{html.escape(p)} <span class="badge warn">本轮无数据</span>')
    extra = [p for p in present if p not in configured]
    for p in extra:
        plat_bits.append(f'{html.escape(p)} <span class="badge info">{present[p]} 条</span>')
    source_state = " · ".join(plat_bits) or "—"

    refresh_tag = (f'<meta http-equiv="refresh" content="{refresh}">' if refresh > 0 else "")
    refresh_note = f"页面每 {refresh} 秒自动刷新" if refresh > 0 else "自动刷新已关闭"
    gen_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    title = " · ".join(watch) if multi else (watch[0] if watch else "机票")
    # 单班次页面保持原来的标题形态；多班次用航线 + 班次数的标题
    h1 = (f"✈️ {html.escape(from_name)} → {html.escape(to_name)} 机票监控"
          if multi else f"✈️ {html.escape(primary_label)} 价格监控")

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} 机票监控</title>
{refresh_tag}
<style>{CARD_CSS}{LEGEND_CSS}</style>
</head>
<body>
<header>
  <div class="wrap">
    <h1>{h1}</h1>
    <p class="sub">{html.escape(' · '.join(watch)) if watch else '—'} · {html.escape(date)} ·
      共 {len(watch)} 个{flight_word}</p>
    <p class="meta">生成时间 {html.escape(gen_at)} · 最近抓取 {html.escape(last_fetch or '—')} ·
      数据源 {source_state} · {html.escape(refresh_note)}</p>
  </div>
</header>

<div class="wrap">
  {stale}
  <div class="cards">{cards_html}</div>

  <section>
    <h2>价格走势</h2>
    <p class="hint">各{flight_word}在每次抓取中的最低价。样本越多趋势越有意义；虚线为告警阈值。</p>
    {chart}
  </section>

  {compare}

  <section>
    <h2>当日全部航班价目</h2>
    {market or '<p class="hint">同轮抓取到的全部航班，按价格升序。监控班次已高亮。</p>'}
    {QUERY_TOOLBAR_TPL.format(
        examples='、'.join(watch[:3]) or 'CZ3417',
        arr_label=f'只看到达{to_name} {to_code}')}
    <div class="scroll"><table>
      <thead><tr>
        <th>航班</th><th>航司</th><th>机型</th><th>时刻</th>
        <th>出发</th><th>到达</th><th>价格</th><th>平台明细</th><th>备注</th>
      </tr></thead>
      <tbody id="tbl" data-threshold="{threshold:.0f}">{table_body}</tbody>
    </table></div>
  </section>

  {detail}

  <section>
    <h2>运行状态</h2>
    <div class="grid2">
      <div><span>监控航线</span>{html.escape(line_label)}</div>
      <div><span>监控日期</span>{html.escape(date)}</div>
      <div><span>{flight_word}</span>{html.escape(' · '.join(watch) or '（未设置）')}</div>
      <div><span>起飞时刻窗</span>{html.escape(route.get('depart_time_from') or '不限')} – {html.escape(route.get('depart_time_to') or '不限')}</div>
      <div><span>告警阈值</span>{html.escape(('¥%.0f' % threshold) if threshold else '未设置')}</div>
      <div><span>抓取间隔</span>{html.escape(str((cfg.get('schedule') or {}).get('interval_minutes', '—')))} 分钟 ±
        {html.escape(str((cfg.get('schedule') or {}).get('jitter_minutes', 0)))} 分钟</div>
      <div><span>本轮记录数</span>{len(rows)} 条</div>
      <div><span>历史样本</span>{total_samples} 轮</div>
      <div style="grid-column:1/-1"><span>数据源</span>{source_state}</div>
    </div>
  </section>

  <footer>
    数据来自公开页面抓取，仅供个人出行参考，不含会员价 / 券后价，实际下单以航司页面为准。<br>
    项目基于 <a href="https://github.com/yangka1212/JiPiao">yangka1212/JiPiao</a>（MIT）改造：增加按航班号解析与告警。
  </footer>
</div>
{QUERY_SCRIPT}
</body>
</html>
"""
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(doc, encoding="utf-8")
    return out_path

def build_report(db_path: str, out_path: str, cfg: dict) -> str:
    """供 main.py 调用的入口：生成报告并返回路径。"""
    conn = connect(db_path)
    try:
        return build_html(cfg, conn, out_path)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 静态托管部署包（Cloudflare Pages / Workers 静态资源）
# --------------------------------------------------------------------------
PAGES_HEADERS = """/*
  Cache-Control: public, max-age=60
  X-Content-Type-Options: nosniff
  X-Frame-Options: SAMEORIGIN
  Referrer-Policy: no-referrer
"""


def _flight_row_json(r: dict) -> dict:
    dep_s, arr_s = _airport_of(r)
    try:
        d = json.loads(r.get("extra") or "{}")
    except Exception:
        d = {}
    return {
        "flight_no": r.get("flight_no") or "",
        "airline": r.get("airline") or "",
        "aircraft": d.get("aircraft", "") or "",
        "depart_time": r.get("depart_time") or "",
        "arrive_time": r.get("arrive_time") or "",
        "dep_airport": dep_s,
        "arr_airport": arr_s,
        "dep_airport_id": d.get("dep_airport_id", ""),
        "arr_airport_id": d.get("arr_airport_id", ""),
        "price": float(r["price"]),
        "platform": r.get("platform", ""),
        "route_level": bool(r.get("route_level")),
        "fetched_at": r.get("fetched_at", ""),
    }


def build_deploy_bundle(cfg: dict, conn, deploy_dir: str, db_path: str = "") -> dict:
    """生成可直接静态托管的部署包，返回摘要信息。

    产出::

        index.html    报告页（含页面内查询 UI）
        latest.json   最近一轮全部航班 + 各监控班次当前价（供外部程序查询）
        history.json  每个监控班次的价格历史序列
        meta.json     摘要（各班次当前价/区间/样本数/更新时间）
        _headers      Cloudflare Pages 的缓存与安全响应头
    """
    d = Path(deploy_dir)
    d.mkdir(parents=True, exist_ok=True)

    build_html(cfg, conn, str(d / "index.html"))

    route = (cfg.get("routes") or [{}])[0]
    watch = [str(w).upper().replace(" ", "") for w in (route.get("watch_flights") or [])]
    date = (route.get("dates") or [""])[0]
    threshold = float(route.get("alert_threshold") or 0)

    latest = latest_round_rows(conn, from_code=route.get("from", ""),
                               to_code=route.get("to", ""))
    rows, last_fetch = latest if isinstance(latest, tuple) else ([], None)
    series = all_flights_history(conn, watch, date)
    # 平台本轮实际产出（页面与 JSON 共用同一份统计）
    present: dict = {}
    for r in rows:
        key = r.get("platform", "?")
        present[key] = present.get(key, 0) + 1
    _, is_suspect = price_guard(rows)
    st = compute_stats(rows, series, watch, is_suspect, threshold)
    stats, cheapest = st["stats"], st["cheapest"]

    # 同轮内按航班号归并，取跨平台最低（含未被监控的班次，方便外部程序查全量）
    by_flight: dict = {}
    for r in rows:
        by_flight.setdefault(r.get("flight_no") or "（全航线最低价）", []).append(r)
    flights = []
    for key, group in by_flight.items():
        best = min(group, key=lambda x: x["price"])
        item = _flight_row_json(best)
        item["flight_no"] = key if key != "（全航线最低价）" else ""
        item["watched"] = key in watch
        item["price_suspect"] = bool(is_suspect(best["price"]))
        item["platforms"] = [
            {"platform": g["platform"], "price": float(g["price"])}
            for g in sorted(group, key=lambda x: x["price"])
        ]
        flights.append(item)
    flights.sort(key=lambda x: x["price"])

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    targets = {s["flight_no"]: {
        "flight_no": s["flight_no"],
        "airline": s["airline"],
        "aircraft": s["aircraft"],
        "depart_time": s["depart_time"],
        "arrive_time": s["arrive_time"],
        "dep_airport": s["dep_airport"],
        "arr_airport": s["arr_airport"],
        "price": s["current"],
        "platform": s["platform"],
        "min_price": s["min"],
        "max_price": s["max"],
        "avg_price": round(s["avg"], 1) if s["avg"] is not None else None,
        "sample_count": s["samples"],
        "below_threshold": s["below_threshold"],
        "cheapest_among_watched": bool(cheapest and s["flight_no"] == cheapest["flight_no"]),
    } for s in stats}

    latest_doc = {
        "generated_at": now,
        "last_fetch": last_fetch,
        "date": date,
        "route": {"from": route.get("from", ""), "to": route.get("to", ""),
                  "from_name": route.get("from_name", ""), "to_name": route.get("to_name", "")},
        "watch_flights": watch,
        "threshold": threshold,
        "targets": targets,
        # 向后兼容：单班次页面/旧调用方按 target 取第一班
        "target": targets.get(watch[0]) if watch else None,
        "cheapest_among_watched": (targets.get(cheapest["flight_no"]) if cheapest else None),
        "cheapest": next((f for f in flights if not f["price_suspect"]),
                         (flights[0] if flights else None)),
        "flight_count": len(flights),
        "flights": flights,
    }
    (d / "latest.json").write_text(json.dumps(latest_doc, ensure_ascii=False, indent=2),
                                   encoding="utf-8")

    history_doc = {
        "generated_at": now,
        "date": date,
        "watch_flights": watch,
        "sample_count": st["total_samples"],
        "series": {fno: [{"fetched_at": p["fetched_at"], "price": p["price"]}
                         for p in (series.get(fno) or [])] for fno in watch},
        # 向后兼容：samples 仍是第一班的历史
        "samples": [{"fetched_at": p["fetched_at"], "price": p["price"]}
                    for p in (series.get(watch[0]) if watch else []) or []],
    }
    (d / "history.json").write_text(json.dumps(history_doc, ensure_ascii=False, indent=2),
                                    encoding="utf-8")

    meta = {
        "updated_at": now,
        "last_fetch": last_fetch,
        "date": date,
        "route": {"from": route.get("from", ""), "to": route.get("to", ""),
                  "from_name": route.get("from_name", ""), "to_name": route.get("to_name", "")},
        "watch_flights": watch,
        "threshold": threshold,
        "flight": watch[0] if watch else "",
        "current_price": (targets.get(watch[0]) or {}).get("price") if watch else None,
        "min_price": (targets.get(watch[0]) or {}).get("min_price") if watch else None,
        "max_price": (targets.get(watch[0]) or {}).get("max_price") if watch else None,
        "avg_price": (targets.get(watch[0]) or {}).get("avg_price") if watch else None,
        "sample_count": st["total_samples"],
        "flights": [targets[s["flight_no"]] for s in stats],
        "cheapest_flight": cheapest["flight_no"] if cheapest else None,
        "cheapest_price": cheapest["current"] if cheapest else None,
        "missing_flights": st["missing"],
        "flight_count": len(flights),
        "source": cfg.get("platforms") or [],
        # 每个平台本轮实际产出多少条：携程被风控拦截时这里会是 0，页面与接口都能看出来
        "platform_status": {p: present.get(p, 0)
                            for p in (cfg.get("platforms") or [])},
    }
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    (d / "_headers").write_text(PAGES_HEADERS, encoding="utf-8")

    return {"deploy_dir": str(d), "meta": meta, "files": sorted(p.name for p in d.iterdir())}

def main():
    ap = argparse.ArgumentParser(description="生成机票监控 HTML 报告")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("-o", "--out", default="")
    ap.add_argument("--flight", default="", help="覆盖要重点展示的航班号，如 CZ3417")
    ap.add_argument("--no-refresh", action="store_true", help="页面不自动刷新")
    ap.add_argument("--deploy-dir", default="",
                    help="同时生成静态托管部署包（index.html + JSON 查询接口）到该目录")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"配置文件不存在: {cfg_path}", file=sys.stderr)
        return 1
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    out_cfg = cfg.setdefault("output", {})
    out_path = args.out or out_cfg.get("report_html") or "report.html"
    if args.no_refresh:
        out_cfg["report_refresh_seconds"] = 0
    if args.flight:
        routes = cfg.setdefault("routes", [{}])
        if not routes:
            routes.append({})
        routes[0]["watch_flights"] = [args.flight]

    db_path = out_cfg.get("db_path") or "data/prices.db"
    if not Path(db_path).exists():
        print(f"数据库不存在: {db_path}（先跑一次 python main.py --once）", file=sys.stderr)
        return 1
    path = build_report(db_path, out_path, cfg)
    size = Path(path).stat().st_size
    print(f"报告已生成: {path}  ({size / 1024:.1f} KB)")
    print(f"用浏览器打开: file:///{Path(path).resolve().as_posix()}")

    deploy_dir = args.deploy_dir or out_cfg.get("deploy_dir") or ""
    if deploy_dir:
        conn = connect(db_path)
        try:
            info = build_deploy_bundle(cfg, conn, deploy_dir, db_path)
        finally:
            conn.close()
        m = info["meta"]
        print(f"部署包已生成: {info['deploy_dir']}  -> {', '.join(info['files'])}")
        print(f"  当前价 ¥{m['current_price']}  区间 ¥{m['min_price']}–¥{m['max_price']}  "
              f"样本 {m['sample_count']}  航班 {m['flight_count']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
