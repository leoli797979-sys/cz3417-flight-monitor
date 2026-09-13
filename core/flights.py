"""逐航班解析工具：把各平台原始 JSON 归一化成"每航班一条"记录。

为什么要这个模块
----------------
上游 JiPiao 只抓"当天全航线最低价"（qunar.py 里 ``float(min(prices))``、
ctrip.py 里 ``_pick_lowest``），``FlightPrice.flight_no`` 因此一直是空字符串，
所以它**无法监控指定航班号**（例如 CZ3417）。

本模块补上这一层：从原始响应里抽出 (航班号, 起飞时刻, 到达时刻, 价格)。
价格取该航班的最低价（多个舱位/政策取 min）。

设计原则：**按"值的形态"识别，不绑定平台字段名**
------------------------------------------------
去哪儿 touchInnerList 与携程 flightListSearchForH5 的字段命名差异很大，
且会随版本改版；硬编码 key 很容易因改版失效。这里改为匹配"值长什么样"
（CZ3417 / 15:15 这种形态），因此对字段改名不敏感。

安全约束：价格只在**本航班节点自己的子树**内向下找，且不跨越嵌套的其它航班
节点，避免把邻座航班的价格算到本航班头上；找到多个候选时按"键名可信度 +
价格更低"择优，避免把裸价/机建费当成含税总价。
"""
from __future__ import annotations

import json
import re
from typing import Iterator, Optional

# 时刻形态：15:15 / 9:05
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")

PRICE_MIN = 100
PRICE_MAX = 50000

# 价格键名 → 可信度优先级（越小越可信）。含税总价类排前面。
PRICE_KEY_ORDER = (
    "minprice", "lowestprice", "saleprice", "cptprice", "cabintotalprice",
    "displayprice", "totalprice", "tprice", "adultprice", "price",
)

DEP_KEY_RE = re.compile(r"dep|dpt|dept|take|start|off", re.I)
ARR_KEY_RE = re.compile(r"arr|land|end", re.I)
FLIGHT_KEY_RE = re.compile(r"flight|flt|^fn$|carrier|airline.*(no|num|code)", re.I)
AIRPORT_KEY_RE = re.compile(r"(airport|port|terminal)", re.I)
DEP_AIRPORT_KEY_RE = re.compile(r"(^|[^a-z])(dep|dpt|dept|from|start)", re.I)
ARR_AIRPORT_KEY_RE = re.compile(r"(^|[^a-z])(arr|to|end|land)", re.I)

# 价格字段最多向子树下钻几层（携程 tprice 在 fltitem -> policyinfo 里，约 1~2 层）
MAX_DESCEND = 4


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def norm_flight_no(value) -> str:
    """把航班号归一成 'CZ3417' 形式；不像航班号则返回 ''。

    前缀有两种合法形态，按顺序尝试（不能用贪婪正则，否则 'CZ3417'
    会被切成前缀 'CZ3'）：
      - 2 位 IATA 代码，至少含一个字母：CZ / MU / 3U / 9C / 8L
      - 3 位 ICAO 代码，全字母：CSN / CCA / CSC
    纯数字前缀（如流水号 '123456'）一律拒绝。
    """
    if not isinstance(value, str):
        return ""
    v = value.strip().upper().replace(" ", "").replace("-", "")
    for plen in (2, 3):
        if len(v) - plen not in (3, 4):
            continue
        prefix, digits = v[:plen], v[plen:]
        if not digits.isdigit():
            continue
        if plen == 2 and prefix.isalnum() and any(c.isalpha() for c in prefix):
            return prefix + digits
        if plen == 3 and prefix.isalpha():
            return prefix + digits
    return ""


def norm_time(value) -> str:
    """把 '9:05' 归一成 '09:05'；不是时刻则返回 ''。"""
    if not isinstance(value, str):
        return ""
    v = value.strip()
    if not TIME_RE.match(v):
        return ""
    h, m = v.split(":")
    return f"{int(h):02d}:{m}"


def iter_dicts(obj) -> Iterator[dict]:
    """深度优先遍历所有 dict 节点。"""
    stack = [obj]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            yield node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def _price_of(value) -> Optional[float]:
    try:
        iv = float(str(value).strip().strip('"'))
    except Exception:
        return None
    if PRICE_MIN <= iv <= PRICE_MAX:
        return iv
    return None


def iter_dicts_bfs(obj) -> Iterator[dict]:
    """按层序（浅 → 深）遍历 dict 节点。

    同一个航班在响应里可能出现在多层（列表项 + 内层航段），层序能保证
    先命中"最外层"的那份，避免抓到深层推荐位里的杂项。
    """
    level = [obj]
    while level:
        nxt = []
        for node in level:
            if isinstance(node, dict):
                yield node
                nxt.extend(node.values())
            elif isinstance(node, list):
                nxt.extend(node)
        level = nxt


def find_flight_no(obj) -> str:
    """在整棵子树里找第一个（最外层）航班号。"""
    for node in iter_dicts_bfs(obj):
        fno = extract_flight_no(node)
        if fno:
            return fno
    return ""


def find_times(obj) -> tuple[str, str]:
    """在整棵子树里找第一组（最外层）起降时刻。

    注意：经停/中转航班这里只会拿到首个航段的时刻对。
    """
    for node in iter_dicts_bfs(obj):
        dep, arr = extract_times(node)
        if dep or arr:
            return dep, arr
    return "", ""


# --------------------------------------------------------------------------
# 单节点字段抽取
# --------------------------------------------------------------------------
def extract_flight_no(node: dict) -> str:
    """从节点里找航班号：先看键名像航班号的字段，再退化到扫所有字符串值。"""
    # 1) 键名可信的
    for key, value in node.items():
        if isinstance(value, str) and FLIGHT_KEY_RE.search(key):
            fno = norm_flight_no(value)
            if fno:
                return fno
    # 2) 退化：任何看起来像航班号的值
    for value in node.values():
        fno = norm_flight_no(value) if isinstance(value, str) else ""
        if fno:
            return fno
    return ""


def extract_times(node: dict) -> tuple[str, str]:
    """从节点里找 (起飞, 到达) 时刻。

    优先用键名区分（dep*/arr*），否则按出现顺序取前两个时刻。
    """
    dep = arr = ""
    loose: list[str] = []
    for key, value in node.items():
        t = norm_time(value) if isinstance(value, str) else ""
        if not t:
            continue
        if DEP_KEY_RE.search(key) and not dep:
            dep = t
        elif ARR_KEY_RE.search(key) and not arr:
            arr = t
        else:
            loose.append(t)
    if not dep:
        for t in loose:
            if t != arr:
                dep = t
                break
    if not arr:
        for t in loose:
            if t != dep:
                arr = t
                break
    return dep, arr


def extract_airline(node: dict) -> str:
    for key, value in node.items():
        if not isinstance(value, str):
            continue
        if re.search(r"airline|carrier", key, re.I) and not FLIGHT_KEY_RE.search(key):
            v = value.strip()
            if 1 < len(v) <= 20 and not norm_flight_no(v):
                return v
    return ""


def extract_airports(node: dict) -> tuple[str, str]:
    """抽出发/到达机场名（用于核对 CTU 双流 vs TFU 天府）。"""
    dep = arr = ""
    for key, value in node.items():
        if not isinstance(value, str) or not AIRPORT_KEY_RE.search(key):
            continue
        v = value.strip()
        if not (1 < len(v) <= 30):
            continue
        if DEP_AIRPORT_KEY_RE.search(key) and not dep:
            dep = v
        elif ARR_AIRPORT_KEY_RE.search(key) and not arr:
            arr = v
    return dep, arr


def _node_has_flight_no(node) -> bool:
    return isinstance(node, dict) and bool(extract_flight_no(node))


def extract_price(node: dict, max_depth: int = MAX_DESCEND) -> Optional[float]:
    """在节点自己的子树里找价格，不跨越嵌套的其它航班节点。"""
    best: tuple[int, float] | None = None
    stack: list[tuple[dict, int]] = [(node, 0)]
    while stack:
        cur, depth = stack.pop()
        for key, value in cur.items():
            kl = key.lower()
            if kl in PRICE_KEY_ORDER:
                price = _price_of(value)
                if price is not None:
                    prio = PRICE_KEY_ORDER.index(kl)
                    if best is None or (prio, price) < best:
                        best = (prio, price)
            if depth >= max_depth:
                continue
            if isinstance(value, dict):
                # 不进入"属于另一架航班"的子树，防止张冠李戴
                if value is not node and _node_has_flight_no(value):
                    continue
                stack.append((value, depth + 1))
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        if _node_has_flight_no(item):
                            continue
                        stack.append((item, depth + 1))
    return best[1] if best else None


# --------------------------------------------------------------------------
# 对外入口
# --------------------------------------------------------------------------
def extract_records(obj) -> list[dict]:
    """从解析好的 JSON 对象里抽出"每航班一条"记录。

    返回 list[dict]，每项含 flight_no / depart_time / arrive_time /
    price / airline / dep_airport / arr_airport。
    同一航班号出现多次时取最低价，并尽量保留带时刻的那条。
    """
    grouped: dict[str, dict] = {}
    for node in iter_dicts(obj):
        fno = extract_flight_no(node)
        if not fno:
            continue
        price = extract_price(node)
        if price is None:
            continue
        dep, arr = extract_times(node)
        dep_ap, arr_ap = extract_airports(node)

        rec = {
            "flight_no": fno,
            "depart_time": dep,
            "arrive_time": arr,
            "price": price,
            "airline": extract_airline(node),
            "dep_airport": dep_ap,
            "arr_airport": arr_ap,
        }

        old = grouped.get(fno)
        if old is None:
            grouped[fno] = rec
            continue
        # 保留"时刻更全 + 价格更低"的那条
        old_score = (bool(old["depart_time"]), -old["price"])
        new_score = (bool(rec["depart_time"]), -rec["price"])
        if new_score > old_score:
            # 新记录更有信息量：用旧值补全新记录的空字段
            rec["depart_time"] = rec["depart_time"] or old["depart_time"]
            rec["arrive_time"] = rec["arrive_time"] or old["arrive_time"]
            rec["airline"] = rec["airline"] or old["airline"]
            grouped[fno] = rec
        else:
            old["depart_time"] = old["depart_time"] or rec["depart_time"]
            old["arrive_time"] = old["arrive_time"] or rec["arrive_time"]
            old["airline"] = old["airline"] or rec["airline"]
    return list(grouped.values())


def extract_records_from_text(text: str) -> list[dict]:
    """从响应文本抽逐航班记录；文本本身可能是"字符串化的 JSON"。

    去哪儿把 data 字段做成被转义的 JSON 字符串，这里统一递归解一层。
    """
    obj = _loads_deep(text)
    if obj is None:
        return []
    return extract_records(obj)


def _loads_deep(text: str):
    """json.loads，并把仍是 JSON 字符串的字段再解一层（去哪儿 data 字段）。"""
    if not text:
        return None
    try:
        obj = json.loads(text)
    except Exception:
        return None
    if isinstance(obj, dict):
        for key in ("data", "result", "response"):
            inner = obj.get(key)
            if isinstance(inner, str) and inner.strip().startswith(("{", "[")):
                try:
                    obj[key] = json.loads(inner)
                except Exception:
                    pass
    return obj


def in_time_window(depart_time: str, lo: str = "", hi: str = "") -> bool:
    """起飞时刻是否落在 [lo, hi] 区间（任一端留空表示不限）。"""
    if not lo and not hi:
        return True
    t = norm_time(depart_time)
    if not t:
        # 时刻未知：不敢断言在窗口内，交给调用方按航班号决定
        return True
    if lo:
        lo_n = norm_time(lo) or lo
        if t < lo_n:
            return False
    if hi:
        hi_n = norm_time(hi) or hi
        if t > hi_n:
            return False
    return True


# --------------------------------------------------------------------------
# 承运人代码 → 航司名
# --------------------------------------------------------------------------
# 去哪儿/携程的响应里，航司名和航班号可能来自不同的（甚至被拼接串行的）字段，
# 但航班号前缀是权威且稳定的。用这张表推导航司名，比信任页面里的 name 字段可靠。
CARRIER_NAMES = {
    # 二字 IATA
    "CZ": "南方航空", "MU": "东方航空", "CA": "国际航空", "HU": "海南航空",
    "3U": "四川航空", "ZH": "深圳航空", "MF": "厦门航空", "SC": "山东航空",
    "FM": "上海航空", "GS": "天津航空", "9C": "春秋航空", "HO": "吉祥航空",
    "KN": "中国联合航空", "EU": "成都航空", "GJ": "长龙航空", "TV": "西藏航空",
    "JD": "首都航空", "G5": "华夏航空", "KY": "昆明航空", "8L": "祥鹏航空",
    "PN": "西部航空", "AQ": "九元航空", "QW": "青岛航空", "NS": "河北航空",
    "RY": "江西航空", "GY": "多彩贵州航空", "DR": "瑞丽航空", "GT": "桂林航空",
    "UQ": "乌鲁木齐航空", "Y8": "金鹏航空", "CN": "大新华航空", "FU": "福州航空",
    "BK": "奥凯航空", "9H": "长安航空", "I9": "幸福航空", "GX": "北部湾航空",
    # 三字 ICAO（部分响应会给 ICAO 形式）
    "CSN": "南方航空", "CCA": "国际航空", "CES": "东方航空", "CHH": "海南航空",
    "CSC": "四川航空", "CSZ": "深圳航空", "CXA": "厦门航空", "CDG": "山东航空",
    "CQH": "春秋航空", "DKH": "吉祥航空", "CUA": "中国联合航空",
}


def carrier_name(flight_no: str) -> str:
    """由航班号前缀推导航司名；未知代码返回 ''。"""
    fno = norm_flight_no(flight_no) or (flight_no or "").strip().upper()
    if not fno:
        return ""
    if len(fno) >= 5 and fno[:2] in CARRIER_NAMES:
        return CARRIER_NAMES[fno[:2]]
    if len(fno) >= 6 and fno[:3] in CARRIER_NAMES:
        return CARRIER_NAMES[fno[:3]]
    return ""
