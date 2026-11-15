"""Tushare 响应映射（ResponseMapper）：上游 DataFrame 行 → 统一 Bar 模型。

输出列/单位换算按周期分支（口径定案见 docs/providers/tushare.md）：
日线上游 vol 手/amount 千元；周/月线上游实数据已是股/元（官方文档声明
「手/千元」与实数据矛盾，同窗求和互证比率精确 ×100/×1000，探针运行
记录见 probes/results/PROBE_LOG.md，本地文件不入库）。

缺失值与 NaN 统一映射为 null；扩展字段仅按受控白名单进入 extensions。
"""

import math
from datetime import datetime
from typing import Any

from tapehub.domain.intervals import Interval
from tapehub.domain.models import CN_TIMEZONE, Bar

_BAR_FIELDS = ("open", "high", "low", "close", "volume", "amount")

# pro_bar 量额换算按周期分支（口径定案见 docs/providers/tushare.md）：
# 日线上游 vol 手/amount 千元；周/月线上游实数据已是股/元，如实透传。
# 官方文档对周月线声明「手/千元」（doc_id=95/171/172）与实数据矛盾
# （同窗求和互证比率精确 ×100/×1000，探针 2026-11-15 第 13 轮）
_BAR_SCALES: dict[str, tuple[float, float]] = {
    "P1D": (100.0, 1000.0),  # vol 手→股、amount 千元→元
    "P1W": (1.0, 1.0),
    "P1M": (1.0, 1.0),
}


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


def _stock_bar(row: dict[str, Any], volume_scale: float, amount_scale: float) -> Bar:
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
        volume=_scaled(row.get("vol"), volume_scale),
        amount=_scaled(row.get("amount"), amount_scale),
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


def to_standard_bars(operation: str, interval: Interval, rows: list[dict[str, Any]]) -> list[Bar]:
    """按 operation 与周期选择映射与量额换算，批量转换为标准 Bar 列表。"""
    if operation == "pro_bar":
        scales = _BAR_SCALES.get(interval.code)
        if scales is None:
            raise ValueError(f"no unit scaling for interval {interval.code}")
        return [_stock_bar(row, *scales) for row in rows]
    if operation == "fut_daily":
        return [_futures_bar(row) for row in rows]
    raise ValueError(f"no response mapping for operation {operation!r}")
