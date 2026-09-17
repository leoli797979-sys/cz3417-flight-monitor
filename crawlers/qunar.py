"""去哪儿机票：httpx 优先 + 浏览器兜底（混合模式）

针对去哪儿 touchInnerList 接口的 Ctrip ubtrms / chloroFp 风控：
- Bella token + caf7be/pre 等指纹头 + cookies 由浏览器跑风控 JS 生成，
  无法纯 httpx 离线生成（服务端校验指纹自洽性后下发 token）
- token 本身可复用（实测有效期 >5分钟，可能 24h）
- 但接口有全局频率限流（约5分钟/次），与航线无关，httpx 连续请求必 1999

混合策略（省约50%浏览器开销）：
1. 每轮第一条航线优先 httpx（用缓存的 token）
2. httpx 成功 → 直接返回；后续航线也尝试 httpx，失败则回退浏览器
3. httpx 1999/无token/异常 → 浏览器单条查（拦请求刷新 token + 解析响应拿价）
4. 浏览器每次跑都会刷新 token，供下次 httpx 使用

实测：浏览器握手后立即 httpx 可拿到真实价格（minPrice 与 DOM 一致）；
但 httpx 第二次（5分钟内）必被风控，故多航线场景第二条起回退浏览器。
"""
import os
import re
import json
import time
import urllib.parse
from typing import List, Optional

import httpx

from core.models import FlightPrice
from core import flights as flights_mod
from .base import BaseCrawler


class QunarCrawler(BaseCrawler):
    name = "qunar"
    use_mobile = True

    # 去哪儿单程列表 H5（用户提供的真实入口）
    URL_TPL = (
        "https://touch.qunar.com/ncs/page/flightlist"
        "?depCity={from_name}&arrCity={to_name}"
        "&goDate={date}&from=touch_index_search"
        "&child=0&baby=0&cabinType=0"
    )

    CITY_NAME = {
        "SZX": "深圳", "KMG": "昆明", "PEK": "北京", "BJS": "北京",
        "SHA": "上海", "PVG": "上海", "CAN": "广州", "HGH": "杭州",
        "CTU": "成都", "SIA": "西安", "CKG": "重庆", "NKG": "南京",
        "WUH": "武汉", "CSX": "长沙", "XMN": "厦门", "SYX": "三亚",
        "HAK": "海口", "LJG": "丽江", "DLU": "大理", "JHG": "西双版纳",
        "DIG": "香格里拉", "TCZ": "腾冲",
    }

    # 反自动化指纹脚本：让设备指纹与 iPhone UA 自洽，骗过 chloroFp 服务端校验
    # 关键：Ctrip 风控发现「iPhone UA + Win32/NVIDIA WebGL」矛盾会拒发指纹 token
    _STEALTH_JS = r"""
    (function(){
      const define = (obj, prop, val) => {
        try { Object.defineProperty(obj, prop, {get: () => val, configurable: true}); } catch(e){}
      };
      // 基础导航器：对齐 iPhone Safari
      define(navigator, 'webdriver', undefined);
      define(navigator, 'languages', ['zh-CN','zh']);
      define(navigator, 'platform', 'iPhone');
      define(navigator, 'maxTouchPoints', 5);
      define(navigator, 'hardwareConcurrency', 6);
      define(navigator, 'vendor', 'Apple Computer, Inc.');
      try { delete navigator.deviceMemory; } catch(e){}
      // plugins 真机 Safari 为空
      define(navigator, 'plugins', []);
      window.chrome = undefined;

      // WebGL：把 NVIDIA/ANGLE 伪装成 Apple GPU
      const APPLE_VENDOR = 'Apple Inc.';
      const APPLE_RENDERER = 'Apple GPU';
      const patchGL = (proto) => {
        if (!proto || !proto.getParameter) return;
        const orig = proto.getParameter;
        proto.getParameter = function(p){
          // UNMASKED_VENDOR_WEBGL=37445, UNMASKED_RENDERER_WEBGL=37446
          if (p === 37445) return APPLE_VENDOR;
          if (p === 37446) return APPLE_RENDERER;
          // VENDOR=7936, RENDERER=7937
          if (p === 7936) return 'WebKit';
          if (p === 7937) return 'WebKit WebGL';
          return orig.call(this, p);
        };
      };
      try { patchGL(WebGLRenderingContext.prototype); } catch(e){}
      try { patchGL(WebGL2RenderingContext.prototype); } catch(e){}

      // 触摸事件支持标记
      try {
        define(window, 'ontouchstart', null);
      } catch(e){}
    })();
    """

    # 去哪儿单程列表真实数据接口
    API_URL = "https://touch.qunar.com/flight/api/touchInnerList"

    # 风控占位响应特征
    RISK_CODE = 1999
    # token 有效期（保守取 20h，实际约 24h）
    TOKEN_TTL_S = 20 * 3600

    def login_url(self) -> str:
        # 触屏版登录入口，登录后 cookie 作用于 touch.qunar.com，与抓取同源
        return "https://user.qunar.com/mobile/login.jsp"

    # 登录墙冷却：去哪儿偶尔会把页面跳到登录墙（实测 2026-09-17 出现 3 次，
    # 都在 ~50 分钟内自行恢复）。撞墙后如果还按 10 分钟一轮的节奏反复跑浏览器握手，
    # 只会把风控喂得更狠，所以撞墙就歇一段时间。冷却期间 httpx 仍然照常尝试 ——
    # 登录墙挡的是页面导航，拿着有效令牌打接口往往还能拿到数据。
    WALL_COOLDOWN_MIN = 30

    @property
    def _wall_path(self) -> str:
        return os.path.join(self.user_data_root, self.name, "wall_until.json")

    def _wall_cooldown_left(self) -> float:
        """距离登录墙冷却结束还有多少秒（0 = 不在冷却中）。"""
        try:
            with open(self._wall_path, "r", encoding="utf-8") as f:
                until = float((json.load(f) or {}).get("until", 0) or 0)
        except Exception:
            return 0.0
        return max(0.0, until - time.time())

    def _start_wall_cooldown(self, minutes: int = 0) -> None:
        minutes = int(minutes or self.WALL_COOLDOWN_MIN)
        os.makedirs(os.path.dirname(self._wall_path), exist_ok=True)
        try:
            with open(self._wall_path, "w", encoding="utf-8") as f:
                json.dump({"until": time.time() + minutes * 60,
                           "since": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
        except Exception:
            pass

    # ==================== 主流程 ====================
    def fetch(self, from_city: str, to_city: str, dates: List[str]) -> List[FlightPrice]:
        """逐航班抓取：优先解析出每架航班的价（可据此监控指定航班号）。

        解析降级顺序：
        1) 逐航班记录（能拿到 flight_no / 起降时刻）→ 每航班一条 FlightPrice
        2) 只有顶层 minPrice（无航班节点）→ 一条 route_level 记录，
           表示"全航线当天最低价"，alerter 在指定航班号模式下不会据此告警
        """
        results: List[FlightPrice] = []
        for date in dates:
            try:
                text = self._fetch_one(from_city, to_city, date)
            except Exception as e:
                self.logger.exception("[qunar] %s 抓取异常: %s", date, e)
                text = None

            if not text:
                self.logger.warning("[qunar] %s 未取到响应", date)
                self._sleep()
                continue

            flights = self._extract_qunar_flights(text)
            if flights:
                for f in flights:
                    results.append(FlightPrice(
                        platform=self.name,
                        from_city=from_city, to_city=to_city,
                        depart_date=date, price=f["price"],
                        airline=f.get("airline", ""),
                        flight_no=f.get("flight_no", ""),
                        depart_time=f.get("depart_time", ""),
                        arrive_time=f.get("arrive_time", ""),
                        extra=json.dumps(
                            {k: f.get(k, "") for k in (
                                "dep_airport", "arr_airport",
                                "dep_airport_id", "arr_airport_id",
                                "dep_terminal", "arr_terminal", "aircraft")},
                            ensure_ascii=False),
                    ))
                self.logger.info(
                    "[qunar] %s 解析到 %d 架航班，最低 ¥%.0f",
                    date, len(flights), min(f["price"] for f in flights))
            else:
                prices = self._extract_qunar_prices(text)
                if prices:
                    results.append(FlightPrice(
                        platform=self.name,
                        from_city=from_city, to_city=to_city,
                        depart_date=date, price=float(min(prices)),
                        route_level=True,
                    ))
                    self.logger.warning(
                        "[qunar] %s 仅解析到全航线最低价 ¥%.0f（无逐航班数据，"
                        "指定航班号监控本轮不可用）", date, min(prices))
                else:
                    self.logger.warning("[qunar] %s 未解析到价格", date)
            self._sleep()
        return results

    def _fetch_one(self, from_city: str, to_city: str, date: str) -> Optional[str]:
        """单日期抓取：优先 httpx，失败回退浏览器。返回原始响应文本。"""
        # 1) 优先 httpx（用缓存 token，省浏览器开销）
        text = self._fetch_via_httpx(from_city, to_city, date)
        if text:
            self.logger.info("[qunar] httpx 命中，省去浏览器")
            return text
        # 2) httpx 失败（限流/无token/异常）→ 浏览器单条查
        self.logger.info("[qunar] httpx 未命中，回退浏览器")
        return self._fetch_via_browser(from_city, to_city, date)

    # ==================== httpx 续航 ====================
    def _fetch_via_httpx(self, from_city: str, to_city: str, date: str) -> Optional[str]:
        """用缓存 token 直接 httpx POST，返回原始响应文本或 None。"""
        token = self._load_token()
        if not token:
            return None

        # 主动换新令牌：实测（61 轮日志统计）直连成功率与令牌年龄强相关 ——
        # 30~90 分钟最高（95%），超过 1.5 小时骤降到 20%，超过 5 小时 0%。
        # 所以宁可提前用浏览器握手换一个新令牌，也不要拿快过期的令牌去撞 1999
        # （撞了之后同轮的兜底还只有 22% 能救回来）。
        max_age_min = int(self.config.get("token_max_age_minutes", 60) or 0)
        if max_age_min > 0:
            age_s = time.time() - float(token.get("updated_at", 0) or 0)
            if age_s > max_age_min * 60:
                self.logger.info(
                    "[qunar] 缓存 token 已用 %.0f 分钟（阈值 %d 分钟），主动走浏览器握手换新",
                    age_s / 60, max_age_min)
                return None

        fc, tc = from_city.upper(), to_city.upper()
        from_name = self.CITY_NAME.get(fc, from_city)
        to_name = self.CITY_NAME.get(tc, to_city)

        # 基于模板构造新 body：替换城市/日期/时间戳，保留 Bella
        body = dict(token["body_template"])
        body["depCity"] = from_name
        body["arrCity"] = to_name
        body["goDate"] = date
        body["firstRequest"] = True
        body["startNum"] = 0
        body["sort"] = 5
        body["_v"] = 2
        body["underageOption"] = ""
        now_ms = int(time.time() * 1000)
        body["r"] = now_ms
        body["st"] = now_ms - 1

        headers = dict(token["headers"])
        headers["referer"] = "https://touch.qunar.com/ncs/page/flightlist"
        cookies = dict(token["cookies"])

        try:
            r = httpx.post(
                self.API_URL,
                headers=headers,
                content=json.dumps(body, ensure_ascii=False, separators=(",", ":")),
                cookies=cookies,
                timeout=25,
            )
        except Exception as e:
            self.logger.warning("[qunar] httpx 请求异常: %s", e)
            return None

        if r.status_code != 200:
            self.logger.warning("[qunar] httpx status=%d", r.status_code)
            return None

        text = r.text
        self._dump_raw_text(text, f"httpx_{from_name}_{to_name}_{date}")

        if self._is_risk_text(text):
            self.logger.warning("[qunar] httpx 命中风控(1999)，可能是限流或token过期")
            return None

        # 只判断"有没有可用数据"，具体定价交给 fetch() 的分层解析
        if not self._extract_qunar_flights(text) and not self._extract_qunar_prices(text):
            self.logger.warning("[qunar] httpx 响应无价格，len=%d", len(text))
            return None
        return text

    @staticmethod
    def _is_risk_text(text: str) -> bool:
        try:
            obj = json.loads(text)
        except Exception:
            return False
        bstatus = obj.get("bstatus") or {}
        if bstatus.get("code") == QunarCrawler.RISK_CODE:
            return True
        if obj.get("ret") is False and obj.get("data") is None:
            return True
        return False

    # ==================== 浏览器单条查（兜底 + 刷新 token） ====================
    def _fetch_via_browser(self, from_city: str, to_city: str, date: str) -> Optional[str]:
        """浏览器跑一次页面：拦请求刷新 token + 带回原始响应文本。

        一次浏览器调用同时完成三件事：
        1. 拦截 touchInnerList 请求，存 token（headers+cookies+body模板含 Bella）
        2. 拦截响应，保存响应文本
        3. 返回响应文本（价格解析交给 fetch()）
        """
        from_name = self.CITY_NAME.get(from_city.upper(), from_city)
        to_name = self.CITY_NAME.get(to_city.upper(), to_city)

        left = self._wall_cooldown_left()
        if left > 0:
            self.logger.warning(
                "[qunar] 处于登录墙冷却中（还剩 %.0f 分钟），本轮跳过浏览器握手；"
                "若长时间不恢复，需要人工登录一次：python main.py --login qunar",
                left / 60)
            return None

        url = self.URL_TPL.format(
            from_name=urllib.parse.quote(from_name),
            to_name=urllib.parse.quote(to_name),
            date=date,
        )
        self.logger.info("[qunar] 浏览器 GET %s", url)

        snap = {
            "headers": None, "body_template": None,
            "cookies": None, "response_text": None,
        }

        with self.browser() as ctx:
            try:
                ctx.add_init_script(self._STEALTH_JS)
            except Exception:
                pass
            page = self.new_page(ctx)

            def on_request(req):
                if "touchInnerList" in req.url and snap["headers"] is None:
                    snap["headers"] = dict(req.headers)
                    try:
                        snap["body_template"] = json.loads(req.post_data or "{}")
                    except Exception:
                        snap["body_template"] = {}
                    self.logger.info("[qunar] 拦截请求，Bella长度=%d",
                                    len(snap["body_template"].get("Bella", "")))

            def on_response(resp):
                if "touchInnerList" in resp.url and snap["response_text"] is None:
                    try:
                        snap["response_text"] = resp.text()
                    except Exception:
                        pass

            page.on("request", on_request)
            page.on("response", on_response)

            wait_s = int(self.config.get("response_wait_seconds", 60) or 60)
            try:
                page.goto(url, wait_until="load")
                # 轮询等待响应。注意：CI 冷启动（无 GPU、冷缓存）明显比本机慢，
                # 实测 GitHub Actions 上 24 秒的窗口拿不到响应，所以窗口可配且默认放大。
                for _ in range(max(1, wait_s // 2)):
                    page.wait_for_timeout(2000)
                    try:
                        page.mouse.wheel(0, 1500)
                    except Exception:
                        pass
                    if snap["response_text"] is not None:
                        break
                if snap["response_text"] is None:
                    try:
                        self.logger.warning(
                            "[qunar] %d 秒内未拦到 touchInnerList 响应（当前页 %s / 标题 %s）",
                            wait_s, page.url, page.title())
                    except Exception:
                        pass
                    self._debug_snapshot(page, f"noresponse_{date}")
            except Exception as e:
                self.logger.warning("[qunar] 浏览器页面异常: %s", e)

            # 导出 cookies
            try:
                ck = ctx.cookies()
                snap["cookies"] = {c["name"]: c["value"] for c in ck}
            except Exception:
                snap["cookies"] = {}
            try:
                snap["page_url"] = page.url
            except Exception:
                snap["page_url"] = ""

        # 存 token（供下次 httpx）。**只有真的拿到 touchInnerList 响应时才存。**
        # 踩过的坑（2026-09-17 23:45）：页面被跳到登录页 user.qunar.com/mobile/login.jsp 时，
        # 请求拦截器照样能抓到一个 Bella，但那是登录页的令牌 —— 存下来会毒化缓存，
        # 之后每轮 httpx 必然 1999，而浏览器回退又被登录墙挡住，从此再也拿不到真令牌。
        on_login_wall = "login.jsp" in (snap.get("page_url") or "")
        if snap["response_text"] and not on_login_wall:
            if snap["headers"] and snap["body_template"] and snap["body_template"].get("Bella"):
                self._save_token(snap)
                self.logger.info("[qunar] token 已刷新，有效期 %dh", self.TOKEN_TTL_S // 3600)
        elif on_login_wall:
            self._start_wall_cooldown(int(self.config.get("wall_cooldown_minutes", 0) or 0))
            self.logger.warning(
                "[qunar] 被跳到登录页，本次不写入 token（避免毒化缓存），"
                "并进入 %d 分钟冷却；若长时间不恢复需要人工登录一次：python main.py --login qunar",
                int(self.config.get("wall_cooldown_minutes", 0) or self.WALL_COOLDOWN_MIN))
        else:
            self.logger.warning("[qunar] 未拿到列表响应，本次不写入 token")

        # 解析交给 fetch() 的分层解析；这里只负责带回原始响应
        if snap["response_text"]:
            self._dump_raw_text(snap["response_text"], f"browser_{from_name}_{to_name}_{date}")
            return snap["response_text"]
        return None

    # ==================== token 持久化 ====================
    @property
    def _token_path(self) -> str:
        return os.path.join(self.user_data_root, self.name, "token.json")

    def _load_token(self) -> Optional[dict]:
        try:
            with open(self._token_path, "r", encoding="utf-8") as f:
                obj = json.load(f)
        except Exception:
            return None
        updated_at = obj.get("updated_at", 0)
        if time.time() - updated_at > self.TOKEN_TTL_S:
            return None
        return obj

    def _save_token(self, snap: dict):
        os.makedirs(os.path.dirname(self._token_path), exist_ok=True)
        data = {
            "headers": snap["headers"],
            "body_template": snap["body_template"],
            "cookies": snap["cookies"],
            "updated_at": time.time(),
        }
        try:
            with open(self._token_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.logger.warning("[qunar] 保存 token 失败: %s", e)

    def _dump_raw_text(self, text: str, tag: str):
        """保存原始响应文本到 debug 目录，用于排查价格异常。"""
        if not self.debug or not text:
            return
        os.makedirs(self.debug_dir, exist_ok=True)
        safe_tag = re.sub(r'[^\w]', '_', tag)
        path = os.path.join(self.debug_dir, f"qunar_raw_{safe_tag}.txt")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            self.logger.info("[qunar] 已保存响应: %s (len=%d)", path, len(text))
        except Exception:
            pass

    # ==================== 价格解析 ====================
    # 去哪儿 data 片段的字段名（2026-09 实测）
    _RE_BINFO = re.compile(r'"binfo"')
    _RE_AIRCODE_ARR = re.compile(r'"airCode"\s*:\s*\[([^\]]{0,300})\]')
    _RE_QUOTED = re.compile(r'"([^"]{2,12})"')
    _RE_ANY_CODE = re.compile(r'"code"\s*:\s*"')
    _RE_MINPRICE = re.compile(r'"minPrice"\s*:\s*"?(\d{2,6})')
    _RE_DEPTIME = re.compile(r'"depTime"\s*:\s*"([0-2]?\d:[0-5]\d)"')
    _RE_ARRTIME = re.compile(r'"arrTime"\s*:\s*"([0-2]?\d:[0-5]\d)"')
    _RE_DEPAP = re.compile(r'"depAirport"\s*:\s*"([^"]{1,20})"')
    _RE_ARRAP = re.compile(r'"arrAirport"\s*:\s*"([^"]{1,20})"')
    _RE_DEPAPID = re.compile(r'"depAirportId"\s*:\s*"([A-Z]{3})"')
    _RE_ARRAPID = re.compile(r'"arrAirportId"\s*:\s*"([A-Z]{3})"')
    _RE_DEPTERM = re.compile(r'"depTerminal"\s*:\s*"([^"]{1,10})"')
    _RE_ARRTERM = re.compile(r'"arrTerminal"\s*:\s*"([^"]{1,10})"')
    _RE_NAME = re.compile(r'"name"\s*:\s*\[\s*"([^"]{1,40})"(?:\s*,\s*"([^"]{1,40})")?')

    @classmethod
    def _parse_flight_objects(cls, text: str) -> list:
        """从去哪儿"被截断 + 转义 + 拼接混淆"的 data 片段里按航班行抽记录。

        实测响应形态（2026-09）::

            {"ret":"true","msg":"查询成功!","code":"0",
             "data":"<被截断/交错的 JSON 片段>","t1000":"<混淆 JS>"}

        三个坑，都是实测踩出来的：

        1. ``data`` **不是合法 JSON**：头部被挪进 ``t1000`` 的混淆 JS
           （``JSON.parse(join('')+...)``）里重建，整串 json.loads 必然失败。
        2. 同一物理航班会被按**共享航班号**重复列多行（实测 15:15 那班同时存在
           CZ3417 / 长龙 GJ3029 / 厦航 MF1196 / 吉祥 HO7413 等多行），
           所以行内的 ``"code"`` 字段经常指向**邻座**航班，不能当航班号用。
        3. ``extparams`` 等字段被服务端做了拼接混淆（字段名中间会被别的片段
           截断），不能作为可靠锚点。

        因此这里以 **``binfo.airCode`` 作为行锚点**（它是该行自己的销售航班号），
        并按 ``"binfo"`` 出现位置切行；价格优先取"本行自己的 code 之后"的
        ``minPrice``，找不到才退回本行第一个 ``minPrice``。
        """
        if not text:
            return []
        # 响应里引号被多重转义（\" 甚至 \\"），先归一化
        s = text.replace('\\\\"', '"').replace('\\"', '"')

        starts = [m.start() for m in cls._RE_BINFO.finditer(s)]
        if not starts:
            return []

        collected: list = []
        for i, b in enumerate(starts):
            end = starts[i + 1] if i + 1 < len(starts) else min(len(s), b + 12000)
            block = s[b:end]

            # ---- 航班号：本行自己的 airCode（可能多个，含共享航班号）----
            codes: list = []
            arr_hit = cls._RE_AIRCODE_ARR.search(block)
            if arr_hit:
                codes = [c for c in cls._RE_QUOTED.findall(arr_hit.group(1))]
            name_hit = cls._RE_NAME.search(block)
            if not codes and name_hit:
                codes = [name_hit.group(1)]
            fno_list = []
            for raw in codes:
                fno = flights_mod.norm_flight_no(raw)
                if fno and fno not in fno_list:
                    fno_list.append(fno)
            if not fno_list:
                continue

            # ---- 价格：优先本行自己的 code 之后的 minPrice ----
            price = None
            for fno in fno_list:
                m_own = re.search(r'"code"\s*:\s*"%s"' % re.escape(fno), block)
                if m_own:
                    tail = block[m_own.end():]
                    nxt = cls._RE_ANY_CODE.search(tail)
                    if nxt:
                        tail = tail[:nxt.start()]
                    mp = cls._RE_MINPRICE.search(tail)
                    if mp:
                        price = float(mp.group(1))
                        break
            if price is None:
                mp = cls._RE_MINPRICE.search(block)
                if mp:
                    price = float(mp.group(1))
            if price is None:
                continue
            # 价格合理性：拼接碎片会混进 "53"、"59" 这种两位数字，按统一下限剔掉
            if not (flights_mod.PRICE_MIN <= price <= flights_mod.PRICE_MAX):
                continue

            # ---- 时刻 / 机场 / 航司 / 机型 ----
            def first(rx, src=block):
                m = rx.search(src)
                return m.group(1) if m else ""

            airline, aircraft = "", ""
            if name_hit:
                raw_name = name_hit.group(1)
                aircraft = cls._clean_label(name_hit.group(2) or "")
                airline = re.sub(r"[A-Z0-9]{2}\d{3,4}$", "", raw_name).strip() or raw_name
                airline = cls._clean_label(airline)

            base = {
                "depart_time": flights_mod.norm_time(first(cls._RE_DEPTIME)),
                "arrive_time": flights_mod.norm_time(first(cls._RE_ARRTIME)),
                "price": price,
                "airline": airline,
                "aircraft": aircraft,
                "dep_airport": first(cls._RE_DEPAP),
                "arr_airport": first(cls._RE_ARRAP),
                "dep_airport_id": first(cls._RE_DEPAPID),
                "arr_airport_id": first(cls._RE_ARRAPID),
                "dep_terminal": first(cls._RE_DEPTERM),
                "arr_terminal": first(cls._RE_ARRTERM),
            }
            for fno in fno_list:
                rec = dict(base, flight_no=fno)
                # 航司名以航班号前缀为准（响应里的 name 字段可能被邻行串到）
                rec["airline"] = flights_mod.carrier_name(fno) or base["airline"]
                collected.append(rec)

        return cls._pick_best_per_flight(collected)

    @staticmethod
    def _clean_label(value: str) -> str:
        """清掉被拼接污染的标签。

        去哪儿的字段拼接会让 `name[1]` 偶尔变成
        `ota_sort_family_first_position_ab` 这类开关名，必须剔除，
        否则机型/航司字段会出现噪声。
        """
        v = (value or "").strip()
        if not v or len(v) > 24:
            return ""
        if "_" in v or v.endswith("Ab"):
            return ""
        if re.search(r"false|true|_ab|sort|position|switch|show", v, re.I):
            return ""
        return v

    @staticmethod
    def _pick_best_per_flight(rows: list) -> list:
        """同一航班号可能有多行（含残缺行）：取字段最全的一行，同分取低价。"""
        fields = ("depart_time", "arrive_time", "dep_airport_id",
                  "arr_airport_id", "airline", "aircraft")

        def score(r):
            filled = sum(1 for f in fields if r.get(f))
            return (filled, -r["price"])

        best: dict = {}
        for r in rows:
            cur = best.get(r["flight_no"])
            if cur is None or score(r) > score(cur):
                best[r["flight_no"]] = r
        return list(best.values())

    @staticmethod
    def _extract_qunar_flights(text: str) -> list:
        """抽取逐航班记录：先按标准 JSON 试，失败再按去哪儿片段切分。

        返回每项含 flight_no / depart_time / arrive_time / price / airline /
        dep_airport / arr_airport 等。
        """
        if not text:
            return []
        # 路线一：如果哪次响应是干净 JSON（字段改名/换接口），走通用解析
        obj = flights_mod._loads_deep(text)
        if isinstance(obj, dict):
            data = obj.get("data")
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except Exception:
                    data = None
            scope = data if isinstance(data, (dict, list)) else obj
            recs = flights_mod.extract_records(scope)
            if recs:
                return recs
        # 路线二：去哪儿实际的"截断片段"形态
        return QunarCrawler._parse_flight_objects(text)

    @staticmethod
    def _extract_qunar_prices(text: str) -> list:
        """解析 touchInnerList JSON，提取航班最低价。

        去哪儿接口 data 字段是字符串化的 JSON（引号被转义为 \\"）。
        实测只有 minPrice 字段可信（航班最低价，与 DOM 渲染价一致）；
        totalPrice 字段含非价格数据（如 "127b"、4、42 等编码/附加费），
        不可作为价格候选。

        解析优先级：
        1) json.loads 全量解析 data 字符串，递归取 minPrice
        2) 兜底正则（兼容转义引号 \\" 形式）只取 minPrice
        """
        if not text:
            return []
        try:
            obj = json.loads(text)
        except Exception:
            return QunarCrawler._regex_extract_prices(text)

        data = obj.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                return QunarCrawler._regex_extract_prices(text)
        if not isinstance(data, dict):
            return QunarCrawler._regex_extract_prices(text)

        prices: list = []

        # 1) 顶层 minPrice（最可信，实测与 DOM 渲染价一致）
        v = data.get("minPrice")
        try:
            iv = int(float(v))
            if 100 <= iv <= 50000:
                prices.append(iv)
        except Exception:
            pass
        if prices:
            return prices

        # 2) 递归遍历航班节点，只取 minPrice（totalPrice 含非价格数据）
        def scan(node):
            if isinstance(node, dict):
                v = node.get("minPrice")
                if v is not None:
                    try:
                        iv = int(float(v))
                        if 100 <= iv <= 50000:
                            prices.append(iv)
                    except Exception:
                        pass
                for v in node.values():
                    if isinstance(v, (dict, list)):
                        scan(v)
            elif isinstance(node, list):
                for v in node:
                    scan(v)

        scan(data)
        if prices:
            return prices

        # 3) 兜底正则
        return QunarCrawler._regex_extract_prices(text)

    @staticmethod
    def _regex_extract_prices(text: str) -> list:
        """正则兜底：只取 minPrice，要求值后跟引号闭合（排除 "127b" 等非数字值）。

        实测 totalPrice 字段含 "127b"、"4" 等非价格数据，
        故正则只匹配 minPrice 且要求数字后紧跟引号。
        """
        prices: list = []
        # minPrice":"910" 或 minPrice\":\"910\"  要求数字后有引号闭合
        for m in re.finditer(
            r'minPrice\\?["\']\s*:\s*\\?["\'](\d{3,5})\\?["\']',
            text,
        ):
            try:
                prices.append(int(m.group(1)))
            except Exception:
                pass
        return [p for p in prices if 100 <= p <= 50000]
