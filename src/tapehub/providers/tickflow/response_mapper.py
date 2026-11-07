"""TickFlow 响应映射（ResponseMapper）：上游返回对象 → 统一 Bar / Quote 模型。

K 线响应结构已按官方文档核实（CompactKlineData，紧凑列式，每字段为数组）：

- 必填字段：timestamp（毫秒）、open/high/low/close/volume/amount；
- 可选字段：prev_close（前收盘）、open_interest（持仓量）、settlement_price（结算价），
  仅在存在且非空时进入受控 ``extensions``；
- SDK 原始模式返回含数组字段的对象（``klines['close'][-1]``）；
  若外层包含 ``data`` 字段则取其 ``data``；缺失值与 NaN 统一映射为 null。

快照响应（QuotesResponse）：``data`` 数组，每项含 symbol/region/last_price/
prev_close/open/high/low/volume/amount/timestamp（毫秒）/session（可空枚举）/
ext（可空：name/change_pct/change_amount/amplitude/turnover_rate，比例字段
0.01 = 1%，即数值即百分数，按文档原值透出）。标准 Quote 模型无开高低/
昨收字段，这些字段与 session/ext 一并经 ``extensions`` 白名单透出；
timestamp 缺失或 last_price 不可解析属上游契约违背，抛 ``UPSTREAM_ERROR``。
"""

import math
from datetime import datetime
from typing import Any

from tapehub.domain.errors import UpstreamError
from tapehub.domain.models import CN_TIMEZONE, Bar, Quote
from tapehub.domain.timeutil import epoch_to_datetime

_EXTENSIONS_FIELDS = ("prev_close", "open_interest", "settlement_price")
_BAR_FIELDS = ("open", "high", "low", "close", "volume", "amount")

# 快照顶层字段的 extensions 白名单：数值字段（前收/开高低）经 _to_float
_QUOTE_TOP_NUMERIC = ("prev_close", "open", "high", "low")
# 快照 ext 块内的 extensions 白名单：名称原样、比例/涨跌额转 float
_QUOTE_EXT_STRING = ("name",)
_QUOTE_EXT_NUMERIC = ("amplitude", "change_amount", "change_pct", "turnover_rate")


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
    """毫秒时间戳 → 北京时间 datetime（+08:00）（纪元算术，见 domain.timeutil）。"""
    return epoch_to_datetime(int(value) * 1_000_000, CN_TIMEZONE)


def _data_columns(payload: Any) -> dict[str, list[Any]] | None:
    """提取列式数据：优先 payload['data']，其次 payload 本身。"""
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            payload = data
    if isinstance(payload, dict) and isinstance(payload.get("timestamp"), list):
        return payload
    return None


def _field(source: Any, name: str) -> Any:
    """从 dict 或对象风格条目取字段；缺失返回 None。"""
    if isinstance(source, dict):
        return source.get(name)
    return getattr(source, name, None)


def _quote_entries(payload: Any, *, provider: str) -> list[Any]:
    """提取 QuotesResponse 的 data 数组：dict['data'] / 列表本身 / 对象 .data。"""
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return data
    elif isinstance(payload, list):
        return payload
    else:
        data = getattr(payload, "data", None)
        if isinstance(data, list):
            return data
    raise UpstreamError(
        "TickFlow quotes payload has no data array.",
        details={"provider": provider},
    )


def to_standard_quote(
    payload: Any, *, provider: str, received_at: datetime
) -> Quote:
    """上游快照响应 → 标准 Quote（单标的快照，取 data 首条）。

    - ``timestamp``（毫秒）缺失/非法、``last_price`` 不可解析属上游契约
      违背，抛 ``UPSTREAM_ERROR``（不伪造时间或价格）；
    - 五档盘口上游无源字段，恒 null（不得用 0 冒充）。
    """
    entries = _quote_entries(payload, provider=provider)
    if not entries:
        raise UpstreamError(
            "TickFlow quotes payload has an empty data array.",
            details={"provider": provider},
        )
    entry = entries[0]
    raw_ts = _field(entry, "timestamp")
    try:
        millis = int(raw_ts)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise UpstreamError(
            "TickFlow quotes payload has no valid timestamp.",
            details={"provider": provider, "field": "timestamp"},
        ) from exc
    last_price = _to_float(_field(entry, "last_price"))
    if last_price is None:
        raise UpstreamError(
            "TickFlow quotes payload has no valid last_price.",
            details={"provider": provider, "field": "last_price"},
        )
    extensions: dict[str, Any] = {}
    for name in ("region", "session"):
        raw = _field(entry, name)
        if raw is None:
            continue
        value = str(raw).strip()
        if value:
            extensions[name] = value
    for name in _QUOTE_TOP_NUMERIC:
        value = _to_float(_field(entry, name))
        if value is not None:
            extensions[name] = value
    ext_block = _field(entry, "ext")
    for name in _QUOTE_EXT_STRING:
        raw = _field(ext_block, name) if ext_block is not None else None
        if raw is None:
            continue
        value = str(raw).strip()
        if value:
            extensions[name] = value
    for name in _QUOTE_EXT_NUMERIC:
        value = _to_float(_field(ext_block, name)) if ext_block is not None else None
        if value is not None:
            extensions[name] = value
    return Quote(
        timestamp=_to_timestamp_ms(millis),
        last_price=last_price,
        volume=_to_float(_field(entry, "volume")),
        amount=_to_float(_field(entry, "amount")),
        provider=provider,
        received_at=received_at,
        extensions=extensions or None,
    )


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
