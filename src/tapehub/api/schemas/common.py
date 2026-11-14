"""API 通用 schema 工具：时间序列化类型。

- ``BeijingDatetime``：服务级时间戳（健康检查、能力目录 generated_at）；
- ``MarketDatetime``：行情数据时间，序列化原样保留偏移。
时间戳口径见 docs/api.md「时间边界与时区」。
"""

from datetime import datetime
from typing import Annotated

from pydantic import PlainSerializer

from tapehub.domain.models import CN_TIMEZONE


def serialize_beijing(value: datetime) -> str:
    """统一输出格式：北京时间，ISO 8601，以 +08:00 结尾。"""
    return value.astimezone(CN_TIMEZONE).isoformat()


def serialize_market_offset(value: datetime) -> str:
    """行情数据时间：领域层已统一归一化为北京时间（+08:00），原样序列化。"""
    return value.isoformat()


BeijingDatetime = Annotated[
    datetime, PlainSerializer(serialize_beijing, return_type=str)
]
MarketDatetime = Annotated[
    datetime, PlainSerializer(serialize_market_offset, return_type=str)
]
