"""数据模型"""
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional


@dataclass
class FlightPrice:
    platform: str           # ctrip / fliggy / tongcheng / qunar
    from_city: str          # 出发地（三字码）
    to_city: str            # 目的地（三字码）
    depart_date: str        # YYYY-MM-DD
    price: float            # 最低价（含税或裸价，视平台展示）
    airline: str = ""       # 航司
    flight_no: str = ""     # 航班号
    depart_time: str = ""   # HH:MM
    arrive_time: str = ""   # HH:MM
    fetched_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    extra: str = ""         # 备用字段（JSON 字符串等）

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Route:
    from_code: str
    from_name: str
    to_code: str
    to_name: str
    dates: list
    alert_threshold: float = 0
