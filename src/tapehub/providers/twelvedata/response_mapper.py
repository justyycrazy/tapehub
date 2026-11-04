"""Twelve Data 响应映射（ResponseMapper）：上游 as_json() 行 → 统一 Bar 模型。

响应结构已按官方文档核实（values 数组，默认倒序=最新在前）：

- 每个元素字段：datetime（bar 开盘时间，墙钟时间无时区后缀）、
  open/high/low/close/volume，均为**字符串**类型；
- datetime 的墙钟口径按资产类型区分（2026-11-04 实测 + 官方文档
  https://twelvedata.com/exchanges/physical_currency|digital_currency）：
  外汇按 Australia/Sydney（AEDT/AEST 随夏令时切换），加密货币按 UTC；
  解析为对应时区的 aware datetime，到统一北京时间的归一化由领域层完成；
- 外汇/加密货币通常无 volume 字段：缺失时标准 volume 为 null，
  不用 0 冒充；能力目录 fields 亦不含 volume；
- 缺失值与 NaN 统一映射为 null。
"""

import math
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from tapehub.domain.models import AssetType, Bar

# 外汇 datetime 的显示时区（官方文档：All times are displayed in the
# Australia/Sydney timezone；ZoneInfo 自动处理 AEDT/AEST 夏令时切换）
SYDNEY_TIMEZONE = ZoneInfo("Australia/Sydney")


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


def display_zone(asset_type: AssetType) -> timezone | ZoneInfo:
    """上游 datetime 墙钟的显示时区：外汇 Sydney，加密货币 UTC。"""
    if asset_type is AssetType.FOREX:
        return SYDNEY_TIMEZONE
    if asset_type is AssetType.CRYPTO:
        return timezone.utc
    raise ValueError(f"Twelve Data unsupported asset type: {asset_type.value}")


def _to_timestamp(value: Any, zone: timezone | ZoneInfo) -> datetime:
    """bar 开盘墙钟时间（无时区后缀）→ 按显示时区感知的 datetime。"""
    text = str(value).strip().replace("T", " ")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


def to_standard_bars(rows: list[dict[str, Any]], asset_type: AssetType) -> list[Bar]:
    """上游行列表 → 标准 Bar 列表（保持上游顺序，由分页组件统一升序排序）。"""
    zone = display_zone(asset_type)
    bars: list[Bar] = []
    for row in rows:
        bars.append(
            Bar(
                timestamp=_to_timestamp(row.get("datetime"), zone),
                open=_to_float(row.get("open")),
                high=_to_float(row.get("high")),
                low=_to_float(row.get("low")),
                close=_to_float(row.get("close")),
                volume=_to_float(row.get("volume")),  # 外汇/加密通常缺失 → null
                amount=None,  # 上游无成交额字段
            )
        )
    return bars
