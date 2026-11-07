"""AlphaFeed 请求映射（RequestMapper）：标准请求 → af.klines.get 参数。

接口签名均已按官方文档核实（https://docs.alphafeed.org/zh-hans/）：

- ``af.klines.get(symbol, period=..., count=..., adjust=..., start_time=..., end_time=...)``；
- symbol 格式 ``代码.市场后缀``，示例 600519.SH / 000001.SZ / 920047.BJ /
  00700.HK / AAPL.US（美股 NYSE/NASDAQ/CBOE 统一 US 后缀，与 tickflow 口径一致）；
- 上游 period 枚举 1m/5m/15m/30m/60m/1d/1w/1M/1Q/1Y——**无 10m**；
  账户套餐口径仅开通日 K 线权限（1d/1w/1M），分钟族不声明（分钟级需
  单独的「分钟K线权限」，403 由探针/文档口径核实）；1Q 无 ISO 对应，
  1Y 超出项目最大到月线的周期范围，均不声明；
- adjust 枚举 none/forward/backward/forward_additive/backward_additive；
- start_time/end_time 为毫秒时间戳（含端点）；count 上限 10000。

原则：所有相关参数显式传递，不依赖上游默认值
（注意 SDK/REST 的 period 默认 1d、adjust 默认 forward，均与 tapehub
的声明默认值无关，必须显式传参）。
"""

from datetime import datetime
from typing import Any

from tapehub.domain.errors import UnsupportedCapabilityError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import Adjust, BarQuery, Instrument, QuoteQuery
from tapehub.domain.timeutil import datetime_to_epoch

# 单次请求上限（官方文档：count 最大 10000）
UPSTREAM_MAX_COUNT = 10000

# 仅声明日/周/月（分钟族无套餐权限、1Q/1Y 超出项目周期上界，见模块 docstring）
_PERIOD_CODES: dict[Interval, str] = {
    Interval.parse("P1D"): "1d",
    Interval.parse("P1W"): "1w",
    Interval.parse("P1M"): "1M",
}

_ADJUST_CODES = {Adjust.NONE: "none", Adjust.QFQ: "forward", Adjust.HFQ: "backward"}

# 已核实的市场后缀：CN 三交易所 + HK + US（美股多交易所统一 US 后缀）
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
    """标准标的 → AlphaFeed 代码，如 CN-SSE-600519 → 600519.SH。"""
    suffix = _EXCHANGE_SUFFIXES.get(instrument.exchange)
    if suffix is None:
        raise UnsupportedCapabilityError(
            f"AlphaFeed does not support exchange {instrument.exchange!r}.",
            details={
                "provider": "alphafeed",
                "instrument": instrument.code,
            },
        )
    return f"{instrument.symbol}.{suffix}"


def period_code(interval: Interval) -> str:
    period = _PERIOD_CODES.get(interval)
    if period is None:
        raise UnsupportedCapabilityError(
            f"AlphaFeed does not support interval {interval.code}.",
            details={"interval": interval.code},
        )
    return period


def adjust_code(adjust: Adjust) -> str:
    """qfq→forward（前复权），hfq→backward，none→none；始终显式传参。"""
    return _ADJUST_CODES[adjust]


def _to_millis(value: datetime) -> int:
    """aware datetime → 纪元毫秒（纪元算术收口 domain.timeutil，向下取整）。"""
    return datetime_to_epoch(value) // 1_000_000


def build_request(query: BarQuery) -> dict[str, Any]:
    """构造 af.klines.get 的关键字参数；所有参数显式传递。"""
    return {
        "symbol": symbol_code(query.instrument),
        "period": period_code(query.interval),
        "adjust": adjust_code(query.adjust),
        "start_time": _to_millis(query.start),
        "end_time": _to_millis(query.end),
        "count": UPSTREAM_MAX_COUNT,
    }


def build_quote_request(query: QuoteQuery) -> dict[str, Any]:
    """构造 af.quotes.get 的关键字参数（symbols/universes 二选一，keyword-only）。

    tapehub 快照为单标的语义：单元素 symbols 列表；上游 5 标的/次的批量
    容量闲置（套餐限频 10 次/分钟才是约束）。
    """
    return {"symbols": [symbol_code(query.instrument)]}


__all__ = [
    "UPSTREAM_MAX_COUNT",
    "adjust_code",
    "build_quote_request",
    "build_request",
    "period_code",
    "symbol_code",
]
