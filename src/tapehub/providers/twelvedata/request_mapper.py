"""Twelve Data 请求映射（RequestMapper）：标准请求 → td.time_series / WS 订阅参数。

映射规则与上游行为定案见 docs/providers/twelvedata.md（唯一出处，本文不复述）。
官方文档：https://twelvedata.com/。
"""

from datetime import datetime
from typing import Any

from tapehub.domain.errors import UnsupportedCapabilityError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import Adjust, AssetType, BarQuery, QuoteQuery
from tapehub.providers.twelvedata.response_mapper import display_zone

# 单次请求上限（官方文档：outputsize 范围 1~5000）
UPSTREAM_MAX_BARS = 5000

# 区间无数据指纹：查询区间内无数据时上游返回 400 BadRequestError，
# 报文含此句，而非空列表
UPSTREAM_NO_DATA_FINGERPRINT = "No data is available on the specified dates"

# WS 端点（实时快照与快照流共用；API key 经查询参数注入，绝不写日志）
WS_ENDPOINT = "wss://ws.twelvedata.com/v1/quotes/price"

_INTERVAL_CODES: dict[Interval, str] = {
    Interval.parse("PT1M"): "1min",
    Interval.parse("PT5M"): "5min",
    Interval.parse("PT15M"): "15min",
    Interval.parse("PT30M"): "30min",
    Interval.parse("PT45M"): "45min",
    Interval.parse("PT1H"): "1h",
    Interval.parse("PT2H"): "2h",
    Interval.parse("PT4H"): "4h",
    Interval.parse("PT8H"): "8h",
    Interval.parse("P1D"): "1day",
    Interval.parse("P1W"): "1week",
    Interval.parse("P1M"): "1month",
}

# 统一复权 → 上游 adjust 取值（仅美股资产类型使用；口径见模块 docstring）
_ADJUST_CODES: dict[Adjust, str] = {
    Adjust.NONE: "none",
    Adjust.QFQ: "all",  # 拆股+分红前复权，与国内 qfq 口径一致
}

# 标准代码中 exchange 段的约定命名空间（美股为白名单集合：
# NYSE/NASDAQ/CBOE；OTC 粉单与 IEX 不接入，依据见 docs/providers/twelvedata.md）
_ASSET_EXCHANGES: dict[AssetType, frozenset[str]] = {
    AssetType.FOREX: frozenset({"OTC"}),
    AssetType.CRYPTO: frozenset({"CRYPTO"}),
    AssetType.STOCK: frozenset({"NYSE", "NASDAQ", "CBOE"}),
    AssetType.ETF: frozenset({"NYSE", "NASDAQ", "CBOE"}),
}

# 需要显式传 exchange 与 adj 的资产类型（美股；货币对不传）
_US_EQUITY_TYPES = frozenset({AssetType.STOCK, AssetType.ETF})


def td_symbol(query: BarQuery | QuoteQuery) -> str:
    """标准标的 → Twelve Data 上游 symbol（如 GLOBAL-OTC-EUR/USD → EUR/USD）。"""
    allowed = _ASSET_EXCHANGES.get(query.asset_type)
    if allowed is None:
        raise UnsupportedCapabilityError(
            f"Twelve Data does not support asset type {query.asset_type.value}.",
            details={
                "provider": "twelvedata",
                "instrument": query.instrument.code,
                "asset_type": query.asset_type.value,
            },
        )
    if query.instrument.exchange not in allowed:
        expected = "/".join(sorted(allowed))
        raise UnsupportedCapabilityError(
            f"Twelve Data expects exchange segment to be one of [{expected}] "
            f"for asset type {query.asset_type.value}, "
            f"got {query.instrument.exchange!r}.",
            details={
                "provider": "twelvedata",
                "instrument": query.instrument.code,
            },
        )
    return query.instrument.symbol


def interval_code(interval: Interval) -> str:
    code = _INTERVAL_CODES.get(interval)
    if code is None:
        raise UnsupportedCapabilityError(
            f"Twelve Data does not support interval {interval.code}.",
            details={"interval": interval.code},
        )
    return code


def adjust_code(adjust: Adjust) -> str:
    """统一复权 → 上游 adjust 取值（hfq 上游无等价物，能力目录即不声明）。"""
    code = _ADJUST_CODES.get(adjust)
    if code is None:
        raise UnsupportedCapabilityError(
            f"Twelve Data does not support adjust {adjust.value}.",
            details={"adjust": adjust.value},
        )
    return code


def _boundary_code(value: datetime, zone: object) -> str:
    """边界（北京时间，域层保证带时区）→ 上游口径墙钟 YYYY-MM-DDTHH:MM:SS。"""
    return value.astimezone(zone).strftime("%Y-%m-%dT%H:%M:%S")  # type: ignore[attr-defined]


def build_request(query: BarQuery) -> dict[str, Any]:
    """构造 td.time_series 的关键字参数；所有相关参数显式传递。

    不依赖上游默认值：interval 默认参数缺省时上游行为未知、
    outputsize 默认 30、美股 adjust 缺省时上游默认 splits（仅拆股），
    均显式指定；边界从北京时间转换为上游按资产类型解释的墙钟口径
    （外汇 Sydney、加密货币 UTC、美股 New York）。美股额外显式传
    exchange（同名 ticker 消歧）与 adjust。
    """
    zone = display_zone(query.asset_type)
    kwargs: dict[str, Any] = {
        "symbol": td_symbol(query),
        "interval": interval_code(query.interval),
        "outputsize": UPSTREAM_MAX_BARS,
        "start_date": _boundary_code(query.start, zone),
        "end_date": _boundary_code(query.end, zone),
    }
    if query.asset_type in _US_EQUITY_TYPES:
        kwargs["exchange"] = query.instrument.exchange
        kwargs["adjust"] = adjust_code(query.adjust)
    return kwargs


__all__ = [
    "UPSTREAM_MAX_BARS",
    "UPSTREAM_NO_DATA_FINGERPRINT",
    "WS_ENDPOINT",
    "adjust_code",
    "build_request",
    "interval_code",
    "td_symbol",
]
