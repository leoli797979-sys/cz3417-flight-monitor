"""低价提醒 + 微信推送（带去抖）

相比上游的关键改动：支持**按指定航班号**告警。

- ``route.watch_flights`` 为空 → 老行为：按 航线+日期 的全天最低价告警
- ``route.watch_flights`` 非空 → 只在**命中该航班号**的记录里取最低价告警，
  并可用 ``depart_time_from/to`` 再按起飞时刻卡一道（排除同号其它班次）。

防误报保护（这是改造的核心动机）：
    上游爬虫只抓"当天全航线最低价"，那条记录无法归属到某一架航班
    （标记为 ``route_level``）。在指定航班号模式下，一旦本轮没有任何记录
    命中目标航班，这里会明确打 WARNING 并且**不推送** ——
    否则用户会收到"当天最便宜那班（往往是清早/红眼）"的价，误以为是 CZ3417。
"""
import json
import logging
from typing import Dict, List, Optional, Tuple

from .flights import in_time_window, norm_flight_no
from .models import FlightPrice, Route


class Alerter:
    def __init__(self, logger: logging.Logger, notifier=None, storage=None,
                 push_drop_min: float = 30, push_rise_min: float = 50):
        """
        notifier:        推送器（None 表示不推送）
        storage:         用于读写 alert_state（去抖）
        push_drop_min:   触发"再次推送"的最小降幅(¥)
        push_rise_min:   触发"再次推送"的最小涨幅(¥)
        """
        self.logger = logger
        self.notifier = notifier
        self.storage = storage
        self.push_drop_min = push_drop_min
        self.push_rise_min = push_rise_min

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------
    def check_and_alert(self, route: Route, prices: List[FlightPrice]):
        if not prices:
            self.logger.warning("[告警] %s→%s 本轮无数据，跳过",
                                route.from_name, route.to_name)
            return

        self._log_table(route, prices)

        targets = self._select_targets(route, prices)
        if targets is None:
            # 指定航班号模式下没命中目标航班：已打 WARNING，明确不告警
            return

        self._compare_dates(route, prices)
        if route.alert_threshold and route.alert_threshold > 0:
            self._handle_threshold(route, prices, targets)

    # ------------------------------------------------------------------
    # 候选筛选：老行为 vs 指定航班号
    # ------------------------------------------------------------------
    def _select_targets(
        self, route: Route, prices: List[FlightPrice]
    ) -> Optional[Dict[Tuple[str, str], FlightPrice]]:
        """返回 {(日期, 航班号): 该组合下跨平台最低价记录}。

        返回 None 表示"本轮不应告警"（指定航班号但没命中，已打日志）。
        """
        watch = [norm_flight_no(f) or str(f).strip().upper()
                 for f in (route.watch_flights or [])]
        watch = [w for w in watch if w]

        if not watch:
            # 老行为：全天最低价，不区分航班号
            best: Dict[Tuple[str, str], FlightPrice] = {}
            for p in prices:
                key = (p.depart_date, norm_flight_no(p.flight_no))
                cur = best.get(key)
                if cur is None or p.price < cur.price:
                    best[key] = p
            return best

        matched: Dict[Tuple[str, str], FlightPrice] = {}
        route_level_seen = False
        for p in prices:
            fno = norm_flight_no(p.flight_no)
            if p.route_level or not fno:
                route_level_seen = True
                continue
            if fno not in watch:
                continue
            if not in_time_window(p.depart_time,
                                  route.depart_time_from, route.depart_time_to):
                self.logger.info(
                    "[告警] %s 命中航班号但起飞时刻 %s 不在窗口 [%s, %s]，忽略",
                    fno, p.depart_time or "未知",
                    route.depart_time_from or "-", route.depart_time_to or "-")
                continue
            key = (p.depart_date, fno)
            cur = matched.get(key)
            if cur is None or p.price < cur.price:
                matched[key] = p

        if matched:
            for (date, fno), best in sorted(matched.items()):
                self.logger.warning(
                    "[目标航班] %s %s %s→%s ¥%.0f（%s）",
                    fno, date,
                    best.depart_time or "--:--", best.arrive_time or "--:--",
                    best.price, best.platform)
            return matched

        # 没命中：明确说明原因，绝不拿全航线最低价顶替
        target_txt = "/".join(watch)
        if route_level_seen:
            self.logger.warning(
                "[告警] 本轮未抓到逐航班数据（爬虫只给出全航线最低价），"
                "目标航班 %s 无法确认 —— 本轮不告警。"
                "常见原因：页面改版 / 反爬拦截 / 该航线结果未含此航班。", target_txt)
        else:
            self.logger.warning(
                "[告警] 本轮结果里没有 %s（日期 %s）。可能未执飞、已售罄、"
                "或平台未返回该航班 —— 本轮不告警。",
                target_txt, ",".join(route.dates or []))
        return None

    # ------------------------------------------------------------------
    # 日志表格：列出当天所有航班的价格（便于人工核对）
    # ------------------------------------------------------------------
    def _log_table(self, route: Route, prices: List[FlightPrice]):
        by_date: Dict[str, List[FlightPrice]] = {}
        for p in prices:
            by_date.setdefault(p.depart_date, []).append(p)

        for date, rows in sorted(by_date.items()):
            self.logger.info("[价目] %s→%s %s 共 %d 条记录",
                             route.from_name, route.to_name, date, len(rows))
            # 同一航班跨平台横向对比
            grouped: Dict[str, List[FlightPrice]] = {}
            for p in rows:
                fno = norm_flight_no(p.flight_no)
                label = fno or ("【全航线最低价】" if p.route_level else "未知航班")
                grouped.setdefault(label, []).append(p)
            for label, group in sorted(
                grouped.items(),
                key=lambda kv: min(x.price for x in kv[1]),
            ):
                group = sorted(group, key=lambda x: x.price)
                head = group[0]
                detail = " | ".join(f"{x.platform}:¥{x.price:.0f}" for x in group)
                # 到达早于起飞＝起飞时刻大概率是被拼接内容串到的，标出来别误导人
                suspect = ""
                if head.depart_time and head.arrive_time and head.depart_time > head.arrive_time:
                    suspect = " [起飞时刻可疑]"
                self.logger.info(
                    "[价目]   %-10s %s→%s %-18s 最低 ¥%.0f  (%s)%s",
                    label,
                    head.depart_time or "--:--",
                    head.arrive_time or "--:--",
                    head.airline or "",
                    head.price, detail, suspect,
                )

    # ------------------------------------------------------------------
    # 多日期对比（沿用上游行为）
    # ------------------------------------------------------------------
    def _compare_dates(self, route: Route, prices: List[FlightPrice]):
        if len(route.dates) <= 1:
            return
        best_by_date: Dict[str, FlightPrice] = {}
        for p in prices:
            cur = best_by_date.get(p.depart_date)
            if cur is None or p.price < cur.price:
                best_by_date[p.depart_date] = p
        ordered = sorted(best_by_date.items(), key=lambda kv: kv[1].price)
        if not ordered:
            return
        cheapest_date, cheapest_p = ordered[0]
        self.logger.info(
            "[多日期] %s->%s 最便宜日期=%s ¥%.0f (%s)",
            route.from_name, route.to_name, cheapest_date,
            cheapest_p.price, cheapest_p.platform,
        )

    # ------------------------------------------------------------------
    # 阈值提醒 + 推送
    # ------------------------------------------------------------------
    def _handle_threshold(self, route: Route, prices: List[FlightPrice],
                          targets: Dict[Tuple[str, str], FlightPrice]):
        for (date, fno), bp in sorted(targets.items()):
            # 去抖状态带上航班号：不同航班互不干扰
            route_key = f"{route.from_code}-{route.to_code}-{date}-{fno or 'ANY'}"

            if bp.price > route.alert_threshold:
                # 涨出阈值：清除去抖状态，下次跌破按"首次"重新推
                if self.storage:
                    self.storage.clear_alert_state(route_key)
                self.logger.info(
                    "[阈值] %s %s ¥%.0f 高于阈值 ¥%.0f，不触发",
                    fno or "全航线最低", date, bp.price, route.alert_threshold)
                continue

            last = self.storage.get_alert_state(route_key) if self.storage else None
            should_push, reason = self._should_push(bp.price, last)

            self.logger.warning(
                "[低价] %s %s %s ¥%.0f (阈值¥%.0f, 上次推送¥%s) -> %s",
                fno or "全航线最低", date,
                f"{bp.depart_time or ''}".strip(),
                bp.price, route.alert_threshold,
                f"{last:.0f}" if last else "-",
                "推送" if should_push else f"跳过({reason})",
            )

            if should_push and self.notifier:
                ok = self._push(route, date, bp, last, prices)
                if ok and self.storage:
                    self.storage.set_alert_state(route_key, bp.price)

    def _should_push(self, cur: float, last: Optional[float]):
        if last is None:
            return True, "首次"
        diff = cur - last
        if diff <= -self.push_drop_min:
            return True, f"降¥{-diff:.0f}"
        if diff >= self.push_rise_min:
            return True, f"涨¥{diff:.0f}"
        return False, f"波动¥{diff:+.0f}(<阈值)"

    # ------------------------------------------------------------------
    # 推送内容
    # ------------------------------------------------------------------
    @staticmethod
    def _airports_of(p: FlightPrice) -> Tuple[str, str]:
        try:
            d = json.loads(p.extra or "{}")
        except Exception:
            return "", ""
        return str(d.get("dep_airport", "")), str(d.get("arr_airport", ""))

    def _cheapest_other(self, date: str, prices: List[FlightPrice],
                        exclude: FlightPrice) -> Optional[FlightPrice]:
        """当天除目标记录外的最低价（用于对比，判断是否值得换航班）。"""
        others = [p for p in prices
                  if p.depart_date == date and p is not exclude
                  and not (p.price == exclude.price
                           and p.platform == exclude.platform
                           and p.flight_no == exclude.flight_no)]
        return min(others, key=lambda x: x.price) if others else None

    def _push(self, route: Route, date: str, p: FlightPrice,
              last: Optional[float], prices: List[FlightPrice]) -> bool:
        diff_txt = ""
        emoji = "✈️"
        if last is not None:
            diff = p.price - last
            if diff <= 0:
                emoji = "📉"
                diff_txt = f"（较上次降 ¥{-diff:.0f}）"
            else:
                emoji = "📈"
                diff_txt = f"（较上次涨 ¥{diff:.0f}）"

        fno = norm_flight_no(p.flight_no) or "全航线最低"
        title = (f"{emoji} {fno} {route.from_name}→{route.to_name} {date} "
                 f"¥{p.price:.0f}{diff_txt}")

        dep_ap, arr_ap = self._airports_of(p)
        view_url = self._build_view_url(route, date, p.platform)
        desp = (
            f"## 机票低价提醒\n\n"
            f"- **航班**：{fno}"
            + (f"（{p.airline}）" if p.airline else "") + "\n"
            f"- **航线**：{route.from_name}（{route.from_code}） → "
            f"{route.to_name}（{route.to_code}）\n"
            f"- **日期**：{date}\n"
        )
        if p.depart_time or p.arrive_time:
            desp += (f"- **时刻**：{p.depart_time or '--:--'} → "
                     f"{p.arrive_time or '--:--'}\n")
        if dep_ap or arr_ap:
            desp += f"- **机场**：{dep_ap or '?'} → {arr_ap or '?'}\n"
        desp += (
            f"- **当前价**：**¥{p.price:.0f}**\n"
            f"- **设定阈值**：¥{route.alert_threshold:.0f}\n"
            f"- **来源**：{p.platform}\n"
            f"- **抓取时间**：{p.fetched_at}\n"
        )
        if last is not None:
            desp += f"- **上次推送价**：¥{last:.0f}\n"

        other = self._cheapest_other(date, prices, p)
        if other is not None and other.price < p.price:
            other_fno = norm_flight_no(other.flight_no) or "其它航班"
            desp += (
                f"\n> 当天其它最低：**{other_fno}** "
                f"{other.depart_time or ''} ¥{other.price:.0f}"
                f"（{other.platform}，差 ¥{p.price - other.price:.0f}）\n"
            )

        desp += f"\n[👉 在 {p.platform} 查看详情]({view_url})\n"
        return self.notifier.send(title, desp)

    def _build_view_url(self, route: Route, date: str, platform: str) -> str:
        if platform == "ctrip":
            return (
                "https://m.ctrip.com/html5/flight/taro/first?from=inner"
                "&tripType=ONE_WAY"
                f"&dcity={route.from_code}&acity={route.to_code}&ddate={date}"
            )
        if platform == "tongcheng":
            return (
                "https://m.ly.com/ft/touch/book1"
                f"?date={date}&an=1&cn=0&baby=0"
                f"&fromcitycode={route.from_code}&fromCode={route.from_code}"
                f"&tocitycode={route.to_code}&toCode={route.to_code}"
                "&cabin=0&platcode=518&frompage=HOME"
            )
        if platform == "qunar":
            return (
                "https://touch.qunar.com/ncs/page/flightlist"
                f"?depCity={route.from_name}&arrCity={route.to_name}"
                f"&goDate={date}&from=touch_index_search"
                "&child=0&baby=0&cabinType=0"
            )
        if platform == "tuniu":
            return (
                "https://m.tuniu.com/flight/domestic/new/"
                f"{route.from_code}_{route.to_code}_OW_1_0_0"
                f"?deptDate={date}&isGo=0"
            )
        # fliggy 默认走飞猪 H5
        return (
            "https://outfliggys.m.taobao.com/app/trip/rx-flight-eco/pages/listing"
            f"?depCityCode={route.from_code}&arrCityCode={route.to_code}"
            f"&leaveDate={date}&adultPassengerNum=1&searchType=1"
        )
