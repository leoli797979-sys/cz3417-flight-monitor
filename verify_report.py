"""对生成的 HTML 报告做 DOM 级校验（替代人工看图）。

检查：关键文案、卡片数值、走势图数据点数量与坐标是否越界、
价目表行数与目标航班高亮、是否出现横向溢出。

用法: python verify_report.py [report.html]
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

src = Path(sys.argv[1] if len(sys.argv) > 1 else "report.html").resolve()
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  <- {extra}" if extra and not cond else ""))


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 1000})
    page.goto("file:///" + src.as_posix())
    page.wait_for_timeout(600)
    text = page.inner_text("body")

    print("[1] 结构与关键文案")
    check("标题含航班号", "CZ3417" in page.title(), page.title())
    for kw in ("价格监控", "价格走势", "当日全部航班价目", "目标航班抓取明细", "运行状态"):
        check(f"区块存在：{kw}", kw in text)
    check("卡片：目标航班当前最低价", "目标航班当前最低价" in text)
    check("卡片：告警阈值", "告警阈值" in text)
    check("页脚免责声明", "仅供个人出行参考" in text)

    print("\n[2] 走势图（内联 SVG）")
    svg_info = page.evaluate("""() => {
        const svg = document.querySelector('section svg');
        if (!svg) return null;
        const vb = svg.getAttribute('viewBox').split(/\\s+/).map(Number);
        const circles = [...svg.querySelectorAll('circle')].map(c => ({
            x: +c.getAttribute('cx'), y: +c.getAttribute('cy'), r: +c.getAttribute('r')
        }));
        const poly = svg.querySelector('polyline');
        const pts = poly ? poly.getAttribute('points').trim().split(/\\s+/).map(s => s.split(',').map(Number)) : [];
        const texts = [...svg.querySelectorAll('text')].map(t => t.textContent);
        return {w: vb[2], h: vb[3], circles, pts, texts};
    }""")
    check("走势图 SVG 存在", svg_info is not None)
    if svg_info:
        inside = all(0 <= c["x"] <= svg_info["w"] and 0 <= c["y"] <= svg_info["h"]
                     for c in svg_info["circles"])
        check(f"数据点({len(svg_info['circles'])} 个)坐标都在画布内", inside)
        check("折线点数与数据点一致",
              len(svg_info["pts"]) == len(svg_info["circles"]),
              f"line={len(svg_info['pts'])} dots={len(svg_info['circles'])}")
        check("y 轴有金额刻度", any(t.strip().startswith("¥") for t in svg_info["texts"]))
        check("图上标注了价格数值",
              sum(1 for t in svg_info["texts"] if t.strip().startswith("¥")) >= 4,
              str(svg_info["texts"])[:120])

    print("\n[3] 价目表")
    rows = page.locator("tbody tr").count()
    check("价目表行数 > 50", rows > 50, f"实际 {rows}")
    check("目标航班恰有 1 行高亮", page.locator("tr.target").count() == 1,
          f"实际 {page.locator('tr.target').count()}")
    if page.locator("tr.target").count():
        trow = page.locator("tr.target").first.inner_text()
        check("目标行含 CZ3417", "CZ3417" in trow, trow[:80])
        check("目标行含时刻 15:15", "15:15" in trow, trow[:80])
        import re as _re
        nums = [int(x) for x in _re.findall(r"[¥￥]\s*(\d{2,5})", trow)]
        sane = [n for n in nums if 100 <= n <= 5000]
        check("目标行价格落在合理区间(¥100–¥5000)", len(sane) > 0, trow[:80])

    print("\n[4] 布局健壮性")
    overflow = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
    check("无横向溢出", overflow <= 1, f"溢出 {overflow}px")
    cells = page.evaluate("""() => {
        const bad = [];
        document.querySelectorAll('td, th, .card .value').forEach(el => {
            if (el.scrollWidth > el.clientWidth + 2) bad.push(el.textContent.slice(0, 24));
        });
        return bad.slice(0, 5);
    }""")
    check("单元格内容未被截断", not cells, str(cells))
    empty = page.locator(".empty").count()
    check("没有『暂无数据』占位", empty == 0, f"{empty} 处")

    print("\n[5] 自动刷新")
    refresh = page.get_attribute("meta[http-equiv='refresh']", "content")
    check("配置了自动刷新 meta", refresh is not None and int(refresh) > 0, str(refresh))
    check("刷新间隔为 300 秒", refresh == "300", str(refresh))

    print("\n[6] 页面内查询 UI（搜索 / 排序 / 过滤）")
    check("存在搜索框 #q", page.locator("#q").count() == 1)
    check("存在排序下拉 #sort", page.locator("#sort").count() == 1)
    check("存在阈值过滤 #cheap", page.locator("#cheap").count() == 1)
    check("存在双流过滤 #direct", page.locator("#direct").count() == 1)
    if page.locator("#q").count():
        # 只统计目标表格 #tbl —— 之前用 "tbody tr" 会连"抓取明细"表的行一起算进去，
        # 导致筛选类断言恒真、把真问题放过去了。
        total = page.evaluate("() => document.querySelectorAll('#tbl tr').length")
        shown0 = page.evaluate("() => [...document.querySelectorAll('#tbl tr')]"
                               ".filter(r => r.style.display !== 'none').length")
        check("初始全部行可见", shown0 == total, f"{shown0}/{total}")
        cnt0 = page.inner_text("#cnt")
        check("计数文案已初始化", f"显示 {total} / {total}" in cnt0, cnt0)

        page.fill("#q", "CZ3417")
        page.wait_for_timeout(250)
        shown = page.evaluate("() => [...document.querySelectorAll('#tbl tr')]"
                              ".filter(r => r.style.display !== 'none').length")
        check("搜索 CZ3417 收敛到 1 行", shown == 1, f"{total} → {shown}")
        check("计数文案随搜索更新", f"显示 1 / {total}" in page.inner_text("#cnt"),
              page.inner_text("#cnt"))
        row_txt = page.evaluate("() => [...document.querySelectorAll('#tbl tr')]"
                                ".filter(r => r.style.display !== 'none')"
                                ".map(r => r.innerText).join(' | ')")
        check("该行确实是目标航班（含 CZ3417 与 15:15）",
              "CZ3417" in row_txt and "15:15" in row_txt, row_txt[:90])

        page.fill("#q", "")
        page.wait_for_timeout(200)

        page.select_option("#sort", "price-desc")
        page.wait_for_timeout(250)
        vals = page.evaluate("() => [...document.querySelectorAll('#tbl tr')]"
                             ".map(r => +r.dataset.price)")
        check("降序排序真正生效（首=最大且 首>末）",
              vals[0] == max(vals) and vals[0] > vals[-1],
              f"首 {vals[0]} 末 {vals[-1]} 最大 {max(vals)}")

        page.select_option("#sort", "price-asc")
        page.wait_for_timeout(250)
        vals2 = page.evaluate("() => [...document.querySelectorAll('#tbl tr')]"
                              ".map(r => +r.dataset.price)")
        check("升序排序真正生效（首=最小）", vals2[0] == min(vals2),
              f"首 {vals2[0]} 最小 {min(vals2)}")

        page.select_option("#sort", "dep-asc")
        page.wait_for_timeout(250)
        deps = page.evaluate("() => [...document.querySelectorAll('#tbl tr')]"
                             ".map(r => r.dataset.dep).filter(Boolean)")
        check("按起飞时刻排序生效（升序）", deps == sorted(deps), str(deps[:5]))

        page.check("#cheap")
        page.wait_for_timeout(250)
        cheap_n = page.evaluate("() => [...document.querySelectorAll('#tbl tr')]"
                                ".filter(r => r.style.display !== 'none').length")
        prices_ok = page.evaluate(
            "() => { const th = +document.getElementById('tbl').dataset.threshold;"
            " return [...document.querySelectorAll('#tbl tr')]"
            ".filter(r => r.style.display !== 'none').every(r => +r.dataset.price <= th); }")
        check("『只看低于阈值』过滤生效且结果全部达标",
              0 < cheap_n < total and prices_ok, f"{total} → {cheap_n}, 全部达标={prices_ok}")
        page.uncheck("#cheap")

        page.check("#direct")
        page.wait_for_timeout(250)
        ctu_ok = page.evaluate("() => [...document.querySelectorAll('#tbl tr')]"
                               ".filter(r => r.style.display !== 'none')"
                               ".every(r => r.dataset.ctu === '1')")
        direct_n = page.evaluate("() => [...document.querySelectorAll('#tbl tr')]"
                                 ".filter(r => r.style.display !== 'none').length")
        check("『只看到达双流』过滤正确", ctu_ok and 0 < direct_n < total,
              f"剩余 {direct_n}")
        page.uncheck("#direct")

    browser.close()

print("\n[7] 查询用 JSON 接口（部署包）")
deploy_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("deploy")
if deploy_dir.exists():
    import json as _json
    docs = {}
    for name, keys in (("latest.json", ("flights", "target", "generated_at", "threshold")),
                       ("history.json", ("samples", "sample_count")),
                       ("meta.json", ("current_price", "min_price", "max_price", "sample_count"))):
        f = deploy_dir / name
        if not f.exists():
            check(f"{name} 存在", False, "文件缺失")
            continue
        try:
            docs[name] = _json.loads(f.read_text(encoding="utf-8"))
            check(f"{name} 可解析且含关键字段",
                  all(k in docs[name] for k in keys), str(list(docs[name])[:8]))
        except Exception as e:
            check(f"{name} 可解析", False, str(e))
    if "meta.json" in docs and "latest.json" in docs:
        m, l = docs["meta.json"], docs["latest.json"]
        check("meta.current_price 与 latest.target.price 一致",
              m["current_price"] == (l["target"] or {}).get("price"),
              f"{m.get('current_price')} vs {(l.get('target') or {}).get('price')}")
        check("latest.flights 非空", len(l["flights"]) > 50, str(len(l.get("flights", []))))
        tgt = [f for f in l["flights"] if f.get("flight_no") == (l.get("watch_flights") or [""])[0]]
        check("latest.flights 含目标航班 CZ3417", len(tgt) == 1, str(tgt[:1]))
        if tgt:
            check("目标航班含时刻/机场字段",
                  tgt[0]["depart_time"] == "15:15" and "CTU" in tgt[0]["arr_airport_id"],
                  str(tgt[0]))
    if "history.json" in docs:
        h = docs["history.json"]
        check("history 样本数与 meta 一致",
              h["sample_count"] == docs.get("meta.json", {}).get("sample_count"),
              f"{h['sample_count']} vs {docs.get('meta.json', {}).get('sample_count')}")
        check("history 样本时间升序",
              [s["fetched_at"] for s in h["samples"]] ==
              sorted(s["fetched_at"] for s in h["samples"]))
    check("Pages 响应头文件 _headers 存在", (deploy_dir / "_headers").exists())
else:
    print("  （未找到部署包目录，跳过；先运行 python publish.py --dry-run）")

print("\n" + "=" * 58)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
for f in FAIL:
    print("  失败:", f)
sys.exit(1 if FAIL else 0)
