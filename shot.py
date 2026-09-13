"""把生成的 HTML 报告渲染成 PNG，用于核对排版（开发/验证用）。

用法: python shot.py [report.html] [report_preview.png]
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

src = Path(sys.argv[1] if len(sys.argv) > 1 else "report.html").resolve()
dst = sys.argv[2] if len(sys.argv) > 2 else "report_preview.png"

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 1500})
    page.goto("file:///" + src.as_posix())
    page.wait_for_timeout(900)
    page.screenshot(path=dst, full_page=True)
    print("页面标题:", page.title())
    print("SVG 图表数:", page.locator("svg").count())
    print("价目表行数:", page.locator("tbody tr").count())
    print("目标航班高亮行:", page.locator("tr.target").count())
    print("页面高度(px):", page.evaluate("document.body.scrollHeight"))
    print("截图输出:", dst)
    browser.close()
