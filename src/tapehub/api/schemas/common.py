"""API 通用 schema 工具：时间序列化类型。

- ``UtcDatetime``：服务级时间戳（健康检查、能力目录 meta），统一 UTC、Z 结尾；
- ``MarketDatetime``：行情数据时间，偏移由领域层按市场规范时区归一化决定
  （CN → 北京时间 +08:00，其余 → UTC），序列化原样保留偏移。
"""

from datetime import datetime, timezone
from typing import Annotated

from pydantic import PlainSerializer


def serialize_utc_z(value: datetime) -> str:
    """统一输出格式：UTC，ISO 8601，以 Z 结尾。"""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def serialize_market_offset(value: datetime) -> str:
    """市场规范时区时间：偏移已在领域层归一化，序列化原样保留偏移。

    UTC（+00:00）按既有外部约定渲染为 ``Z`` 结尾；北京时间渲染为 ``+08:00``。
    """
    return value.isoformat().replace("+00:00", "Z")


UtcDatetime = Annotated[datetime, PlainSerializer(serialize_utc_z, return_type=str)]
MarketDatetime = Annotated[
    datetime, PlainSerializer(serialize_market_offset, return_type=str)
]
