"""对生成的 HTML 报告做 DOM 级校验（替代人工看图），支持单班次与多班次。

检查：关键文案、卡片数值、走势图（多序列）数据点与坐标、
价目表高亮、班次对照表、**页面显示值与 JSON 接口值的一致性**、
布局溢出、页内查询 UI 的真实交互。

用法:
    python verify_report.py [report.html] [deploy_dir]
    python verify_report.py deploy-w5/index.html deploy-w5
"""
import json
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

src = Path(sys.argv[1] if len(sys.argv) > 1 else "report.html").resolve()
deploy_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else \
    (src.parent if (src.parent / "meta.json").exists() else Path("deploy"))
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  <- {extra}" if extra and not cond else ""))


# 先读部署包的 meta.json：监控班次清单决定单/多班次的断言口径
meta = {}
if (deploy_dir / "meta.json").exists():
    meta = json.loads((deploy_dir / "meta.json").read_text(encoding="utf-8"))
watch = [str(w).upper() for w in (meta.get("watch_flights") or [])]
n_watch = len(watch)
multi = n_watch > 1
expected = {f["flight_no"]: f for f in (meta.get("flights") or [])}
primary = watch[0] if watch else ""
flight_word = "监控班次" if multi else "目标航班"
print(f"报告: {src}")
print(f"部署包: {deploy_dir}   监控班次: {n_watch} 个 {watch}")

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 1000})
    page.goto("file:///" + src.as_posix())
    page.wait_for_timeout(700)
    text = page.inner_text("body")

    print("\n[1] 结构与关键文案")
    check("标题含监控班次号", primary and primary in page.title(), page.title())
    h1 = page.locator("h1").inner_text()
    check("主标题含『监控』", "监控" in h1, h1)
    for kw in ("价格走势", "当日全部航班价目", f"{flight_word}抓取明细", "运行状态"):
        check(f"区块存在：{kw}", kw in text)
    check("卡片：告警阈值", "告警阈值" in text)
    check("页脚免责声明", "仅供个人出行参考" in text)
    for fno in watch:
        check(f"页面上出现班次 {fno}", fno in text)

    print("\n[2] 走势图（内联 SVG，多序列）")
    svg_info = page.evaluate("""() => {
        const svg = document.querySelector('section svg');
        if (!svg) return null;
        const vb = svg.getAttribute('viewBox').split(/\\s+/).map(Number);
        const circles = [...svg.querySelectorAll('circle')].map(c => ({
            x: +c.getAttribute('cx'), y: +c.getAttribute('cy')
        }));
        const polys = [...svg.querySelectorAll('polyline')].map(p =>
            p.getAttribute('points').trim().split(/\\s+/).map(s => s.split(',').map(Number)));
        const texts = [...svg.querySelectorAll('text')].map(t => t.textContent);
        return {w: vb[2], h: vb[3], circles, polys, texts};
    }""")
    check("走势图 SVG 存在", svg_info is not None)
    if svg_info:
        inside = all(0 <= c["x"] <= svg_info["w"] and 0 <= c["y"] <= svg_info["h"]
                     for c in svg_info["circles"])
        check(f"数据点({len(svg_info['circles'])} 个)坐标都在画布内", inside)
        check("画布尺寸合理", svg_info["w"] > 800 and svg_info["h"] > 200,
              f"{svg_info['w']}x{svg_info['h']}")
        check("y 轴有金额刻度", any(t.strip().startswith("¥") for t in svg_info["texts"]))
        # 多班次：每个班次一条折线、图例齐全
        with_data = [f for f in watch if len((meta.get("flights") or []) and [1]) >= 0
                     and expected.get(f, {}).get("sample_count", 0) >= 2]
        check(f"折线数量等于有历史的班次数({len(with_data)})",
              len(svg_info["polys"]) <= max(1, len(with_data)),
              f"折线 {len(svg_info['polys'])} 条")
        legend = page.locator(".legend .lg").count()
        check(f"图例项数等于班次数({n_watch})", legend == n_watch, f"实际 {legend}")

    print("\n[3] 价目表与高亮")
    rows = page.locator("tbody tr").count()
    check("价目表行数 > 50", rows > 50, f"实际 {rows}")
    hl = page.locator("tr.target").count()
    check(f"高亮行数等于监控班次数({n_watch})", hl == n_watch, f"实际 {hl}")
    if hl:
        trow = page.locator("tr.target").first.inner_text()
        check("高亮行含监控班次号", any(f in trow for f in watch), trow[:80])
        nums = [int(x) for x in re.findall(r"[¥￥]\s*(\d{2,5})", trow)]
        check("高亮行价格落在合理区间", any(100 <= n <= 5000 for n in nums), trow[:80])

    if multi:
        print("\n[4] 监控班次对照表")
        cells = page.evaluate(
            "() => [...document.querySelectorAll('tr[data-flight]')]"
            ".map(r => ({f: r.dataset.flight, p: +r.dataset.price}))")
        check(f"对照表包含全部 {n_watch} 个班次", len(cells) == n_watch, str(cells))
        prices = [c["p"] for c in cells]
        check("对照表按当前价升序", prices == sorted(prices), str(prices))
        mism = [(c["f"], c["p"], expected[c["f"]].get("price"))
                for c in cells if c["f"] in expected
                and expected[c["f"]].get("price") is not None
                and abs(c["p"] - expected[c["f"]]["price"]) > 0.5]
        check("★ 页面价格与 meta.json 完全一致", not mism, str(mism))
        dep_bad = [(f, expected[f].get("depart_time")) for f in watch
                   if expected.get(f, {}).get("depart_time")
                   and expected[f].get("arrive_time")
                   and expected[f]["depart_time"] > expected[f]["arrive_time"]]
        check("★ 没有『到达早于起飞』的班次被展示", not dep_bad, str(dep_bad))
    else:
        print("\n[4] 单班次：不显示对照表")
        check("无对照表行（单班次外观不变）",
              page.locator("tr[data-flight]").count() == 0)

    print("\n[5] 布局健壮性")
    overflow = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
    check("无横向溢出", overflow <= 1, f"溢出 {overflow}px")
    bad = page.evaluate("""() => {
        const out = [];
        document.querySelectorAll('td, th, .card .value').forEach(el => {
            if (el.scrollWidth > el.clientWidth + 2) out.push(el.textContent.slice(0, 24));
        });
        return out.slice(0, 5);
    }""")
    check("单元格内容未被截断", not bad, str(bad))
    check("没有『暂无数据』占位", page.locator(".empty").count() == 0)

    print("\n[6] 自动刷新")
    refresh = page.get_attribute("meta[http-equiv='refresh']", "content")
    check("配置了自动刷新", refresh is not None and int(refresh) > 0, str(refresh))

    print("\n[7] 页内查询 UI（真实交互）")
    check("搜索框 #q 存在", page.locator("#q").count() == 1)
    check("排序 #sort 存在", page.locator("#sort").count() == 1)
    check("阈值过滤 #cheap 存在", page.locator("#cheap").count() == 1)
    check("目的地过滤 #dest 存在（按航向动态生成）", page.locator("#dest").count() == 1)
    if page.locator("#q").count() and primary:
        total = page.evaluate("() => document.querySelectorAll('#tbl tr').length")
        shown0 = page.evaluate("() => [...document.querySelectorAll('#tbl tr')]"
                               ".filter(r => r.style.display !== 'none').length")
        check("初始全部行可见", shown0 == total, f"{shown0}/{total}")
        check("计数文案已初始化", f"显示 {total} / {total}" in page.inner_text("#cnt"))

        page.fill("#q", primary)
        page.wait_for_timeout(250)
        shown = page.evaluate("() => [...document.querySelectorAll('#tbl tr')]"
                              ".filter(r => r.style.display !== 'none').length")
        check(f"搜索 {primary} 收敛结果", 0 < shown < total, f"{total} → {shown}")
        row_txt = page.evaluate("() => [...document.querySelectorAll('#tbl tr')]"
                                ".filter(r => r.style.display !== 'none')"
                                ".map(r => r.innerText).join(' | ')")
        check(f"结果含 {primary}", primary in row_txt, row_txt[:80])
        page.fill("#q", "")
        page.wait_for_timeout(200)

        page.select_option("#sort", "price-desc")
        page.wait_for_timeout(250)
        vals = page.evaluate("() => [...document.querySelectorAll('#tbl tr')]"
                             ".map(r => +r.dataset.price)")
        check("降序排序真正生效", vals[0] == max(vals) and vals[0] > vals[-1],
              f"首 {vals[0]} 末 {vals[-1]}")
        page.select_option("#sort", "price-asc")
        page.wait_for_timeout(250)
        vals2 = page.evaluate("() => [...document.querySelectorAll('#tbl tr')]"
                              ".map(r => +r.dataset.price)")
        check("升序排序真正生效", vals2[0] == min(vals2), f"首 {vals2[0]}")

        page.check("#cheap")
        page.wait_for_timeout(250)
        cheap_n = page.evaluate("() => [...document.querySelectorAll('#tbl tr')]"
                                ".filter(r => r.style.display !== 'none').length")
        ok_cheap = page.evaluate(
            "() => { const th = +document.getElementById('tbl').dataset.threshold;"
            " return [...document.querySelectorAll('#tbl tr')]"
            ".filter(r => r.style.display !== 'none').every(r => +r.dataset.price <= th); }")
        check("『只看低于阈值』生效且结果达标", 0 < cheap_n and ok_cheap,
              f"{total} → {cheap_n}, 达标={ok_cheap}")
        page.uncheck("#cheap")

        page.check("#dest")
        page.wait_for_timeout(250)
        ok_dest = page.evaluate("() => [...document.querySelectorAll('#tbl tr')]"
                                ".filter(r => r.style.display !== 'none')"
                                ".every(r => r.dataset.arr === '1')")
        dest_n = page.evaluate("() => [...document.querySelectorAll('#tbl tr')]"
                               ".filter(r => r.style.display !== 'none').length")
        check("『只看到达本页目的地』过滤正确且非空",
              ok_dest and 0 < dest_n <= total, f"剩余 {dest_n}/{total}")
        page.uncheck("#dest")

    browser.close()

print("\n[8] 查询用 JSON 接口（部署包）")
if deploy_dir.exists():
    docs = {}
    for name, keys in (("latest.json", ("flights", "targets", "target", "generated_at", "threshold")),
                       ("history.json", ("samples", "series", "sample_count")),
                       ("meta.json", ("current_price", "flights", "watch_flights"))):
        f = deploy_dir / name
        if not f.exists():
            check(f"{name} 存在", False, "文件缺失")
            continue
        try:
            docs[name] = json.loads(f.read_text(encoding="utf-8"))
            check(f"{name} 可解析且含关键字段",
                  all(k in docs[name] for k in keys), str(list(docs[name])[:10]))
        except Exception as e:
            check(f"{name} 可解析", False, str(e))

    if {"meta.json", "latest.json", "history.json"} <= set(docs):
        m, l, h = docs["meta.json"], docs["latest.json"], docs["history.json"]
        check(f"meta.flights 覆盖全部 {n_watch} 个班次",
              len(m.get("flights") or []) == n_watch, str(len(m.get("flights") or [])))
        check("latest.targets 覆盖全部班次", set(l.get("targets") or {}) == set(watch),
              str(sorted((l.get("targets") or {}).keys())))
        check("history.series 覆盖全部班次", set(h.get("series") or {}) == set(watch),
              str(sorted((h.get("series") or {}).keys())))
        bad = []
        for f in m.get("flights") or []:
            fno = f["flight_no"]
            tgt = (l.get("targets") or {}).get(fno) or {}
            if tgt.get("price") != f.get("price"):
                bad.append((fno, f.get("price"), tgt.get("price")))
        check("★ meta 与 latest 的各班次价格一致", not bad, str(bad))
        check("latest.flights 非空", len(l.get("flights") or []) > 50,
              str(len(l.get("flights") or [])))
        for fno, pts in (h.get("series") or {}).items():
            ts = [x["fetched_at"] for x in pts]
            if ts != sorted(ts):
                check(f"{fno} 历史时间升序", False, str(ts[:3]))
                break
        else:
            check("各班次历史时间均升序", True)
        check("history 样本数与 meta 一致",
              h.get("sample_count") == m.get("sample_count"),
              f"{h.get('sample_count')} vs {m.get('sample_count')}")
        check("向后兼容字段仍在（target / samples）",
              l.get("target") is not None and isinstance(h.get("samples"), list))
    check("Pages 响应头文件 _headers 存在", (deploy_dir / "_headers").exists())
else:
    print("  （未找到部署包目录，跳过）")

print("\n" + "=" * 58)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
for f in FAIL:
    print("  失败:", f)
sys.exit(1 if FAIL else 0)
