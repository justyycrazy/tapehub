"""Twelve Data 响应映射（ResponseMapper）：上游 as_json()/WS price → 统一 Bar / Quote 模型。

字段结构、datetime 墙钟口径与 extensions 白名单见 docs/providers/twelvedata.md
（唯一出处，本文不复述）。官方文档：https://twelvedata.com/exchanges/stocks。

bars 的 datetime 为墙钟时间（无时区后缀），解析为对应显示时区的 aware
datetime，统一北京时间归一化由领域层完成；缺失值与 NaN 统一映射为 null。
quote（WS price 事件）``timestamp`` 为分钟桶 Unix 秒、按 UTC 解释；
``price`` → ``last_price``，缺失/不可解析属上游契约违背，抛 UPSTREAM_ERROR
（不伪造）；标准字段只有 ``timestamp`` + ``last_price``（与能力目录 fields
声明一致）。
"""

import math
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from tapehub.domain.errors import UpstreamError
from tapehub.domain.models import AssetType, Bar, Quote
from tapehub.domain.timeutil import epoch_to_datetime

# 外汇 datetime 的显示时区（官方文档：All times are displayed in the
# Australia/Sydney timezone；ZoneInfo 自动处理 AEDT/AEST 夏令时切换）
SYDNEY_TIMEZONE = ZoneInfo("Australia/Sydney")

# 美股 datetime 的显示时区（官方文档 https://twelvedata.com/exchanges/stocks：
# All times are displayed in the America/New_York timezone；ZoneInfo 自动
# 处理 EST/EDT 夏令时切换）
NEW_YORK_TIMEZONE = ZoneInfo("America/New_York")

# WS price 事件 extensions 白名单：仅透出实测稳定出现的字符串元数据字段；
# 数值字段经 _to_float 转换，None 不透出
_PRICE_EXTENSIONS_FIELDS = ("currency_base", "currency_quote", "exchange", "type")


def _to_float(value: Any) -> float | None:
    """转换为 float；None/NaN/±inf/不可转换值返回 None，不用 0 冒充。

    ±inf 与 NaN 同语义置 null：JSON 路由经 pydantic 序列化会把 inf 静默
    置 null（未声明口径），SSE 帧经 ``json.dumps`` 则输出非法 JSON token
    （``Infinity``，严格解析器拒收）——非有限值不进入统一口径。
    """
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def display_zone(asset_type: AssetType) -> timezone | ZoneInfo:
    """上游 datetime 墙钟的显示时区：外汇 Sydney、加密货币 UTC、美股 New York。"""
    if asset_type is AssetType.FOREX:
        return SYDNEY_TIMEZONE
    if asset_type is AssetType.CRYPTO:
        return timezone.utc
    if asset_type in (AssetType.STOCK, AssetType.ETF):
        return NEW_YORK_TIMEZONE
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
    # 快照新鲜度：Runtime 打的到达时刻（`_arrival`，墙钟秒）→ 距今毫秒。
    # 断连/静默窗口内命中缓存的快照可据此判陈旧；无戳（构造直造 payload）不透出
    arrival = payload.get("_arrival")
    if isinstance(arrival, (int, float)) and not isinstance(arrival, bool):
        extensions["snapshot_age_ms"] = max(0, round((time.time() - arrival) * 1000))
    return Quote(
        timestamp=epoch_to_datetime(epoch * 1_000_000_000, timezone.utc),
        last_price=price,
        volume=None,  # WS price 事件无 volume
        amount=None,  # 上游无成交额字段
        provider=provider,
        received_at=received_at,
        extensions=extensions or None,
    )
