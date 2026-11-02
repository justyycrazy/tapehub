"""TickFlow 响应映射（ResponseMapper）：上游返回对象 → 统一 Bar 模型。

响应结构已按官方文档核实（CompactKlineData，紧凑列式，每字段为数组）：

- 必填字段：timestamp（毫秒）、open/high/low/close/volume/amount；
- 可选字段：prev_close（前收盘）、open_interest（持仓量）、settlement_price（结算价），
  仅在存在且非空时进入受控 ``extensions``；
- SDK 原始模式返回含数组字段的对象（``klines['close'][-1]``）；
  若外层包含 ``data`` 字段则取其 ``data``；缺失值与 NaN 统一映射为 null。
"""

import math
from datetime import datetime, timezone
from typing import Any

from tapehub.domain.models import Bar

_EXTENSIONS_FIELDS = ("prev_close", "open_interest", "settlement_price")
_BAR_FIELDS = ("open", "high", "low", "close", "volume", "amount")


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


def _to_timestamp_ms(value: Any) -> datetime:
    """毫秒时间戳 → UTC datetime。"""
    millis = int(value)
    seconds, remainder = divmod(millis, 1000)
    return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(
        microsecond=remainder * 1000
    )


def _data_columns(payload: Any) -> dict[str, list[Any]] | None:
    """提取列式数据：优先 payload['data']，其次 payload 本身。"""
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            payload = data
    if isinstance(payload, dict) and isinstance(payload.get("timestamp"), list):
        return payload
    return None


def to_standard_bars(payload: Any) -> list[Bar]:
    """上游响应对象 → 标准 Bar 列表（保持上游顺序，由分页组件统一排序）。"""
    columns = _data_columns(payload)
    if columns is None:
        raise ValueError("unsupported TickFlow klines payload: expected columnar data")
    timestamps = columns["timestamp"]
    count = len(timestamps)
    bars: list[Bar] = []
    for index in range(count):
        extensions: dict[str, Any] = {}
        for field in _EXTENSIONS_FIELDS:
            values = columns.get(field)
            if values is None:
                continue
            value = _to_float(values[index])
            if value is not None:
                extensions[field] = value
        bars.append(
            Bar(
                timestamp=_to_timestamp_ms(timestamps[index]),
                open=_to_float(columns["open"][index]),
                high=_to_float(columns["high"][index]),
                low=_to_float(columns["low"][index]),
                close=_to_float(columns["close"][index]),
                volume=_to_float(columns["volume"][index]),
                amount=_to_float(columns["amount"][index]),
                extensions=extensions or None,
            )
        )
    return bars
