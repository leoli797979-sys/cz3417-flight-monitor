"""机票价格监控 主入口

用法:
    python main.py                # 按 config.yaml 配置启动定时监控
    python main.py --once         # 立即跑一次后退出
    python main.py -c other.yaml  # 指定配置文件
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

import yaml

from core.logger import setup_logger
from core.models import Route
from core.storage import PriceStorage
from core.alerter import Alerter
from core.scheduler import run_scheduler
from core.notifier import build_notifier
from crawlers import REGISTRY


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_routes(cfg: dict):
    routes = []
    for r in cfg.get("routes", []):
        # watch_flights: 只监控这些航班号（空 = 按全航线最低价，老行为）
        watch = r.get("watch_flights", r.get("flight_no", [])) or []
        if isinstance(watch, str):
            watch = [watch]
        watch = [str(x).strip().upper().replace(" ", "") for x in watch]
        dates = list(r.get("dates", []))

        # 环境变量覆盖：CI（GitHub Actions）里临时改监控目标不用改文件
        env_date = (os.environ.get("MONITOR_DATE") or "").strip()
        if env_date:
            dates = [env_date]
        env_flights = (os.environ.get("MONITOR_FLIGHTS") or "").strip()
        if env_flights:
            watch = [x.strip().upper().replace(" ", "")
                     for x in env_flights.split(",") if x.strip()]

        routes.append(Route(
            from_code=r["from"],
            from_name=r.get("from_name", r["from"]),
            to_code=r["to"],
            to_name=r.get("to_name", r["to"]),
            dates=dates,
            alert_threshold=float(r.get("alert_threshold", 0) or 0),
            watch_flights=[w for w in watch if w],
            depart_time_from=str(r.get("depart_time_from", "") or ""),
            depart_time_to=str(r.get("depart_time_to", "") or ""),
        ))
    return routes


def refresh_report_and_publish(cfg: dict, logger, storage: PriceStorage,
                               config_path: str) -> None:
    """用库里已有数据重刷 HTML 报告并发布到 Cloudflare。

    为什么单独抽成一个函数：轮次可能被看门狗强杀（实测过一次：携程页面崩溃后
    Playwright 关闭浏览器卡死，整轮挂住直到被系统终止，报告没刷新、快照也没推送，
    外部网页就一直停在旧数据）。那种情况下定时任务会再用
    ``main.py --report-only`` 调一次这里，把网页补上。
    """
    out_cfg = cfg.get("output") or {}
    report_html = out_cfg.get("report_html", "")
    pub_cfg = cfg.get("publish") or {}
    project_root = Path(__file__).resolve().parent

    if report_html:
        try:
            from report import build_report
            path = build_report(storage.db_path, report_html, cfg)
            logger.info("HTML 报告已刷新: %s", path)
        except Exception as e:
            logger.warning("刷新 HTML 报告失败: %s", e)

    # 发布到 Cloudflare Pages：让外部链接始终是最新快照（失败不影响抓取）
    if bool(pub_cfg.get("enabled")):
        try:
            proc = subprocess.run(
                [sys.executable, "publish.py", "-q", "-c", config_path],
                cwd=str(project_root),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", timeout=420)
            tail = [ln for ln in (proc.stdout or "").strip().splitlines() if ln.strip()]
            # publish.py 的结论行不带缩进，取它比取最后一行更清楚
            summary = next((ln for ln in reversed(tail) if not ln.startswith("  ")),
                           tail[-1] if tail else "")
            if proc.returncode == 0:
                logger.info("Cloudflare 发布: %s", summary)
            else:
                logger.warning("发布到 Cloudflare 失败(码 %s): %s", proc.returncode, summary)
        except Exception as e:
            logger.warning("发布到 Cloudflare 异常: %s", e)


def make_job(cfg: dict, logger, storage: PriceStorage, alerter: Alerter,
             config_path: str = "config.yaml"):
    crawler_cfg = cfg.get("crawler", {})
    platforms = cfg.get("platforms", ["ctrip", "fliggy", "tongcheng"])
    routes = build_routes(cfg)
    # 航向之间的间隔。为什么需要：去哪儿 touchInnerList 有全局限流（约 5 分钟/次），
    # 同一轮里隔几秒连发两次请求，第二次基本必被 1999 拦截。
    # 注意这里是「上一个航向抓完之后」再等，所以实际间隔恒 >= 该值，
    # 不会因为前一航向跑久了（浏览器兜底要 1~2 分钟）而被压缩。
    route_delay = int((cfg.get("schedule") or {}).get("route_delay_seconds", 0) or 0)
    # 每个平台各自的最小抓取间隔（分钟），见下面 job() 里的说明
    skip_cfg = (cfg.get("schedule") or {}).get("platform_skip_minutes") or {}

    crawlers = []
    for name in platforms:
        cls = REGISTRY.get(name)
        if cls is None:
            logger.warning("未知平台: %s，已跳过", name)
            continue
        crawlers.append(cls(crawler_cfg, logger))

    def job():
        logger.info("===== 开始一轮抓取 =====")
        for idx, route in enumerate(routes):
            if idx > 0 and route_delay > 0:
                logger.info("[间隔] 等待 %d 秒再抓下一个航向（规避去哪儿全局限流）", route_delay)
                time.sleep(route_delay)
            all_prices = []
            for c in crawlers:
                # 平台各自的最小间隔：携程整轮要开一次浏览器（~40 秒）且常被风控挡，
                # 逐航班价格又与去哪儿一致（实测两边完全吻合），所以不必每轮都跑 ——
                # 尤其在 10 分钟一轮的节奏下，跑它既拖慢轮次又更容易触发浏览器崩溃。
                skip_min = int((skip_cfg.get(c.name, 0) or 0))
                if skip_min > 0:
                    age = storage.last_batch_age_minutes(c.name)
                    if age < skip_min:
                        logger.info("[跳过] %s 距上次抓取仅 %.0f 分钟（阈值 %d 分钟），本轮不抓",
                                    c.name, age, skip_min)
                        continue
                storage.mark_attempt(c.name)
                prices = c.safe_fetch(route.from_code, route.to_code, route.dates)
                storage.save_many(prices)
                all_prices.extend(prices)
            alerter.check_and_alert(route, all_prices)
        logger.info("===== 本轮抓取结束 =====")

        # 清掉用不到的旧批次：库每轮都会推给云端，体积直接决定推送（也就是网页刷新）能开多快
        watch_all = [w for r in routes for w in (r.watch_flights or [])]
        try:
            removed = storage.prune(keep_batches=3, watch=watch_all)
            if removed:
                logger.info("[清理] 删除 %d 行非监控班次的旧记录（保留最近 3 批），库现为 %.2f MB",
                            removed, os.path.getsize(storage.db_path) / 1024 / 1024)
        except Exception as e:
            logger.warning("[清理] 失败(不影响抓取): %s", e)

        # 每轮结束后刷新 HTML 报告并发布（定时任务下外部链接自动保持最新）
        refresh_report_and_publish(cfg, logger, storage, config_path)

    return job


def main():
    ap = argparse.ArgumentParser(description="机票价格监控工具")
    ap.add_argument("-c", "--config", default="config.yaml", help="配置文件路径")
    ap.add_argument("--once", action="store_true", help="只运行一次后退出")
    ap.add_argument("--headless", action="store_true",
                    help="强制无头浏览器（无人值守定时任务用，不弹窗口）")
    ap.add_argument("--no-report", action="store_true", help="本轮结束后不刷新 HTML 报告")
    ap.add_argument("--report-only", action="store_true",
                    help="不抓取，直接用库里已有数据重刷报告并发布（轮次被看门狗强杀后补网页用）")
    ap.add_argument("--login", metavar="PLATFORM",
                    help="登录指定平台(ctrip/fliggy/tongcheng)，弹出可见浏览器，登录完成后回车保存会话")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"配置文件不存在: {cfg_path}", file=sys.stderr)
        sys.exit(1)
    cfg = load_config(str(cfg_path))
    if args.headless:
        cfg.setdefault("crawler", {})["headless"] = True
    if args.no_report:
        cfg.setdefault("output", {})["report_html"] = ""

    out = cfg.get("output", {})
    logger = setup_logger(out.get("log_path", "logs/monitor.log"))
    storage = PriceStorage(out.get("db_path", "data/prices.db"))
    notifier = build_notifier(cfg.get("notifier"), logger)
    if notifier:
        logger.info("已启用推送: %s", type(notifier).__name__)
    notify_cfg = cfg.get("notifier") or {}
    alerter = Alerter(
        logger,
        notifier=notifier,
        storage=storage,
        push_drop_min=float(notify_cfg.get("push_drop_min", 30)),
        push_rise_min=float(notify_cfg.get("push_rise_min", 50)),
    )

    # ---- 登录模式 ----
    if args.login:
        name = args.login.strip().lower()
        cls = REGISTRY.get(name)
        if cls is None:
            print(f"未知平台: {name}，可选: {list(REGISTRY)}", file=sys.stderr)
            sys.exit(2)
        crawler = cls(cfg.get("crawler", {}), logger)
        crawler.interactive_login()
        return

    # ---- 只补报告/发布（不抓取）----
    if args.report_only:
        logger.info("===== 仅重刷报告并发布（不抓取）=====")
        refresh_report_and_publish(cfg, logger, storage, args.config)
        return

    job = make_job(cfg, logger, storage, alerter, args.config)

    if args.once:
        job()
        return

    sched_cfg = cfg.get("schedule", {})
    interval = int(sched_cfg.get("interval_minutes", 30))
    jitter = int(sched_cfg.get("jitter_minutes", 0))
    run_on_start = bool(sched_cfg.get("run_on_start", True))
    logger.info("启动定时调度，每 %d 分钟一次 (±%d 分钟随机扰动, 首次立即运行=%s)",
                interval, jitter, run_on_start)
    try:
        run_scheduler(job, interval, run_on_start, jitter_minutes=jitter)
    except (KeyboardInterrupt, SystemExit):
        logger.info("收到退出信号，停止监控")


if __name__ == "__main__":
    main()
