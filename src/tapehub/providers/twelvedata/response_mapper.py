"""Twelve Data 响应映射（ResponseMapper）：上游 as_json() → 统一 Bar / Quote 模型。

响应结构已按官方文档核实：

- time-series：values 数组，默认倒序=最新在前。每个元素字段：datetime
  （bar 开盘时间，墙钟时间无时区后缀）、open/high/low/close/volume，
  均为**字符串**类型；
- datetime 的墙钟口径按资产类型区分（2026-11-04 实测 + 官方文档
  https://twelvedata.com/exchanges/physical_currency|digital_currency）：
  外汇按 Australia/Sydney（AEDT/AEST 随夏令时切换），加密货币按 UTC；
  解析为对应时区的 aware datetime，到统一北京时间的归一化由领域层完成；
- quote（REST /quote）：JSON 对象，数值字段为**字符串**类型，timestamp
  / last_quote_at 为 Unix 秒；close → last_price、volume → volume（外汇
  可能缺失 → null）；五档盘口与 amount 上游无源字段恒 null；
  extensions 白名单以 2026-11-05 quote 探针实证为准（报告
  20261105T184934+0800，人工确认回填）；
- 外汇/加密货币通常无 volume 字段：缺失时标准 volume 为 null，
  不用 0 冒充（bars 能力目录 fields 亦不含 volume）；
- 缺失值与 NaN 统一映射为 null。
"""

import math
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from tapehub.domain.errors import UpstreamError
from tapehub.domain.models import AssetType, Bar, Quote

# quote extensions 白名单（2026-11-05 探针实证 + 人工确认，报告
# 20261105T184934+0800）：仅透出文档与实测一致的标量字段；
# fifty_two_week（嵌套对象）与 crypto 专属 rolling_* 不透出；
# currency/mic_code/average_volume 文档列出但实测未返回，不声明
# 数值字段经 _to_float 转换，None 值不透出（tqsdk P5 白名单同款过滤）
_QUOTE_EXTENSIONS_FIELDS = (
    "name",
    "exchange",
    "open",
    "high",
    "low",
    "previous_close",
    "change",
    "percent_change",
    "is_market_open",
)

# 白名单中的数值字段（字符串 → float）
_QUOTE_EXTENSIONS_NUMERIC = frozenset(
    {"open", "high", "low", "previous_close", "change", "percent_change"}
)

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


def _to_epoch(value: Any) -> int | None:
    """Unix 秒字段（int 或数字字符串）→ int；缺失/非法返回 None。"""
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _quote_extensions(payload: dict[str, Any]) -> dict[str, Any]:
    """按白名单提取 extensions；数值字段转 float，None 值一律不透出。"""
    extensions: dict[str, Any] = {}
    for field in _QUOTE_EXTENSIONS_FIELDS:
        raw = payload.get(field)
        if field in _QUOTE_EXTENSIONS_NUMERIC:
            value = _to_float(raw)
        elif field == "is_market_open":
            value = raw if isinstance(raw, bool) else None
        else:  # name / exchange：字符串字段
            value = str(raw).strip() if raw is not None else None
            if value == "":
                value = None
        if value is not None:
            extensions[field] = value
    return extensions


def to_standard_quote(
    payload: Any, *, provider: str, received_at: datetime
) -> Quote:
    """上游 /quote 响应对象 → 标准 Quote（extensions 白名单见模块 docstring）。

    - 行情时间：优先 ``last_quote_at``（最后一根分钟 K 线的 Unix 秒，快照
      新鲜度口径），缺失回退 ``timestamp``（当期 interval bar 开盘时刻）；
      两者皆缺属上游契约违背，抛 UPSTREAM_ERROR（不伪造时间）；Unix 秒
      按 UTC 解释，统一北京时间归一化由领域层 Quote 完成；
    - ``close`` → last_price、``volume`` → volume（2026-11-05 探针实测
      EUR/USD 与 BTC/USD 均未返回 volume；缺失 → null，映射保留防御处理）；
    - 五档盘口与 amount 上游无源字段，恒 null（不得用 0 冒充）。
    """
    if not isinstance(payload, dict):
        raise UpstreamError(
            "Twelve Data quote payload is not a JSON object.",
            details={"provider": provider},
        )
    epoch = _to_epoch(payload.get("last_quote_at"))
    if epoch is None:
        epoch = _to_epoch(payload.get("timestamp"))
    if epoch is None:
        raise UpstreamError(
            "Twelve Data quote payload has no time field.",
            details={
                "provider": provider,
                "fields_checked": ["last_quote_at", "timestamp"],
            },
        )
    return Quote(
        timestamp=datetime.fromtimestamp(epoch, tz=timezone.utc),
        last_price=_to_float(payload.get("close")),
        volume=_to_float(payload.get("volume")),
        amount=None,  # 上游无成交额字段
        provider=provider,
        received_at=received_at,
        extensions=_quote_extensions(payload) or None,
    )
