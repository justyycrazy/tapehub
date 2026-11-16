"""Tiingo 响应映射（ResponseMapper）：REST bars 行 / WS 快照数据 → 统一 Bar / Quote 模型。

响应结构与字段口径见 docs/providers/tiingo.md（唯一出处，本文不复述）。

bars 响应按形状统一拍平（crypto 嵌套 / fx 扁平防御兼容），裸时间按 UTC
解释、统一北京时间归一化由领域层 BarBatch 完成；``date`` 缺失/不可解析
属上游契约违背，抛 ``UPSTREAM_ERROR``。quote ``last_price`` 取
``lastPrice``、缺失回退 ``midPrice``，两者均缺失属契约违背，抛
UPSTREAM_ERROR（不伪造）；缺失值与 NaN 统一映射为 null。
"""

import math
import time
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
    """上游 ``date``（ISO 8601）→ aware datetime；裸时间按 UTC 解释。

    缺失/不可解析属上游契约违背（bar 无时间戳无法标准化），抛
    ``UPSTREAM_ERROR``——不伪造时间，也不以裸 ``ValueError`` 穿透。
    """
    if value is None:
        raise UpstreamError(
            "Tiingo bar row has no valid date.",
            details={"field": "date"},
        )
    text = str(value).strip().replace("T", " ")
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError) as exc:
        raise UpstreamError(
            "Tiingo bar row has no valid date.",
            details={"field": "date"},
        ) from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=BOUNDARY_ZONE)
    return parsed


def to_standard_bars(rows: list[dict[str, Any]], asset_type: AssetType) -> list[Bar]:
    """上游行列表 → 标准 Bar 列表（保持上游顺序，由分页组件统一升序排序）。

    两端点响应形状不同：crypto 为按 ticker 分组的嵌套结构（``priceData``
    为 bar 行数组）、fx 为扁平行；按形状统一拍平（嵌套/扁平防御兼容），
    bar 字段名两端点一致。
    """
    del asset_type  # 两资产类型同构解析；差异仅在 volume 键是否出现
    bars: list[Bar] = []
    for row in rows:
        nested = row.get("priceData")
        items = nested if isinstance(nested, list) else [row]
        for item in items:
            bars.append(
                Bar(
                    timestamp=_to_timestamp(item.get("date")),
                    open=_to_float(item.get("open")),
                    high=_to_float(item.get("high")),
                    low=_to_float(item.get("low")),
                    close=_to_float(item.get("close")),
                    # crypto volume 有值（base 币计）；forex 文档无该字段 → null
                    volume=_to_float(item.get("volume")),
                    amount=None,  # 上游无成交额字段
                )
            )
    return bars


def ws_element_to_snapshot(element: Any) -> dict[str, Any]:
    """WS data 元素 → 标准化快照 dict（runtime 缓存/监听/映射统一消费）。

    data 元素为位置数组：

    - fx Q（8 元素）：``["Q", ticker, date, bidSize, bidPrice, midPrice,
      askSize, askPrice]``（无 exchange；mid=(bid+ask)/2）；
    - crypto Q（9 元素）：同上，第 4 位多 ``exchange``；
    - T 元素同理（含 exchange 共 6 位、不含共 5 位：``["T", ticker, date,
      (exchange,) lastSize, lastPrice]``）；
    - 对象形态透传（防御：上游若回归文档形态按字段名读取）；
    - 形状不足/未知类型返回空 dict（runtime 按 ticker 缺失丢弃）。
    """
    if isinstance(element, dict):
        return element
    if not isinstance(element, list) or len(element) < 3:
        return {}
    kind, ticker, date = element[0], element[1], element[2]
    if kind == "Q" and len(element) >= 9:
        exchange, bid_size, bid_price, mid_price, ask_size, ask_price = element[3:9]
        return {
            "ticker": ticker,
            "date": date,
            "exchange": exchange,
            "bidSize": bid_size,
            "bidPrice": bid_price,
            "midPrice": mid_price,
            "askSize": ask_size,
            "askPrice": ask_price,
        }
    if kind == "Q" and len(element) >= 8:
        bid_size, bid_price, mid_price, ask_size, ask_price = element[3:8]
        return {
            "ticker": ticker,
            "date": date,
            "bidSize": bid_size,
            "bidPrice": bid_price,
            "midPrice": mid_price,
            "askSize": ask_size,
            "askPrice": ask_price,
        }
    if kind == "T" and len(element) >= 6:
        exchange, last_size, last_price = element[3:6]
        return {
            "ticker": ticker,
            "date": date,
            "exchange": exchange,
            "lastSize": last_size,
            "lastPrice": last_price,
        }
    if kind == "T" and len(element) >= 5:
        last_size, last_price = element[3:5]
        return {
            "ticker": ticker,
            "date": date,
            "lastSize": last_size,
            "lastPrice": last_price,
        }
    return {}


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
    # 快照新鲜度：Runtime 打的到达时刻（`_arrival`，墙钟秒）→ 距今毫秒。
    # 断连/静默窗口内命中缓存的快照可据此判陈旧；无戳（构造直造 payload）不透出
    arrival = payload.get("_arrival")
    if isinstance(arrival, (int, float)) and not isinstance(arrival, bool):
        extensions["snapshot_age_ms"] = max(0, round((time.time() - arrival) * 1000))
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
