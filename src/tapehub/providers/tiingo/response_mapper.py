"""Tiingo 响应映射（ResponseMapper）：REST bars 行 / WS 快照数据 → 统一 Bar / Quote 模型。

bars（REST，形状按 2026-11-13 数据探针实证，报告 20261113T163754+0800——
官方文档未如实反映 crypto 端点的嵌套结构）：

- ``/tiingo/crypto/prices`` 返回**按 ticker 分组的嵌套数组**，外层元素字段
  ``ticker/baseCurrency/quoteCurrency/priceData``，``priceData`` 为 bar 行
  数组（``date`` 为 ISO 8601 带 ``+00:00``、``open/high/low/close``、
  ``volume``（base 币计）、``volumeNotional``、``tradesDone``）；
- ``/tiingo/fx/<ticker>/prices`` 返回扁平数组，元素字段：``date``（ISO 带
  Z 后缀）、``ticker``、``open/high/low/close``——无 volume（标准 volume
  为 null，不用 0 冒充）；墙钟口径经探针实证为 UTC（tg-fx-date-shape
  观察 date 全部带 Z 且日内分布吻合）；
- 适配器按形状统一拍平（嵌套/扁平防御兼容）；``date`` 缺失/不可解析属
  上游契约违背，抛 ``UPSTREAM_ERROR``（bar 无时间戳无法标准化）；
- 裸时间（无时区后缀）按 UTC 解释，统一北京时间归一化由领域层 BarBatch
  完成；缺失值与 NaN 统一映射为 null；amount 上游无成交额字段恒 null。

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

    2026-11-13 数据探针实证（报告 20261113T163754+0800）：两个端点响应
    形状不同——``/tiingo/crypto/prices`` 返回**按 ticker 分组的嵌套结构**
    ``{"ticker", "baseCurrency", "quoteCurrency", "priceData": [...]}``（官方
    文档未如实反映）；``/tiingo/fx/<ticker>/prices`` 为扁平行（date 带 Z、
    仅 OHLC）。按形状统一拍平（嵌套/扁平防御兼容），bar 字段名两端点一致
    （date/open/high/low/close[/volume/volumeNotional/tradesDone]）。
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

    2026-11-13 WS 行为探针实证（报告 ws-behavior-20261113T2130+0800，
    官方文档字段表的对象形态**错误**）——data 元素为位置数组：

    - fx Q（8 元素）：``["Q", ticker, date, bidSize, bidPrice, midPrice,
      askSize, askPrice]``（无 exchange；mid=(bid+ask)/2 探针自洽验证）；
    - crypto Q（9 元素）：同上，第 4 位多 ``exchange``；
    - crypto T（6 元素）：``["T", ticker, date, exchange, lastSize,
      lastPrice]``；
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
