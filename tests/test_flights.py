"""改造自测：逐航班解析 + 指定航班号告警（不联网，用构造的响应样本）

运行：  .venv\\Scripts\\python.exe tests\\test_flights.py
"""
import json
import logging
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import flights as F                      # noqa: E402
from core.alerter import Alerter                   # noqa: E402
from core.models import FlightPrice, Route         # noqa: E402
from crawlers.qunar import QunarCrawler            # noqa: E402

# 解析器会读本地"共享号 -> 实际承运号"学习缓存（user_data/qunar/aliases.json）。
# 单测必须与本地状态无关，所以指向临时文件并把进程内缓存置空。
QunarCrawler._ALIAS_FILE = os.path.join(tempfile.gettempdir(), "cz3417_test_aliases.json")
QunarCrawler._alias_cache = {}

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  <- {extra}" if extra and not cond else ""))


# --------------------------------------------------------------------------
# 样本 1：去哪儿 touchInnerList（data 是被转义的 JSON 字符串，顶层还有 minPrice）
# --------------------------------------------------------------------------
QUNAR_DATA = {
    "minPrice": 780,                      # 全航线最低价（不可归属到某架航班）
    "flightList": [
        {"flightNo": "CZ3417", "depTime": "15:15", "arrTime": "17:35",
         "minPrice": 1180, "airlineName": "南方航空",
         "depAirportName": "广州白云机场", "arrAirportName": "成都双流机场"},
        {"flightNo": "3U8888", "depTime": "07:20", "arrTime": "09:40",
         "minPrice": 780, "airlineName": "四川航空"},
        {"flightNo": "MU5301", "depTime": "21:50", "arrTime": "00:15",
         "minPrice": 690, "airlineName": "东方航空"},
    ],
}
QUNAR_TEXT = json.dumps({"ret": True, "data": json.dumps(QUNAR_DATA, ensure_ascii=False)},
                        ensure_ascii=False)

# --------------------------------------------------------------------------
# 样本 2：携程 flightListSearchForH5（fltitem 每项一架航班，policyinfo 内含 tprice）
# CZ3417 的 980 元政策 quantity=0（无票诱饵价），必须被剔除
# --------------------------------------------------------------------------
CTRIP_TEXT = json.dumps({
    "fltitem": [
        {"flightinfo": {"flightno": "CZ3417", "departtime": "15:15",
                        "arrivaltime": "17:35", "airlinename": "南方航空"},
         "policyinfo": [{"tprice": 1180, "quantity": 9},
                        {"tprice": 980, "quantity": 0},
                        {"tprice": 1320, "quantity": 3}]},
        {"flightinfo": {"flightno": "CA4302", "departtime": "08:10",
                        "arrivaltime": "10:30"},
         "policyinfo": [{"tprice": 820, "quantity": 5}]},
    ]
}, ensure_ascii=False)


def test_qunar_parse():
    print("\n[1] 去哪儿逐航班解析")
    recs = F.extract_records_from_text(QUNAR_TEXT)
    by_no = {r["flight_no"]: r for r in recs}
    check("解析出 3 架航班", len(recs) == 3, f"实际 {len(recs)}: {[r['flight_no'] for r in recs]}")
    cz = by_no.get("CZ3417")
    check("CZ3417 存在", cz is not None)
    if cz:
        check("CZ3417 价格=1180（不是顶层 780）", cz["price"] == 1180, f"实际 {cz['price']}")
        check("CZ3417 起飞时刻=15:15", cz["depart_time"] == "15:15", cz["depart_time"])
        check("CZ3417 到达时刻=17:35", cz["arrive_time"] == "17:35", cz["arrive_time"])
        check("CZ3417 机场=双流", "双流" in cz.get("arr_airport", ""), cz.get("arr_airport", ""))
    check("顶层 minPrice 780 未被算到任何航班",
          all(r["price"] != 780 or r["flight_no"] == "3U8888" for r in recs))


def test_ctrip_parse():
    print("\n[2] 携程逐航班解析")
    from crawlers.ctrip import CtripCrawler
    recs = CtripCrawler._extract_ctrip_flights(CTRIP_TEXT)
    by_no = {r["flight_no"]: r for r in recs}
    check("解析出 2 架航班", len(recs) == 2, f"实际 {len(recs)}")
    cz = by_no.get("CZ3417")
    check("CZ3417 存在", cz is not None)
    if cz:
        check("CZ3417 取 1180（无票诱饵价 980 被剔除）", cz["price"] == 1180, f"实际 {cz['price']}")
        check("CZ3417 时刻 15:15→17:35",
              (cz["depart_time"], cz["arrive_time"]) == ("15:15", "17:35"),
              f"{cz['depart_time']}→{cz['arrive_time']}")
    check("CA4302 = 820", by_no.get("CA4302", {}).get("price") == 820)


def make_route(**kw):
    base = dict(from_code="CAN", from_name="广州", to_code="CTU", to_name="成都",
                dates=["2026-09-22"], alert_threshold=1200,
                watch_flights=["CZ3417"], depart_time_from="14:00",
                depart_time_to="16:30")
    base.update(kw)
    return Route(**base)


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, title, desp):
        self.sent.append((title, desp))
        return True


def quiet_logger():
    lg = logging.getLogger("test")
    lg.handlers = [logging.NullHandler()]
    lg.setLevel(logging.CRITICAL)
    return lg


def test_alert_watch_mode():
    print("\n[3] 指定航班号模式告警")
    rows = [
        FlightPrice(platform="qunar", from_city="CAN", to_city="CTU",
                    depart_date="2026-09-22", price=1180, flight_no="CZ3417",
                    depart_time="15:15", arrive_time="17:35",
                    extra=json.dumps({"dep_airport": "广州白云", "arr_airport": "成都双流"},
                                     ensure_ascii=False)),
        FlightPrice(platform="ctrip", from_city="CAN", to_city="CTU",
                    depart_date="2026-09-22", price=1240, flight_no="CZ3417",
                    depart_time="15:15", arrive_time="17:35"),
        FlightPrice(platform="qunar", from_city="CAN", to_city="CTU",
                    depart_date="2026-09-22", price=690, route_level=True),
        FlightPrice(platform="qunar", from_city="CAN", to_city="CTU",
                    depart_date="2026-09-22", price=780, flight_no="3U8888",
                    depart_time="07:20"),
    ]
    nf = FakeNotifier()
    a = Alerter(quiet_logger(), notifier=nf)

    a.check_and_alert(make_route(), rows)
    check("有 CZ3417 且低于阈值 → 推送 1 次", len(nf.sent) == 1, f"实际 {len(nf.sent)}")
    if nf.sent:
        title, desp = nf.sent[0]
        check("推送标题含航班号 CZ3417", "CZ3417" in title, title)
        check("推送标题价=1180（跨平台取低，不是 690）", "1180" in title, title)
        check("推送正文含双流机场", "双流" in desp)
        check("推送正文含当日对比信息", "当天其它最低" in desp)

    # 阈值调低到 1000：1180 高于阈值 → 不推
    nf2 = FakeNotifier()
    Alerter(quiet_logger(), notifier=nf2).check_and_alert(
        make_route(alert_threshold=1000), rows)
    check("价格高于阈值 → 不推送", len(nf2.sent) == 0, f"实际 {len(nf2.sent)}")

    # 只有全航线最低价（无逐航班数据）→ 必须不推送（防误报）
    nf3 = FakeNotifier()
    Alerter(quiet_logger(), notifier=nf3).check_and_alert(
        make_route(), [r for r in rows if r.route_level or r.flight_no == "3U8888"])
    check("无逐航班数据时不拿全航线最低价误报", len(nf3.sent) == 0, f"实际 {len(nf3.sent)}")

    # CZ3417 存在但起飞时刻不在窗口内 → 不推送
    nf4 = FakeNotifier()
    morning = [FlightPrice(platform="qunar", from_city="CAN", to_city="CTU",
                           depart_date="2026-09-22", price=900, flight_no="CZ3417",
                           depart_time="08:00")]
    Alerter(quiet_logger(), notifier=nf4).check_and_alert(make_route(), morning)
    check("起飞时刻不在 14:00-16:30 窗口 → 不推送", len(nf4.sent) == 0, f"实际 {len(nf4.sent)}")


def test_legacy_mode():
    print("\n[4] 未配置航班号时保持原行为")
    rows = [
        FlightPrice(platform="qunar", from_city="CAN", to_city="CTU",
                    depart_date="2026-09-22", price=690, route_level=True),
    ]
    nf = FakeNotifier()
    Alerter(quiet_logger(), notifier=nf).check_and_alert(
        make_route(watch_flights=[], depart_time_from="", depart_time_to=""),
        rows)
    check("老行为下全航线最低价仍会推送", len(nf.sent) == 1, f"实际 {len(nf.sent)}")


def test_qunar_fragment_parse():
    """回归：去哪儿真实响应是"被截断 + 转义 + 共享航班号串行"的片段。

    这里复刻三个坑：
      1. data 不是合法 JSON（头部缺失）
      2. 邻座航班对象里会出现 "code":"CZ3417"（共享航班号引用）→ 不能当航班号用
      3. extparams 等字段被拼接污染
    正确行为：CZ3417 取自己 binfo 的时刻 + 自己 code 后的 minPrice(510)，
    而不是邻座的 19:30/500。
    """
    print("\n[6] 去哪儿截断片段 + 共享航班号串行（真实结构回归）")
    fragment = (
        'em":false},{"selected":false,"detailItemId":"G5","detailItemTitle":"华夏航空"},'
        # 邻座：西藏航 TV6262 19:30→22:00，其行尾的 code 指向 CZ3417（串行陷阱）
        '{"binfo":{"airCode":["TV6262"],"arrAirport":"双流","arrTerminal":"T2",'
        '"arrTime":"22:00","depAirport":"白云","depTerminal":"T3","depTime":"19:30",'
        '"name":["西藏航TV6262","空客320(中)"],"depAirportId":"CAN","arrAirportId":"CTU"},'
        '"cat":"","code":"CZ3417","minPrice":"500","extparams":"{\\"lowPrice\\":500}"},'
        # 目标：南航 CZ3417 15:15→17:35
        '{"binfo":{"airCode":["CZ3417"],"arrAirport":"双流","arrTerminal":"T1",'
        '"arrTime":"17:35","depAirport":"白云","depTerminal":"T2","depTime":"15:15",'
        '"name":["南航CZ3417","波音737(中)"],"depAirportId":"CAN","arrAirportId":"CTU"},'
        '"cat":"","code":"CZ3417","minPrice":"510","extparams":"{\\"lowPrice\\":510}"},'
        # 拼接污染样本：机型字段被开关名顶替
        '{"binfo":{"airCode":["3U8888"],"arrAirport":"天府","arrTime":"09:40",'
        '"depAirport":"白云","depTime":"07:20","name":["四川航空3U8888",'
        '"ota_sort_family_first_position_ab"],"depAirportId":"CAN","arrAirportId":"TFU"},'
        '"cat":"","code":"3U8888","minPrice":"780"},'
        # 碎片污染：两位数字不是价格
        '{"binfo":{"airCode":["MU9999"],"arrTime":"12:00","depTime":"10:00"},'
        '"code":"MU9999","minPrice":"53"}'
    )
    text = json.dumps({"ret": "true", "msg": "查询成功!", "code": "0", "data": fragment},
                      ensure_ascii=False)

    recs = QunarCrawler._extract_qunar_flights(text)
    by_no = {r["flight_no"]: r for r in recs}
    print(f"      解析到: {sorted(by_no)}")

    cz = by_no.get("CZ3417")
    check("CZ3417 被解析出来", cz is not None)
    if cz:
        check("CZ3417 价格=510（不是邻座的 500）", cz["price"] == 510, f"实际 {cz['price']}")
        check("CZ3417 时刻=15:15→17:35（不是邻座的 19:30→22:00）",
              (cz["depart_time"], cz["arrive_time"]) == ("15:15", "17:35"),
              f"{cz['depart_time']}→{cz['arrive_time']}")
        check("CZ3417 航司=南方航空（由航班号前缀权威推导）",
              cz.get("airline") == "南方航空", cz.get("airline"))
        check("CZ3417 到达机场=双流(CTU)",
              cz.get("arr_airport_id") == "CTU" and "双流" in cz.get("arr_airport", ""))
    tv = by_no.get("TV6262")
    check("邻座 TV6262 仍是自己 19:30/500", tv is not None
          and tv["depart_time"] == "19:30" and tv["price"] == 500,
          f"{tv['depart_time'] if tv else '-'}/{tv['price'] if tv else '-'}")
    check("被 ab 开关名污染的机型被清空",
          by_no.get("3U8888", {}).get("aircraft") == "",
          str(by_no.get("3U8888", {}).get("aircraft")))
    check("两位数字碎片 53 被当作非法价格剔除", "MU9999" not in by_no)

    nf = FakeNotifier()
    rows = [
        FlightPrice(platform="qunar", from_city="CAN", to_city="CTU",
                    depart_date="2026-09-22", price=cz["price"], flight_no="CZ3417",
                    depart_time=cz["depart_time"], arrive_time=cz["arrive_time"])
    ] if cz else []
    if rows:
        Alerter(quiet_logger(), notifier=nf).check_and_alert(make_route(), rows)
        check("端到端：CZ3417 ¥510 ≤ 阈值1200 → 触发推送", len(nf.sent) == 1)


def test_time_helpers():
    print("\n[5] 时刻/航班号归一化")
    check("norm_flight_no('cz 3417') == 'CZ3417'", F.norm_flight_no("cz 3417") == "CZ3417")
    check("数字+字母前缀 3U8888（川航）", F.norm_flight_no("3U8888") == "3U8888")
    check("数字+字母前缀 9C8901（春秋）", F.norm_flight_no("9c8901") == "9C8901")
    check("三字母 ICAO CSN3417", F.norm_flight_no("CSN3417") == "CSN3417")
    check("纯数字流水号被拒绝", F.norm_flight_no("123456") == "")
    check("过短字符串被拒绝", F.norm_flight_no("AB") == "")
    check("norm_time('9:05') == '09:05'", F.norm_time("9:05") == "09:05")
    check("窗口内 15:15", F.in_time_window("15:15", "14:00", "16:30"))
    check("窗口外 08:00", not F.in_time_window("08:00", "14:00", "16:30"))
    check("时刻未知时不误杀", F.in_time_window("", "14:00", "16:30"))
    check("承运人推导 CZ3417→南方航空", F.carrier_name("CZ3417") == "南方航空")
    check("承运人推导 3U8888→四川航空", F.carrier_name("3U8888") == "四川航空")
    check("承运人推导 GJ3029→长龙航空", F.carrier_name("GJ3029") == "长龙航空")
    check("承运人推导 ICAO CSN3417→南方航空", F.carrier_name("CSN3417") == "南方航空")
    check("未知承运人返回空", F.carrier_name("XX1234") == "")


if __name__ == "__main__":
    test_qunar_parse()
    test_ctrip_parse()
    test_alert_watch_mode()
    test_legacy_mode()
    test_qunar_fragment_parse()
    test_time_helpers()
    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print(f"  失败: {f}")
        sys.exit(1)
    print("全部通过")
