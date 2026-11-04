"""API 通用 schema 工具：时间序列化类型。

- ``BeijingDatetime``：项目内部统一北京时间（+08:00）——服务级时间戳
  （健康检查、能力目录 generated_at）一律使用；
- ``MarketDatetime``：行情数据时间，领域层已全市场统一归一化为北京时间
  （+08:00，2026-11-04 数据探针确认上游口径后统一），序列化原样保留偏移；
  UTC（+00:00）渲染为 ``Z`` 的分支仅作兼容保留。
"""

from datetime import datetime
from typing import Annotated

from pydantic import PlainSerializer

from tapehub.domain.models import CN_TIMEZONE


def serialize_beijing(value: datetime) -> str:
    """统一输出格式：北京时间，ISO 8601，以 +08:00 结尾。"""
    return value.astimezone(CN_TIMEZONE).isoformat()


def serialize_market_offset(value: datetime) -> str:
    """市场规范时区时间：偏移已在领域层归一化，序列化原样保留偏移。"""
    return value.isoformat().replace("+00:00", "Z")


BeijingDatetime = Annotated[
    datetime, PlainSerializer(serialize_beijing, return_type=str)
]
MarketDatetime = Annotated[
    datetime, PlainSerializer(serialize_market_offset, return_type=str)
]
