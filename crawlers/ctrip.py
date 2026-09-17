"""携程机票：m.ctrip.com H5 (taro) + 手机UA + XHR 拦截

数据源接口 flightListSearchForH5 返回 JSON。
每个航班(fltitem)的 policyinfo 内含多个销售政策，
每个政策有 tprice(含税价) 与 quantity(余票)。quantity 为 null/0 表示
无票(诱饵价)，必须剔除，否则会拿到买不到的超低价。
"""
import os
import re
import json
import urllib.parse
from typing import List

from core.models import FlightPrice
from core import flights as flights_mod
from .base import BaseCrawler


class CtripCrawler(BaseCrawler):
    name = "ctrip"
    use_mobile = True  # 移动端 H5

    # 携程 H5 机票单程列表（taro 版，用户提供的真实入口）
    URL_TPL = ("https://m.ctrip.com/html5/flight/taro/first?from=inner"
               "&tripType=ONE_WAY"
               "&dcity={from_city}&dcityName={from_name}"
               "&acity={to_city}&acityName={to_name}"
               "&ddate={date}")

    # 仅认航班列表接口；LowestPriceSearch 是跨日期低价日历，会混入其他日期价格
    XHR_KEYS = [
        "flightListSearchForH5",
    ]

    # 机票首页：先走一次"热身"建立 cookie 与访客标识，
    # 直接打列表页会被 Whale Guard 判为机器流量（实测接口返回纯文本 "whaleguard block"）
    HOME_URL = "https://m.ctrip.com/html5/flight/"

    CITY_NAME = {
        "SZX": "深圳", "KMG": "昆明", "PEK": "北京", "BJS": "北京",
        "SHA": "上海", "PVG": "上海", "CAN": "广州", "HGH": "杭州",
        "CTU": "成都", "SIA": "西安", "CKG": "重庆", "NKG": "南京",
        "WUH": "武汉", "CSX": "长沙", "XMN": "厦门", "SYX": "三亚",
        "HAK": "海口", "LJG": "丽江", "DLU": "大理", "JHG": "西双版纳",
        "DIG": "香格里拉", "TCZ": "腾冲",
    }

    def login_url(self) -> str:
        return "https://m.ctrip.com/webapp/passenger/login"

    @staticmethod
    def _detect_block(captured: list, page=None) -> str:
        """识别携程风控拦截。

        实测被拦时列表接口返回的正文就是纯文本 ``whaleguard block``，
        同时页面会渲染成验证码墙（HTML 里出现 captcha）。
        """
        for item in captured:
            text = (item.get("text") or "").strip()
            low = text[:400].lower()
            if "whaleguard" in low:
                return f"接口返回风控: {text[:60]}"
            if len(text) < 200 and "block" in low:
                return f"接口返回: {text[:60]}"
        try:
            html = (page.content() if page is not None else "") or ""
        except Exception:
            html = ""
        if "captcha" in html.lower():
            return "页面出现验证码墙(captcha)"
        return ""

    def fetch(self, from_city: str, to_city: str, dates: List[str]) -> List[FlightPrice]:
        results: List[FlightPrice] = []
        with self.browser() as ctx:
            page = self.new_page(ctx)
            # 热身：先访问机票首页，拿到访客 cookie 再去列表页
            try:
                self.logger.info("[ctrip] 预热访问 %s", self.HOME_URL)
                page.goto(self.HOME_URL, wait_until="domcontentloaded")
                page.wait_for_timeout(4000)
            except Exception as e:
                self.logger.warning("[ctrip] 预热失败(继续尝试): %s", e)

            for date in dates:
                url = self.URL_TPL.format(
                    from_city=from_city.upper(),
                    to_city=to_city.upper(),
                    from_name=urllib.parse.quote(self.CITY_NAME.get(from_city.upper(), from_city)),
                    to_name=urllib.parse.quote(self.CITY_NAME.get(to_city.upper(), to_city)),
                    date=date,
                )
                self.logger.info("[ctrip] GET %s", url)
                captured = self.attach_xhr_collector(page, self.XHR_KEYS)
                try:
                    page.goto(url, wait_until="domcontentloaded")
                    # 携程 H5 是 SPA，冷启动 + 风控校验比去哪儿更慢，窗口给足
                    page.wait_for_timeout(12000)
                    for _ in range(6):
                        try:
                            page.mouse.wheel(0, 2000)
                        except Exception:
                            pass
                        page.wait_for_timeout(1500)

                    flights = []
                    for item in captured:
                        flights.extend(self._extract_ctrip_flights(item.get("text", "")))
                    self._dump_xhr(captured, f"ctrip_xhr_{date}")

                    blocked = self._detect_block(captured, page)
                    if blocked:
                        self.logger.warning(
                            "[ctrip] %s 被风控拦截：%s —— 需要登录会话或更换网络出口。"
                            "可用 python main.py --login ctrip 登录一次以保存会话。",
                            date, blocked)
                        self._debug_snapshot(page, f"blocked_{date}")

                    if flights:
                        # 按航班号归并（同一航班多条政策 → 取最低有票价）
                        merged: dict = {}
                        for f in flights:
                            old = merged.get(f["flight_no"])
                            if old is None or f["price"] < old["price"]:
                                merged[f["flight_no"]] = f
                        for f in merged.values():
                            results.append(FlightPrice(
                                platform=self.name,
                                from_city=from_city, to_city=to_city,
                                depart_date=date, price=f["price"],
                                airline=f.get("airline", ""),
                                flight_no=f["flight_no"],
                                depart_time=f.get("depart_time", ""),
                                arrive_time=f.get("arrive_time", ""),
                            ))
                        self.logger.info(
                            "[ctrip] %s 解析到 %d 架航班，最低 ¥%.0f",
                            date, len(merged),
                            min(f["price"] for f in merged.values()))
                    else:
                        price = self._pick_lowest(captured)
                        if price is not None:
                            results.append(FlightPrice(
                                platform=self.name,
                                from_city=from_city, to_city=to_city,
                                depart_date=date, price=price,
                                route_level=True,
                            ))
                            self.logger.warning(
                                "[ctrip] %s 仅解析到全航线最低价 ¥%.0f"
                                "（无逐航班数据，指定航班号监控本轮不可用）", date, price)
                        else:
                            self.logger.warning("[ctrip] %s 未解析到价格", date)
                            self._debug_snapshot(page, f"nopx_{date}")
                except Exception as e:
                    self.logger.exception("[ctrip] %s 抓取异常: %s", date, e)
                self._sleep()
        return results

    def _pick_lowest(self, captured: list) -> float | None:
        prices: list = []
        for item in captured:
            prices.extend(self._extract_ctrip_prices(item.get("text", "")))
        prices = [p for p in prices if 100 <= p <= 50000]
        return float(min(prices)) if prices else None

    @staticmethod
    def _item_prices(item) -> list:
        """单个 fltitem 内所有"有票"政策的含税价。

        quantity 为 null/0 的政策是无票诱饵价，必须剔除。
        """
        prices: list = []

        def scan(node):
            if isinstance(node, dict):
                if "tprice" in node:
                    tp = node.get("tprice")
                    qty = node.get("quantity", None)
                    try:
                        tpv = int(float(tp))
                    except Exception:
                        tpv = None
                    has_ticket = qty is not None
                    if has_ticket:
                        try:
                            has_ticket = int(qty) > 0
                        except Exception:
                            has_ticket = True  # 非数字但非 null，视为有票
                    if tpv is not None and has_ticket:
                        prices.append(tpv)
                for v in node.values():
                    scan(v)
            elif isinstance(node, list):
                for v in node:
                    scan(v)

        scan(item)
        return prices

    @classmethod
    def _extract_ctrip_flights(cls, text: str) -> list:
        """抽取逐航班记录：flightListSearchForH5 的 fltitem 每项就是一架航班。"""
        if not text:
            return []
        try:
            obj = json.loads(text)
        except Exception:
            return []
        flts = obj.get("fltitem")
        if not isinstance(flts, list):
            return []

        records: list = []
        for item in flts:
            prices = cls._item_prices(item)
            if not prices:
                continue
            fno = flights_mod.find_flight_no(item)
            if not fno:
                continue
            dep, arr = flights_mod.find_times(item)
            records.append({
                "flight_no": fno,
                "depart_time": dep,
                "arrive_time": arr,
                "price": float(min(prices)),
                # 航司名以航班号前缀为准，避免页面标签串行
                "airline": flights_mod.carrier_name(fno)
                           or flights_mod.extract_airline(item),
            })
        return records

    @staticmethod
    def _extract_ctrip_prices(text: str) -> list:
        """解析携程 flightListSearchForH5 JSON，返回所有"有票"政策的含税价。

        逐航班递归遍历 policyinfo，配对 (tprice, quantity)，
        quantity 为 null/0 的政策剔除（无票诱饵价）。
        """
        if not text:
            return []
        try:
            obj = json.loads(text)
        except Exception:
            # 解析失败则退回正则（但仍要求 tprice 与 quantity 在同一对象，尽量配对）
            return CtripCrawler._regex_fallback(text)

        flts = obj.get("fltitem")
        if not isinstance(flts, list):
            return CtripCrawler._regex_fallback(text)

        prices: list = []
        for f in flts:
            prices.extend(CtripCrawler._item_prices(f))
        return prices

    @staticmethod
    def _regex_fallback(text: str) -> list:
        """JSON 解析失败时的兜底：仅取 tprice 紧跟 quantity 非 null 的。"""
        prices: list = []
        for m in re.finditer(
            r'"tprice"\s*:\s*(\d+(?:\.\d+)?)\s*,\s*"quantity"\s*:\s*(null|"?\d+"?)',
            text,
        ):
            qty = m.group(2)
            if qty == "null":
                continue
            try:
                if int(qty.strip('"')) <= 0:
                    continue
            except Exception:
                pass
            prices.append(int(float(m.group(1))))
        return prices

    def _dump_xhr(self, captured: list, tag: str):
        if not self.debug or not captured:
            return
        os.makedirs(self.debug_dir, exist_ok=True)
        path = os.path.join(self.debug_dir, tag + ".txt")
        try:
            with open(path, "w", encoding="utf-8") as f:
                for i, item in enumerate(captured):
                    f.write(f"\n----- [{i}] {item['url']} -----\n")
                    f.write((item.get("text") or "")[:200000])
                    f.write("\n")
            self.logger.info("[ctrip] 已保存 XHR: %s", path)
        except Exception:
            pass
