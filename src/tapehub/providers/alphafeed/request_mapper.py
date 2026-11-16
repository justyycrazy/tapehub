"""AlphaFeed 请求映射（RequestMapper）：标准请求 → af.klines.get 参数。

映射规则与上游行为定案见 docs/providers/alphafeed.md（唯一出处，本文不复述）。
官方文档：https://docs.alphafeed.org/zh-hans/。
"""

from datetime import datetime
from typing import Any

from tapehub.domain.errors import UnsupportedCapabilityError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import Adjust, BarQuery, Instrument, QuoteQuery
from tapehub.domain.timeutil import datetime_to_epoch

# 单次请求上限（官方文档：count 最大 10000）
UPSTREAM_MAX_COUNT = 10000

# 仅声明日/周/月（分钟族无套餐权限、1Q/1Y 超出项目周期上界，见 docs/providers/alphafeed.md）
_PERIOD_CODES: dict[Interval, str] = {
    Interval.parse("P1D"): "1d",
    Interval.parse("P1W"): "1w",
    Interval.parse("P1M"): "1M",
}

_ADJUST_CODES = {Adjust.NONE: "none", Adjust.QFQ: "forward", Adjust.HFQ: "backward"}

# 市场级后缀（HK/US 后缀与 exchange 段无关，市场段即后缀）；CN 按交易所
# 消歧（SH/SZ/BJ）。market/exchange 一致性由域层词汇表校验，此处只按
# 接入范围收窄（docs/providers/alphafeed.md「标的代码映射」）。
_MARKET_SUFFIXES = {"HK": "HK", "US": "US"}
_CN_EXCHANGE_SUFFIXES = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}


def symbol_code(instrument: Instrument) -> str:
    """标准标的 → AlphaFeed 代码：US/HK 按市场段、CN 按交易所段消歧
    （CN-SSE-600519 → 600519.SH、US-NASDAQ-AAPL → AAPL.US）。"""
    if instrument.market == "CN":
        suffix = _CN_EXCHANGE_SUFFIXES.get(instrument.exchange)
        if suffix is None:
            raise UnsupportedCapabilityError(
                f"AlphaFeed does not support CN exchange {instrument.exchange!r}.",
                details={
                    "provider": "alphafeed",
                    "instrument": instrument.code,
                },
            )
    else:
        suffix = _MARKET_SUFFIXES.get(instrument.market)
        if suffix is None:
            raise UnsupportedCapabilityError(
                f"AlphaFeed does not support market {instrument.market!r}.",
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

    tapehub 快照为单标的语义：单元素 symbols 列表。
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
