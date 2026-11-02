"""Twelve Data 请求映射（RequestMapper）：标准请求 → td.time_series 参数。

接口签名均已按官方文档核实（https://twelvedata.com/docs/market-data/time-series）：

- ``td.time_series(symbol, interval, outputsize, start_date, end_date).as_json()``；
- symbol 为斜杠分隔货币对：外汇 ``EUR/USD``（ISO 4217）、加密 ``BTC/USD``；
- interval 枚举 1min/5min/15min/30min/45min/1h/2h/4h/8h/1day/1week/1month，
  本适配器声明 PT1M~PT1H、P1D/P1W/P1M；
- 外汇/加密货币的 datetime 按 UTC（官方文档明确）；
  start_date/end_date 格式 YYYY-MM-DD 或 YYYY-MM-DDTHH:MM:SS；
- outputsize 范围 1~5000：显式传最大值 5000（不依赖上游默认 30）。
"""

from typing import Any

from tapehub.domain.errors import UnsupportedCapabilityError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import AssetType, BarQuery

# 单次请求上限（官方文档：outputsize 范围 1~5000）
UPSTREAM_MAX_BARS = 5000

_INTERVAL_CODES: dict[Interval, str] = {
    Interval.parse("PT1M"): "1min",
    Interval.parse("PT5M"): "5min",
    Interval.parse("PT15M"): "15min",
    Interval.parse("PT30M"): "30min",
    Interval.parse("PT1H"): "1h",
    Interval.parse("P1D"): "1day",
    Interval.parse("P1W"): "1week",
    Interval.parse("P1M"): "1month",
}

# 标准代码中 exchange 段的约定命名空间
_ASSET_EXCHANGE = {
    AssetType.FOREX: "OTC",
    AssetType.CRYPTO: "CRYPTO",
}


def td_symbol(query: BarQuery) -> str:
    """标准标的 → Twelve Data 货币对（如 GLOBAL-OTC-EUR/USD → EUR/USD）。"""
    expected_exchange = _ASSET_EXCHANGE.get(query.asset_type)
    if expected_exchange is None:
        raise UnsupportedCapabilityError(
            f"Twelve Data does not support asset type {query.asset_type.value}.",
            details={
                "provider": "twelvedata",
                "instrument": query.instrument.code,
                "asset_type": query.asset_type.value,
            },
        )
    if query.instrument.exchange != expected_exchange:
        raise UnsupportedCapabilityError(
            f"Twelve Data expects exchange segment {expected_exchange!r} "
            f"for asset type {query.asset_type.value}, got {query.instrument.exchange!r}.",
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


def _boundary_code(value: object) -> str:
    """边界（UTC，域层保证带时区）→ YYYY-MM-DDTHH:MM:SS。"""
    return value.strftime("%Y-%m-%dT%H:%M:%S")  # type: ignore[union-attr]


def build_request(query: BarQuery) -> dict[str, Any]:
    """构造 td.time_series 的关键字参数；所有相关参数显式传递。

    不依赖上游默认值：interval 默认参数缺省时上游行为未知、
    outputsize 默认 30、外汇/加密时间按 UTC，均显式指定。
    """
    return {
        "symbol": td_symbol(query),
        "interval": interval_code(query.interval),
        "outputsize": UPSTREAM_MAX_BARS,
        "start_date": _boundary_code(query.start),
        "end_date": _boundary_code(query.end),
    }


__all__ = ["UPSTREAM_MAX_BARS", "build_request", "interval_code", "td_symbol"]
