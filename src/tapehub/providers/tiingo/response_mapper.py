"""Tiingo 响应映射（ResponseMapper）：REST bars 行 / WS 快照数据 → 统一 Bar / Quote 模型。

bars（REST，官方文档 crypto/forex 页核实）：

- ``/tiingo/crypto/prices`` 返回扁平数组，元素字段：``date``（ISO 8601 带
  Z 后缀，官方口径 UTC）、``open/high/low/close``、``volume``（base 币计）、
  ``volumeNotional``/``tradesDone``/``exchange``/``baseCurrency``/
  ``quoteCurrency`` 等元数据；
- ``/tiingo/fx/<ticker>/prices`` 元素字段：``date``、``ticker``、
  ``open/high/low/close``——文档仅列 OHLC，无 volume（标准 volume 为 null，
  不用 0 冒充）；
- ``date`` 解析为 ISO aware datetime；裸时间（无时区后缀）按 UTC 解释
  （加密官方口径 UTC；外汇墙钟口径未核实，待探针实证后修正），统一
  北京时间归一化由领域层 BarBatch 完成；
- 缺失值与 NaN 统一映射为 null；amount 上游无成交额字段恒 null。

quote（WS 快照数据元素，官方文档 websockets/crypto|forex 页字段表）：

- TOP（top-of-book）元素字段：``ticker``/``date``/``exchange``/``bidSize``/
  ``bidPrice``/``askSize``/``askPrice``/``midPrice``；Trade 元素字段：
  ``lastSize``/``lastPrice``；
- ``last_price`` 取 ``lastPrice``，缺失回退 ``midPrice``（文档：bid/ask
  均非空时 mid=(bidPrice+askPrice)/2）；两者均缺失/不可解析属上游契约
  违背，抛 UPSTREAM_ERROR（不伪造）；
- ``bid_price1``/``ask_price1``/``bid_volume1``/``ask_volume1`` 防御映射
  （文档核实字段，缺失 → null）；WS 消息形态与 ``lastSize`` 语义
  （单笔量 vs 累计量）未经探针实证，标准 ``volume`` 恒 null、fields
  声明仅 timestamp + last_price（探针实证后扩容）；
- ``date`` 缺失/不可解析属上游契约违背，抛 UPSTREAM_ERROR；
- extensions 白名单：``exchange``（缺失不透出）。
"""

import math
from datetime import datetime
from typing import Any

from tapehub.domain.errors import UpstreamError
from tapehub.domain.models import AssetType, Bar, Quote
from tapehub.providers.tiingo.request_mapper import BOUNDARY_ZONE

# WS 快照 extensions 白名单：仅透出文档核实的字符串元数据字段（缺失不透出）
_WS_EXTENSIONS_FIELDS = ("exchange",)


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
    """上游 ``date``（ISO 8601）→ aware datetime；裸时间按 UTC 解释。"""
    text = str(value).strip().replace("T", " ")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=BOUNDARY_ZONE)
    return parsed


def to_standard_bars(rows: list[dict[str, Any]], asset_type: AssetType) -> list[Bar]:
    """上游行列表 → 标准 Bar 列表（保持上游顺序，由分页组件统一升序排序）。"""
    del asset_type  # 两资产类型同构解析；差异仅在 volume 键是否出现
    bars: list[Bar] = []
    for row in rows:
        bars.append(
            Bar(
                timestamp=_to_timestamp(row.get("date")),
                open=_to_float(row.get("open")),
                high=_to_float(row.get("high")),
                low=_to_float(row.get("low")),
                close=_to_float(row.get("close")),
                # crypto volume 有值（base 币计）；forex 文档无该字段 → null
                volume=_to_float(row.get("volume")),
                amount=None,  # 上游无成交额字段
            )
        )
    return bars


def ws_price_to_quote(
    payload: Any, *, provider: str, received_at: datetime
) -> Quote:
    """WS 快照数据元素 → 标准 Quote（M2 快照与 M3 流共用，字段口径见模块 docstring）。

    - ``date``（ISO）缺失/不可解析、``lastPrice``/``midPrice`` 均缺失属
      上游契约违背，抛 ``UPSTREAM_ERROR``（不伪造时间或价格）；
    - 五档盘口防御映射（缺失 → null）；标准 ``volume``/``amount`` 恒 null
      （lastSize 为单笔成交量的口径未实证，不冒充累计成交量）。
    """
    if not isinstance(payload, dict):
        raise UpstreamError(
            "Tiingo quote payload is not a JSON object.",
            details={"provider": provider},
        )
    raw_date = payload.get("date")
    try:
        timestamp = _to_timestamp(raw_date) if raw_date is not None else None
    except (TypeError, ValueError) as exc:
        raise UpstreamError(
            "Tiingo quote payload has no valid date.",
            details={"provider": provider, "field": "date"},
        ) from exc
    if timestamp is None:
        raise UpstreamError(
            "Tiingo quote payload has no valid date.",
            details={"provider": provider, "field": "date"},
        )
    last_price = _to_float(payload.get("lastPrice"))
    if last_price is None:
        last_price = _to_float(payload.get("midPrice"))
    if last_price is None:
        raise UpstreamError(
            "Tiingo quote payload has no valid price.",
            details={"provider": provider, "field": "lastPrice"},
        )
    extensions: dict[str, Any] = {}
    for field in _WS_EXTENSIONS_FIELDS:
        raw = payload.get(field)
        if raw is None:
            continue
        value = str(raw).strip()
        if value:
            extensions[field] = value
    return Quote(
        timestamp=timestamp,
        last_price=last_price,
        bid_price1=_to_float(payload.get("bidPrice")),  # 防御映射，缺失 → null
        bid_volume1=_to_float(payload.get("bidSize")),
        ask_price1=_to_float(payload.get("askPrice")),
        ask_volume1=_to_float(payload.get("askSize")),
        volume=None,
        amount=None,
        provider=provider,
        received_at=received_at,
        extensions=extensions or None,
    )
