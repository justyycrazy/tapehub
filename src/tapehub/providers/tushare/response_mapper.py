"""Tushare 响应映射（ResponseMapper）：上游 DataFrame 行 → 统一 Bar 模型。

输出列与单位均已按官方文档核实：

- 股票（pro_bar → daily，doc_id=27）：trade_date YYYYMMDD；vol 成交量（手）、
  amount 成交额（千元）→ 标准化为股（×100）与元（×1000）；
  扩展字段 pre_close/change/pct_chg；
- 期货（fut_daily，doc_id=138）：vol 成交量（手，保持）、amount 成交金额
  （万元）→ 标准化为元（×10000）；扩展字段 pre_close/settle/oi（持仓量）。

缺失值与 NaN 统一映射为 null；扩展字段仅按受控白名单进入 extensions。
"""

import math
from datetime import datetime, timezone
from typing import Any

from tapehub.domain.models import Bar

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


def _scaled(value: Any, scale: float) -> float | None:
    result = _to_float(value)
    if result is None:
        return None
    return result * scale


def _to_timestamp(value: Any) -> datetime:
    """trade_date（YYYYMMDD）→ 该交易日 00:00 UTC。"""
    return datetime.strptime(str(value), "%Y%m%d").replace(tzinfo=timezone.utc)


def _stock_bar(row: dict[str, Any]) -> Bar:
    extensions: dict[str, Any] = {}
    for source, name in (("pre_close", "pre_close"), ("change", "change"), ("pct_chg", "pct_chg")):
        value = _to_float(row.get(source))
        if value is not None:
            extensions[name] = value
    return Bar(
        timestamp=_to_timestamp(row.get("trade_date")),
        open=_to_float(row.get("open")),
        high=_to_float(row.get("high")),
        low=_to_float(row.get("low")),
        close=_to_float(row.get("close")),
        volume=_scaled(row.get("vol"), 100.0),  # 手 → 股
        amount=_scaled(row.get("amount"), 1000.0),  # 千元 → 元
        extensions=extensions or None,
    )


def _futures_bar(row: dict[str, Any]) -> Bar:
    extensions: dict[str, Any] = {}
    for source, name in (("pre_close", "pre_close"), ("settle", "settle"), ("oi", "oi")):
        value = _to_float(row.get(source))
        if value is not None:
            extensions[name] = value
    return Bar(
        timestamp=_to_timestamp(row.get("trade_date")),
        open=_to_float(row.get("open")),
        high=_to_float(row.get("high")),
        low=_to_float(row.get("low")),
        close=_to_float(row.get("close")),
        volume=_to_float(row.get("vol")),  # 期货成交量保持手
        amount=_scaled(row.get("amount"), 10000.0),  # 万元 → 元
        extensions=extensions or None,
    )


_MAPPERS = {"pro_bar": _stock_bar, "fut_daily": _futures_bar}


def to_standard_bars(operation: str, rows: list[dict[str, Any]]) -> list[Bar]:
    """按 operation 选择映射配置，批量转换为标准 Bar 列表。"""
    mapper = _MAPPERS.get(operation)
    if mapper is None:
        raise ValueError(f"no response mapping for operation {operation!r}")
    return [mapper(row) for row in rows]
