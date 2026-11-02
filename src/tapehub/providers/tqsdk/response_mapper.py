"""TqSdk 响应映射（ResponseMapper）：上游 DataFrame 行 → 统一 Bar 模型。

输出列已按官方文档核实：

- ``id, datetime, open, high, low, close, volume, open_oi, close_oi``；
- datetime 为自 unix epoch 以来的**纳秒**数（K 线起点，按北京时间），
  绝对时间戳，标准化为 UTC；
- 无成交额字段：标准 ``amount`` 恒为 null，不用 0 冒充；
- 持仓量 open_oi/close_oi 进入受控 ``extensions``；volume 保持手；
- 缺失值与 NaN 统一映射为 null。
"""

import math
from datetime import datetime, timezone
from typing import Any

from tapehub.domain.models import Bar

_EXTENSIONS_FIELDS = ("open_oi", "close_oi")


def _to_float(value: Any) -> float | None:
    """转换为 float；None/NaN/不可转换值返回 None，不用 0 冒充。"""
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result):
        return None
    return result


def _to_timestamp_ns(value: Any) -> datetime:
    """纳秒时间戳 → UTC datetime。"""
    nanos = int(value)
    seconds, remainder_ns = divmod(nanos, 1_000_000_000)
    return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(
        microsecond=remainder_ns // 1000
    )


def to_standard_bars(rows: list[dict[str, Any]]) -> list[Bar]:
    """上游行列表 → 标准 Bar 列表（保持上游顺序，由分页组件统一排序）。"""
    bars: list[Bar] = []
    for row in rows:
        extensions: dict[str, Any] = {}
        for field in _EXTENSIONS_FIELDS:
            value = _to_float(row.get(field))
            if value is not None:
                extensions[field] = value
        bars.append(
            Bar(
                timestamp=_to_timestamp_ns(row.get("datetime")),
                open=_to_float(row.get("open")),
                high=_to_float(row.get("high")),
                low=_to_float(row.get("low")),
                close=_to_float(row.get("close")),
                volume=_to_float(row.get("volume")),  # 期货成交量保持手
                amount=None,  # 上游无成交额字段
                extensions=extensions or None,
            )
        )
    return bars
