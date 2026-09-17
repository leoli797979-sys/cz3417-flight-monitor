"""检查页面"数据源"状态是否渲染成徽标（而不是被转义的标签文本）。

用法: python scripts/check_source_badge.py deploy-ctucan/index.html
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

src = Path(sys.argv[1] if len(sys.argv) > 1 else "deploy-ctucan/index.html").resolve()

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1280, "height": 900})
    pg.goto("file:///" + src.as_posix())
    pg.wait_for_timeout(500)
    meta = pg.inner_text("p.meta")
    print("页头 meta 文本:")
    print("  " + meta.replace("\n", " | "))
    bad = "<span" in meta or "</span>" in meta
    print(f"是否出现未渲染的标签文本: {bad}")
    ok = pg.locator("p.meta .badge").count()
    print(f"页头徽标数量: {ok}")
    b.close()
    sys.exit(1 if bad or ok == 0 else 0)
