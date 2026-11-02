"""AlphaFeed 响应映射（ResponseMapper）：上游返回对象 → 统一 Bar 模型。

响应结构已按官方文档核实（GET /v1/klines）：顶层 ``data`` 为 CompactKlineData
列式结构（各数组等长，同下标对应同一根K线）：

- 必填字段：timestamp（毫秒）、open/high/low/close/volume/amount；
- 可选字段：prev_close（前收盘价），存在且非空时进入受控 ``extensions``；
- 缺失值与 NaN 统一映射为 null。
"""

import math
from datetime import datetime, timezone
from typing import Any

from tapehub.domain.models import Bar

_EXTENSIONS_FIELDS = ("prev_close",)


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


def _data_columns(payload: Any) -> dict[str, list[Any]]:
    """提取列式数据：data 包装、单标的批量包装、直接列式均可。"""
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            payload = data
    if isinstance(payload, dict) and isinstance(payload.get("timestamp"), list):
        return payload
    # 防御：批量包装 {symbol: CompactKlineData} 时取第一个（单标的请求只返回一个）
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, dict) and isinstance(value.get("timestamp"), list):
                return value
    raise ValueError("unsupported AlphaFeed klines payload: expected columnar data")


def to_standard_bars(payload: Any) -> list[Bar]:
    """上游响应对象 → 标准 Bar 列表（保持上游顺序，由分页组件统一排序）。"""
    columns = _data_columns(payload)
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
