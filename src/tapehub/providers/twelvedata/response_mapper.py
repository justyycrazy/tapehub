"""Twelve Data 响应映射（ResponseMapper）：上游 as_json() 行 → 统一 Bar 模型。

响应结构已按官方文档核实（values 数组，默认倒序=最新在前）：

- 每个元素字段：datetime（bar 开盘时间，外汇/加密货币按 UTC）、
  open/high/low/close/volume，均为**字符串**类型；
- 外汇/加密货币通常无 volume 字段：缺失时标准 volume 为 null，
  不用 0 冒充；能力目录 fields 亦不含 volume；
- 缺失值与 NaN 统一映射为 null。
"""

import math
from datetime import datetime, timezone
from typing import Any

from tapehub.domain.models import Bar


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


def _to_timestamp(value: Any) -> datetime:
    """bar 开盘时间 → UTC datetime（外汇/加密货币按官方文档为 UTC）。"""
    text = str(value).strip().replace("T", " ")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def to_standard_bars(rows: list[dict[str, Any]]) -> list[Bar]:
    """上游行列表 → 标准 Bar 列表（保持上游顺序，由分页组件统一升序排序）。"""
    bars: list[Bar] = []
    for row in rows:
        bars.append(
            Bar(
                timestamp=_to_timestamp(row.get("datetime")),
                open=_to_float(row.get("open")),
                high=_to_float(row.get("high")),
                low=_to_float(row.get("low")),
                close=_to_float(row.get("close")),
                volume=_to_float(row.get("volume")),  # 外汇/加密通常缺失 → null
                amount=None,  # 上游无成交额字段
            )
        )
    return bars
