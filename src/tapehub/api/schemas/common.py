"""API 通用 schema 工具：UTC 时间序列化等。"""

from datetime import datetime, timezone
from typing import Annotated

from pydantic import PlainSerializer


def serialize_utc_z(value: datetime) -> str:
    """统一输出格式：UTC，ISO 8601，以 Z 结尾。"""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


UtcDatetime = Annotated[datetime, PlainSerializer(serialize_utc_z, return_type=str)]
