"""Tushare 响应映射（ResponseMapper）：上游 DataFrame 行 → 统一 Bar 模型。

输出列/单位换算（股票 vol 手×100、amount 千元×1000；期货 vol 手保持、
amount 万元×10000）见 docs/providers/tushare.md（唯一出处，本文不复述）。
探针运行记录见 probes/results/PROBE_LOG.md（本地文件，不入库）。

缺失值与 NaN 统一映射为 null；扩展字段仅按受控白名单进入 extensions。
"""

import math
from datetime import datetime
from typing import Any

from tapehub.domain.models import CN_TIMEZONE, Bar

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
    """trade_date（YYYYMMDD）→ 该交易日 00:00 北京时间（+08:00）。"""
    return datetime.strptime(str(value), "%Y%m%d").replace(tzinfo=CN_TIMEZONE)


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
