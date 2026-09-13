"""生成自包含的 HTML 监控报告（无外部依赖，可离线打开、可被定时任务反复覆盖刷新）

用法：
    python report.py                      # 用 config.yaml，输出 report.html
    python report.py -o 我的报告.html
    python report.py --flight CZ3417      # 覆盖要重点展示的航班号

设计要点：
* 单文件、无 CDN、无 JS 依赖 —— 定时任务生成后可直接双击打开，断网也能看
* 内联 SVG 画价格走势（自己算坐标，不引图表库）
* 页面可配自动刷新（默认 300 秒），适合挂在副屏当监控面板
"""
from __future__ import annotations

import argparse
import html
import json
import sqlite3
import statistics
import sys
from datetime import datetime
from pathlib import Path

import yaml

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
QUERY_TOOLBAR = """
    <div class="toolbar">
      <input type="search" id="q" placeholder="搜索航班号 / 航司 / 机场 / 时刻，例如 CZ3417、双流、15:15">
      <select id="sort">
        <option value="price-asc">价格 低 → 高</option>
        <option value="price-desc">价格 高 → 低</option>
        <option value="dep-asc">起飞时刻 早 → 晚</option>
        <option value="dep-desc">起飞时刻 晚 → 早</option>
      </select>
      <label><input type="checkbox" id="cheap"> 只看已低于阈值</label>
      <label><input type="checkbox" id="direct"> 只看到达双流 CTU</label>
      <span class="cnt" id="cnt"></span>
    </div>
"""

QUERY_SCRIPT = """
<script>
(function () {
  var q = document.getElementById('q'),
      sort = document.getElementById('sort'),
      cheap = document.getElementById('cheap'),
      direct = document.getElementById('direct'),
      tbody = document.getElementById('tbl'),
      cnt = document.getElementById('cnt');
  if (!tbody) { return; }
  var all = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
  var threshold = parseFloat(tbody.getAttribute('data-threshold') || '0');

  function visible(tr) {
    var term = (q.value || '').trim().toLowerCase();
    if (term && tr.innerText.toLowerCase().indexOf(term) < 0) { return false; }
    if (cheap.checked && threshold > 0 && parseFloat(tr.dataset.price) > threshold) { return false; }
    if (direct.checked && tr.dataset.ctu !== '1') { return false; }
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
  direct.addEventListener('change', apply);
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


def latest_round_rows(conn, window_minutes: int = 5) -> list:
    """取最近一轮的记录：以最后一条抓取时间往前 window_minutes 分钟为界。"""
    mx = conn.execute("SELECT MAX(fetched_at) AS m FROM flight_prices").fetchone()["m"]
    if not mx:
        return []
    rows = conn.execute(
        "SELECT * FROM flight_prices WHERE fetched_at >= datetime(?, ?) ORDER BY price ASC",
        (mx, f"-{window_minutes} minutes"),
    ).fetchall()
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


def svg_price_chart(points: list, threshold: float = 0, width: int = 1060,
                    height: int = 280) -> str:
    """points: [{'fetched_at': str, 'price': float}]（按时间升序）"""
    if not points:
        return '<div class="empty">暂无历史样本。跑够两轮抓取后这里会出现价格走势。</div>'

    pad_l, pad_r, pad_t, pad_b = 62, 22, 26, 42
    inner_w = width - pad_l - pad_r
    inner_h = height - pad_t - pad_b
    prices = [float(p["price"]) for p in points]
    lo, hi = _nice_bounds(min(prices), max(prices))
    if threshold and threshold > 0:
        lo = min(lo, threshold - (hi - lo) * 0.05)
    n = len(points)

    def x_at(i: int) -> float:
        return pad_l + (inner_w / 2 if n == 1 else inner_w * i / (n - 1))

    def y_at(v: float) -> float:
        return pad_t + inner_h * (1 - (v - lo) / (hi - lo))

    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
             f'role="img" aria-label="价格走势" style="display:block">',
             f'<rect x="0" y="0" width="{width}" height="{height}" fill="#fff"/>']

    # 横向网格 + y 轴刻度
    ticks = 4
    for k in range(ticks + 1):
        v = lo + (hi - lo) * k / ticks
        y = y_at(v)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
                     f'stroke="#eaeef2" stroke-width="1"/>')
        parts.append(f'<text x="{pad_l - 10}" y="{y + 4:.1f}" text-anchor="end" '
                     f'font-size="11.5" fill="#656d76">¥{v:.0f}</text>')

    # 阈值线
    if threshold and lo <= threshold <= hi:
        y = y_at(threshold)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
                     f'stroke="#cf222e" stroke-width="1.4" stroke-dasharray="6 5"/>')
        parts.append(f'<text x="{width - pad_r}" y="{y - 7:.1f}" text-anchor="end" '
                     f'font-size="11.5" fill="#cf222e">告警阈值 ¥{threshold:.0f}</text>')

    # 折线 + 面积
    coords = [(x_at(i), y_at(float(p["price"]))) for i, p in enumerate(points)]
    if n > 1:
        line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
        area = (f"{coords[0][0]:.1f},{pad_t + inner_h:.1f} " + line +
                f" {coords[-1][0]:.1f},{pad_t + inner_h:.1f}")
        parts.append(f'<polygon points="{area}" fill="rgba(31,111,235,.10)"/>')
        parts.append(f'<polyline points="{line}" fill="none" stroke="#1f6feb" '
                     f'stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round"/>')

    # 数据点 + 数值标注
    for i, ((x, y), p) in enumerate(zip(coords, points)):
        color = "#1f6feb"
        if threshold and float(p["price"]) <= threshold:
            color = "#1a7f37"
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.2" fill="#fff" '
                     f'stroke="{color}" stroke-width="2.4"/>')
        if n <= 14 or i in (0, n - 1):
            parts.append(f'<text x="{x:.1f}" y="{y - 12:.1f}" text-anchor="middle" '
                         f'font-size="11.5" fill="#424a53">¥{float(p["price"]):.0f}</text>')

    # x 轴时间标签
    idxs = sorted({0, n - 1} | ({n // 2} if n >= 3 else set()))
    for i in idxs:
        label = str(points[i]["fetched_at"])[5:16]
        parts.append(f'<text x="{x_at(i):.1f}" y="{height - 14}" text-anchor="middle" '
                     f'font-size="11.5" fill="#656d76">{html.escape(label)}</text>')
    parts.append('</svg>')
    return "".join(parts)


# --------------------------------------------------------------------------
# 页面组装
# --------------------------------------------------------------------------
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

    latest = latest_round_rows(conn)
    if isinstance(latest, tuple):
        rows, last_fetch = latest
    else:
        rows, last_fetch = [], None

    history = target_history(conn, watch, date)
    positions = all_positions(conn, watch, date)

    # 识别被拼接截断的假低价（见 price_guard 注释）
    suspect_cut, is_suspect = price_guard(rows)

    # ---- 目标航班当前价（同轮内跨平台取低）----
    target_rows = [r for r in rows
                   if r.get("flight_no", "").upper().replace(" ", "") in watch]
    cur = min(target_rows, key=lambda r: r["price"]) if target_rows else None

    # ---- 统计 ----
    if history:
        hp = [float(h["price"]) for h in history]
        h_lo, h_hi, h_avg = min(hp), max(hp), sum(hp) / len(hp)
        first_price = hp[0]
        delta = (float(cur["price"]) - first_price) if cur else 0.0
    else:
        h_lo = h_hi = h_avg = first_price = delta = 0.0

    # ---- 陈旧告警：超过 3 倍抓取间隔没更新就提示 ----
    stale = ""
    if last_fetch:
        try:
            age_min = (datetime.now() - datetime.strptime(last_fetch, "%Y-%m-%d %H:%M:%S")).total_seconds() / 60
            interval = float((cfg.get("schedule") or {}).get("interval_minutes", 90) or 90)
            if age_min > interval * 3:
                stale = (f'<div class="banner">⚠️ 最近一次抓取是 {age_min:.0f} 分钟前'
                         f'（配置间隔 {interval:.0f} 分钟），定时任务可能已停止或抓取持续失败，'
                         f'请检查计划任务与 <code>logs/monitor.log</code>。</div>')
        except Exception:
            pass

    # ---- 卡片 ----
    if cur:
        dep_s, arr_s = _airport_of(cur)
        delta_cls = "down" if delta < 0 else ("up" if delta > 0 else "")
        delta_txt = f"较首次样本 {'+' if delta > 0 else ''}¥{delta:.0f}" if len(history) > 1 else "样本不足，暂无趋势"
        thr_txt = f"¥{threshold:.0f}" if threshold else "未设置"
        thr_note = ("已低于阈值，会触发推送" if threshold and cur["price"] <= threshold
                    else ("高于阈值，暂不推送" if threshold else "设置 alert_threshold 后生效"))
        cards = f"""
  <div class="card primary">
    <span class="label">目标航班当前最低价</span>
    <span class="value">¥{float(cur['price']):.0f}</span>
    <span class="delta {delta_cls}">{_esc(delta_txt)}</span>
  </div>
  <div class="card">
    <span class="label">航班 / 时刻</span>
    <span class="value" style="font-size:20px">{_esc(cur['flight_no'])}</span>
    <span class="note">{_esc(cur.get('depart_time') or '--:--')} → {_esc(cur.get('arrive_time') or '--:--')}
      {'· ' + _esc(cur.get('aircraft')) if cur.get('aircraft') else ''}</span>
  </div>
  <div class="card">
    <span class="label">机场</span>
    <span class="value" style="font-size:17px">{_esc(dep_s or from_name)}</span>
    <span class="note">→ {_esc(arr_s or to_name)}</span>
  </div>
  <div class="card">
    <span class="label">历史区间（{len(history)} 个样本）</span>
    <span class="value" style="font-size:20px">¥{h_lo:.0f} – ¥{h_hi:.0f}</span>
    <span class="note">均价 ¥{h_avg:.0f}</span>
  </div>
  <div class="card">
    <span class="label">告警阈值</span>
    <span class="value" style="font-size:20px">{_esc(thr_txt)}</span>
    <span class="note">{_esc(thr_note)}</span>
  </div>"""
    else:
        cards = ('<div class="card"><span class="label">目标航班</span>'
                 '<span class="value" style="font-size:18px">本轮未抓到</span>'
                 '<span class="note">见下方"运行状态"与日志</span></div>')

    # ---- 市场参考（排除疑似截断的假低价）----
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
                  f'注意 15:15 这班在平台上会以多个共享航班号重复列示，价格相同。</p>')

    # ---- 价目表：同轮内按航班号归并，取跨平台最低 ----
    by_flight: dict = {}
    for r in rows:
        key = r.get("flight_no") or "（全航线最低价）"
        by_flight.setdefault(key, []).append(r)
    table_rows = []
    for key, group in by_flight.items():
        best = min(group, key=lambda x: x["price"])
        platforms = " / ".join(f"{g['platform']}¥{float(g['price']):.0f}" for g in
                               sorted(group, key=lambda x: x["price"]))
        dep_s, arr_s = _airport_of(best)
        suspect = ""
        if best.get("depart_time") and best.get("arrive_time") \
                and best["depart_time"] > best["arrive_time"]:
            suspect = '<span class="badge warn">起飞时刻可疑</span>'
        is_target = key in watch
        tgt_badge = '<span class="badge ok">目标</span>' if is_target else ""
        if not best.get("flight_no"):
            suspect += '<span class="badge info">仅全航线最低价</span>'
        if is_suspect(best["price"]):
            suspect += '<span class="badge warn">价格可疑</span>'
        try:
            arr_id = (json.loads(best.get("extra") or "{}").get("arr_airport_id") or "").upper()
        except Exception:
            arr_id = ""
        table_rows.append((best["price"], f"""<tr class="{'target' if is_target else ''}" \
data-price="{float(best['price']):.0f}" data-dep="{_esc(best.get('depart_time') or '')}" \
data-ctu="{1 if arr_id == 'CTU' else 0}">
      <td class="mono">{_esc(key)}{tgt_badge}</td>
      <td>{_esc(best.get('airline'))}</td>
      <td>{_esc(best.get('aircraft'))}</td>
      <td class="mono">{_esc(best.get('depart_time') or '--:--')} → {_esc(best.get('arrive_time') or '--:--')}</td>
      <td>{_esc(dep_s)}</td>
      <td>{_esc(arr_s)}</td>
      <td class="mono"><b>¥{float(best['price']):.0f}</b></td>
      <td style="font-size:12.5px;color:#656d76">{_esc(platforms)}</td>
      <td>{suspect}</td>
    </tr>"""))
    table_rows.sort(key=lambda t: t[0])
    table_body = "\n".join(html_row for _, html_row in table_rows) or \
        '<tr><td colspan="9" class="empty">暂无数据</td></tr>'

    # ---- 目标航班抓取明细 ----
    detail = ""
    if positions:
        trs = []
        for p in positions[:40]:
            trs.append(f"""<tr>
      <td class="mono">{_esc(p['fetched_at'])}</td>
      <td>{_esc(p['platform'])}</td>
      <td class="mono">{_esc(p.get('depart_time') or '--:--')} → {_esc(p.get('arrive_time') or '--:--')}</td>
      <td class="mono">¥{float(p['price']):.0f}</td>
      <td>{_esc(p.get('airline'))}</td>
    </tr>""")
        detail = f"""
<section>
  <h2>目标航班抓取明细</h2>
  <p class="hint">每次抓到该航班号的原始记录（最新在前，最多 40 条）。</p>
  <div class="scroll"><table>
    <thead><tr><th>抓取时间</th><th>平台</th><th>时刻</th><th>价格</th><th>航司</th></tr></thead>
    <tbody>{''.join(trs)}</tbody>
  </table></div>
</section>"""

    refresh_tag = (f'<meta http-equiv="refresh" content="{refresh}">' if refresh > 0 else "")
    refresh_note = f"页面每 {refresh} 秒自动刷新" if refresh > 0 else "自动刷新已关闭"
    gen_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(' / '.join(watch) or '机票')} 机票监控报告</title>
{refresh_tag}
<style>{CARD_CSS}</style>
</head>
<body>
<header>
  <div class="wrap">
    <h1>✈️ {' / '.join(watch) if watch else '机票'} 价格监控</h1>
    <p class="sub">{_esc(line_label)} · {_esc(date)}</p>
    <p class="meta">生成时间 {_esc(gen_at)} · 最近抓取 {_esc(last_fetch or '—')} ·
      数据源 {' / '.join(cfg.get('platforms') or []) or '—'} · {_esc(refresh_note)}</p>
  </div>
</header>

<div class="wrap">
  {stale}
  <div class="cards">{cards}</div>

  <section>
    <h2>价格走势</h2>
    <p class="hint">目标航班在每次抓取中的最低价。样本越多趋势越有意义；绿点表示已低于阈值。</p>
    {svg_price_chart(history, threshold)}
  </section>

  <section>
    <h2>当日全部航班价目</h2>
    {market or '<p class="hint">同轮抓取到的全部航班，按价格升序。目标航班已高亮。</p>'}
    {QUERY_TOOLBAR}
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
      <div><span>监控航线</span>{_esc(line_label)}</div>
      <div><span>监控日期</span>{_esc(date)}</div>
      <div><span>目标航班</span>{_esc(' / '.join(watch) or '（未设置）')}</div>
      <div><span>起飞时刻窗</span>{_esc(route.get('depart_time_from') or '不限')} – {_esc(route.get('depart_time_to') or '不限')}</div>
      <div><span>告警阈值</span>{_esc(('¥%.0f' % threshold) if threshold else '未设置')}</div>
      <div><span>抓取间隔</span>{_esc((cfg.get('schedule') or {}).get('interval_minutes', '—'))} 分钟 ±
        {_esc((cfg.get('schedule') or {}).get('jitter_minutes', 0))} 分钟</div>
      <div><span>本轮记录数</span>{len(rows)} 条</div>
      <div><span>历史样本</span>{len(history)} 轮</div>
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
        latest.json   最近一轮全部航班（供外部程序/脚本查询）
        history.json  目标航班价格历史序列
        meta.json     摘要（当前价/区间/样本数/更新时间）
        _headers      Cloudflare Pages 的缓存与安全响应头
    """
    d = Path(deploy_dir)
    d.mkdir(parents=True, exist_ok=True)

    # 1) 报告页（复用已校验过的渲染逻辑）
    build_html(cfg, conn, str(d / "index.html"))

    out = cfg.get("output") or {}
    route = (cfg.get("routes") or [{}])[0]
    watch = [str(w).upper().replace(" ", "") for w in (route.get("watch_flights") or [])]
    date = (route.get("dates") or [""])[0]
    threshold = float(route.get("alert_threshold") or 0)

    latest = latest_round_rows(conn)
    rows, last_fetch = latest if isinstance(latest, tuple) else ([], None)
    history = target_history(conn, watch, date)

    # 同轮内按航班号归并，取跨平台最低
    by_flight: dict = {}
    for r in rows:
        by_flight.setdefault(r.get("flight_no") or "（全航线最低价）", []).append(r)

    flights = []
    for key, group in by_flight.items():
        best = min(group, key=lambda x: x["price"])
        item = _flight_row_json(best)
        item["flight_no"] = key if key != "（全航线最低价）" else ""
        item["platforms"] = [
            {"platform": g["platform"], "price": float(g["price"])}
            for g in sorted(group, key=lambda x: x["price"])
        ]
        flights.append(item)
    flights.sort(key=lambda x: x["price"])

    target_rows = [f for f in flights if f["flight_no"] in watch]
    target = min(target_rows, key=lambda x: x["price"]) if target_rows else None
    prices = [float(h["price"]) for h in history]

    # 当日最低价排除"疑似被拼接截断"的假低价（详见 price_guard）
    _, is_suspect = price_guard(rows)
    for f in flights:
        f["price_suspect"] = bool(is_suspect(f["price"]))
    sane_flights = [f for f in flights if not f["price_suspect"]]

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    latest_doc = {
        "generated_at": now,
        "last_fetch": last_fetch,
        "date": date,
        "route": {"from": route.get("from", ""), "to": route.get("to", ""),
                  "from_name": route.get("from_name", ""), "to_name": route.get("to_name", "")},
        "watch_flights": watch,
        "threshold": threshold,
        "target": target,
        "cheapest": (sane_flights[0] if sane_flights else (flights[0] if flights else None)),
        "flight_count": len(flights),
        "suspect_price_count": len(flights) - len(sane_flights),
        "flights": flights,
    }
    (d / "latest.json").write_text(json.dumps(latest_doc, ensure_ascii=False, indent=2),
                                   encoding="utf-8")

    history_doc = {
        "generated_at": now,
        "flight": watch[0] if watch else "",
        "watch_flights": watch,
        "date": date,
        "sample_count": len(history),
        "samples": [{"fetched_at": h["fetched_at"], "price": float(h["price"])} for h in history],
    }
    (d / "history.json").write_text(json.dumps(history_doc, ensure_ascii=False, indent=2),
                                    encoding="utf-8")

    meta = {
        "updated_at": now,
        "last_fetch": last_fetch,
        "flight": watch[0] if watch else "",
        "date": date,
        "current_price": target["price"] if target else None,
        "min_price": min(prices) if prices else None,
        "max_price": max(prices) if prices else None,
        "avg_price": round(sum(prices) / len(prices), 1) if prices else None,
        "sample_count": len(prices),
        "flight_count": len(flights),
        "threshold": threshold,
        "source": cfg.get("platforms") or [],
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
