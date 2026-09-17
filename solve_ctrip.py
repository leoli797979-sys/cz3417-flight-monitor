"""交互式通过携程人机验证（Whale Guard 拼图），把信任会话存入 user_data/ctrip。

为什么需要这一步
----------------
携程的航班列表接口在有风控时会直接返回纯文本 ``whaleguard block``，页面弹出拼图验证
（"请完成以下验证：依次点击图标验证 / 滑动将展现拼图"）。这类验证靠改指纹过不去，
**必须真人过一次**；过一次后信任 cookie 落在持久化 profile 里，后续抓取可复用。

用法
----
    python solve_ctrip.py --from CTU --to CAN --date 2026-09-27 --wait 300

运行后会在桌面弹出浏览器窗口：请在其中完成拼图。脚本每 5 秒检测一次是否已拿到航班
数据，拿到即判定通过并保存会话；窗口会在 ``--wait`` 秒后自动关闭。
"""
from __future__ import annotations

import argparse
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.logger import setup_logger          # noqa: E402
from crawlers.ctrip import CtripCrawler       # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="交互式通过携程人机验证")
    ap.add_argument("--from", dest="fc", default="CTU")
    ap.add_argument("--to", dest="tc", default="CAN")
    ap.add_argument("--date", default="2026-09-27")
    ap.add_argument("--wait", type=int, default=300, help="等待真人操作的秒数")
    args = ap.parse_args()

    logger = setup_logger("logs/solve_ctrip.log")
    cfg = {
        "headless": False,          # 必须是可见窗口，否则没法滑动拼图
        "debug": True,
        "debug_dir": "debug",
        "user_data_dir": "user_data",
        "timeout_seconds": 60,
        "delay_min": 1,
        "delay_max": 2,
    }
    c = CtripCrawler(cfg, logger)
    url = c.URL_TPL.format(
        from_city=args.fc.upper(),
        to_city=args.tc.upper(),
        from_name=urllib.parse.quote(c.CITY_NAME.get(args.fc.upper(), args.fc)),
        to_name=urllib.parse.quote(c.CITY_NAME.get(args.tc.upper(), args.tc)),
        date=args.date,
    )

    ok = False
    with c.browser(headless=False) as ctx:
        page = c.new_page(ctx)
        captured = c.attach_xhr_collector(page, c.XHR_KEYS)
        logger.info("打开: %s", url)
        try:
            page.goto(url, wait_until="domcontentloaded")
        except Exception as e:
            logger.warning("页面打开异常(继续等待): %s", e)

        print()
        print("=" * 66)
        print("  请在刚弹出的浏览器窗口中完成人机验证（滑动拼图）。")
        print(f"  脚本会每 5 秒检测一次，最多等待 {args.wait} 秒；成功后自动保存会话。")
        print("=" * 66)

        t0 = time.time()
        elapsed = 0
        reloaded = set()
        while time.time() - t0 < args.wait:
            time.sleep(5)
            elapsed = int(time.time() - t0)
            flights = []
            for item in captured:
                flights.extend(c._extract_ctrip_flights(item.get("text", "")))
            if flights:
                ok = True
                logger.info("✅ 验证通过：已解析到 %d 架航班", len({f["flight_no"] for f in flights}))
                break
            # 若人已过验证但列表请求没有重发，刷新一次重新触发
            for mark in (60, 180):
                if elapsed >= mark and mark not in reloaded:
                    reloaded.add(mark)
                    logger.info("%d 秒仍无数据，刷新页面重新请求航班列表…", elapsed)
                    try:
                        page.reload(wait_until="domcontentloaded")
                    except Exception as e:
                        logger.warning("刷新失败: %s", e)
            if elapsed % 30 == 0:
                logger.info("已等待 %d 秒，仍在等待验证…", elapsed)

        # 留点时间让信任 cookie 落盘
        time.sleep(3)

    print()
    if ok:
        print("✅ 成功：携程信任会话已保存到 user_data/ctrip")
        print("   现在可以用 python probe_ctrip.py --from %s --to %s --date %s 验证无头模式能否复用。"
              % (args.fc, args.tc, args.date))
    else:
        print("❌ 未在时限内通过验证。可重试本脚本，或改用：")
        print("   python main.py --login ctrip    # 用携程账号登录（手机号+验证码）后再抓")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
