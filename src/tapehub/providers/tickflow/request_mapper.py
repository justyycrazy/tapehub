"""TickFlow 请求映射（RequestMapper）：标准请求 → tf.klines.get / tf.quotes.get 参数。

接口签名均已按官方文档核实（https://docs.tickflow.org/zh-hans/）：

- ``tf.klines.get(symbol, period=..., count=..., adjust=..., start_time=..., end_time=...)``；
- ``tf.quotes.get(symbols=...)``（快照无 period/adjust/时间范围维度）；
- symbol 格式 ``代码.市场后缀``，后缀 SH / SZ / BJ / HK / US
  （A 股 SSE/SZSE/BSE，港股 HKEX，美股 NYSE/NASDAQ/CBOE——exchange 段仅作
  标准代码规范，上游统一 US 后缀）；
- period 枚举 1m/5m/10m/15m/30m/60m/1d/1w/1M/1Q/1Y（1Q 无 ISO 对应不声明；
  1Y 超出项目最大到月线的周期范围，不声明）；
- adjust 枚举 forward/forward_additive/backward/backward_additive/none；
- start_time/end_time 为毫秒时间戳；count 默认 100、最大 10000。

原则：所有相关参数显式传递，不依赖上游默认值
（注意 SDK 的 adjust 默认 forward，与 tapehub 的显式传参约定无关）。
"""

from typing import Any

from tapehub.domain.errors import UnsupportedCapabilityError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import Adjust, BarQuery, Instrument, QuoteQuery

# 单次请求上限（官方文档：单次单标的最多 10000 根）
UPSTREAM_MAX_COUNT = 10000

_PERIOD_CODES: dict[Interval, str] = {
    Interval.parse("PT1M"): "1m",
    Interval.parse("PT5M"): "5m",
    Interval.parse("PT10M"): "10m",
    Interval.parse("PT15M"): "15m",
    Interval.parse("PT30M"): "30m",
    Interval.parse("PT1H"): "60m",
    Interval.parse("P1D"): "1d",
    Interval.parse("P1W"): "1w",
    Interval.parse("P1M"): "1M",
}

_ADJUST_CODES = {Adjust.NONE: "none", Adjust.QFQ: "forward", Adjust.HFQ: "backward"}

# 已核实的市场后缀：A 股 / 港股 / 美股（美股 exchange 段仅作代码规范）
_EXCHANGE_SUFFIXES = {
    "SSE": "SH",
    "SZSE": "SZ",
    "BSE": "BJ",
    "HKEX": "HK",
    "NYSE": "US",
    "NASDAQ": "US",
    "CBOE": "US",
}


def symbol_code(instrument: Instrument) -> str:
    """标准标的 → TickFlow 代码，如 CN-SSE-600000 → 600000.SH、
    HK-HKEX-00700 → 00700.HK、US-NASDAQ-AAPL → AAPL.US。"""
    suffix = _EXCHANGE_SUFFIXES.get(instrument.exchange)
    if suffix is None:
        raise UnsupportedCapabilityError(
            f"TickFlow does not support exchange {instrument.exchange!r}.",
            details={
                "provider": "tickflow",
                "instrument": instrument.code,
            },
        )
    return f"{instrument.symbol}.{suffix}"


def period_code(interval: Interval) -> str:
    period = _PERIOD_CODES.get(interval)
    if period is None:
        raise UnsupportedCapabilityError(
            f"TickFlow does not support interval {interval.code}.",
            details={"interval": interval.code},
        )
    return period


def adjust_code(adjust: Adjust) -> str:
    """qfq→forward（比例前复权），hfq→backward，none→none；始终显式传参。"""
    return _ADJUST_CODES[adjust]


def quote_symbols(query: QuoteQuery) -> list[str]:
    """快照查询 → tf.quotes.get 的 symbols 列表（单标的快照，单元素列表）。"""
    return [symbol_code(query.instrument)]


def _to_millis(value: object) -> int:
    return int(value.timestamp() * 1000)  # type: ignore[union-attr]


def build_request(query: BarQuery) -> dict[str, Any]:
    """构造 tf.klines.get 的关键字参数；所有参数显式传递。"""
    return {
        "symbol": symbol_code(query.instrument),
        "period": period_code(query.interval),
        "adjust": adjust_code(query.adjust),
        "start_time": _to_millis(query.start),
        "end_time": _to_millis(query.end),
        "count": UPSTREAM_MAX_COUNT,
    }


def build_quote_request(query: QuoteQuery) -> dict[str, Any]:
    """构造 tf.quotes.get 的关键字参数；快照只有 symbols 维度。"""
    return {"symbols": quote_symbols(query)}


__all__ = [
    "UPSTREAM_MAX_COUNT",
    "adjust_code",
    "build_quote_request",
    "build_request",
    "period_code",
    "quote_symbols",
    "symbol_code",
]
