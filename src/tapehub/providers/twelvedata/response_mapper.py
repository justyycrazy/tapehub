"""Twelve Data 响应映射（ResponseMapper）：上游 as_json()/WS price → 统一 Bar / Quote 模型。

bars（REST time-series）结构已按官方文档核实：

- time-series：values 数组，默认倒序=最新在前。每个元素字段：datetime
  （bar 开盘时间，墙钟时间无时区后缀）、open/high/low/close/volume，
  均为**字符串**类型；
- datetime 的墙钟口径按资产类型区分（2026-11-04 实测 + 官方文档
  https://twelvedata.com/exchanges/physical_currency|digital_currency）：
  外汇按 Australia/Sydney（AEDT/AEST 随夏令时切换），加密货币按 UTC；
  解析为对应时区的 aware datetime，到统一北京时间的归一化由领域层完成；
- 外汇/加密货币通常无 volume 字段：缺失时标准 volume 为 null，
  不用 0 冒充（bars 能力目录 fields 亦不含 volume）；
- 缺失值与 NaN 统一映射为 null。

quote（WS 实时流 price 事件，2026-11-05 探针实证，报告
probes/results/twelvedata/ws-behavior-20261105T221000+0800.md 不入库）：

- M2 快照与 M3 流同源：均为 WS 订阅的最新 price 事件（REST /quote
  路径已退役）；
- 字段按资产类别差异且少于官方文档示例：加密（BTC/USD）带
  ``exchange``，外汇（EUR/USD）无 ``exchange``；两者均**无 bid/ask、
  无 volume**（文档 XAU/USD 示例的顶档买卖价不可推广到货币对）——
  标准字段只有 ``timestamp`` + ``last_price``，与能力目录 fields 声明一致；
- ``timestamp`` 为**分钟桶** Unix 秒（对齐分钟边界，非 tick 时刻），
  按 UTC 解释，统一北京时间归一化由领域层 Quote 完成；快照新鲜度
  由 ``meta.received_at``（网关接收时刻）表达；
- ``price`` → ``last_price``；缺失/不可解析属上游契约违背，抛
  UPSTREAM_ERROR（不伪造）；
- extensions 白名单 4 个字符串字段：``currency_base`` / ``currency_quote``
  / ``exchange`` / ``type``（缺失不透出）。
"""

import math
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from tapehub.domain.errors import UpstreamError
from tapehub.domain.models import AssetType, Bar, Quote

# 外汇 datetime 的显示时区（官方文档：All times are displayed in the
# Australia/Sydney timezone；ZoneInfo 自动处理 AEDT/AEST 夏令时切换）
SYDNEY_TIMEZONE = ZoneInfo("Australia/Sydney")

# WS price 事件 extensions 白名单（2026-11-05 探针实证）：仅透出实测
# 稳定出现的字符串元数据字段；数值字段经 _to_float 转换，None 不透出
_PRICE_EXTENSIONS_FIELDS = ("currency_base", "currency_quote", "exchange", "type")


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


def ws_price_to_quote(
    payload: Any, *, provider: str, received_at: datetime
) -> Quote:
    """WS price 事件 → 标准 Quote（M2 快照与 M3 流共用，字段口径见模块 docstring）。

    - ``timestamp``（Unix 秒）缺失/非法、``price`` 不可解析属上游契约
      违背，抛 ``UPSTREAM_ERROR``（不伪造时间或价格）；
    - 五档盘口/成交量/成交额上游无源字段，恒 null（不得用 0 冒充）。
    """
    if not isinstance(payload, dict):
        raise UpstreamError(
            "Twelve Data price payload is not a JSON object.",
            details={"provider": provider},
        )
    raw_ts = payload.get("timestamp")
    try:
        epoch = int(float(raw_ts))
    except (TypeError, ValueError) as exc:
        raise UpstreamError(
            "Twelve Data price payload has no valid timestamp.",
            details={"provider": provider, "field": "timestamp"},
        ) from exc
    price = _to_float(payload.get("price"))
    if price is None:
        raise UpstreamError(
            "Twelve Data price payload has no valid price.",
            details={"provider": provider, "field": "price"},
        )
    extensions: dict[str, Any] = {}
    for field in _PRICE_EXTENSIONS_FIELDS:
        raw = payload.get(field)
        if raw is None:
            continue
        value = str(raw).strip()
        if value:
            extensions[field] = value
    return Quote(
        timestamp=datetime.fromtimestamp(epoch, tz=timezone.utc),
        last_price=price,
        volume=None,  # WS price 事件无 volume（探针实证）
        amount=None,  # 上游无成交额字段
        provider=provider,
        received_at=received_at,
        extensions=extensions or None,
    )
