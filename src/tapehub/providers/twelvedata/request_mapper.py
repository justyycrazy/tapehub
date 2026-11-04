"""Twelve Data 请求映射（RequestMapper）：标准请求 → td.time_series 参数。

接口签名均已按官方文档核实（https://twelvedata.com/docs/market-data/time-series）：

- ``td.time_series(symbol, interval, outputsize, start_date, end_date).as_json()``；
- symbol 为斜杠分隔货币对：外汇 ``EUR/USD``（ISO 4217）、加密 ``BTC/USD``；
- interval 枚举 1min/5min/15min/30min/45min/1h/2h/4h/8h/1day/1week/1month，
  本适配器声明全部 12 个周期（45min/2h/4h/8h 经探针实测后补充声明）；
- datetime 墙钟口径按资产类型区分（2026-11-04 实测）：外汇按
  Australia/Sydney 解释，加密货币按 UTC 解释；start_date/end_date 无时区
  后缀，格式 YYYY-MM-DD 或 YYYY-MM-DDTHH:MM:SS，故北京时间边界须先
  转换为对应口径的墙钟再传递；
- outputsize 范围 1~5000：显式传最大值 5000（不依赖上游默认 30）。
"""

from datetime import datetime
from typing import Any

from tapehub.domain.errors import UnsupportedCapabilityError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import AssetType, BarQuery
from tapehub.providers.twelvedata.response_mapper import display_zone

# 单次请求上限（官方文档：outputsize 范围 1~5000）
UPSTREAM_MAX_BARS = 5000

# 区间无数据指纹（2026-11-04 数据探针实测核实，报告 20261104T065529Z）：
# 查询区间内无数据时上游返回 400 BadRequestError，报文含此句，而非空列表
UPSTREAM_NO_DATA_FINGERPRINT = "No data is available on the specified dates"

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


def _boundary_code(value: datetime, zone: object) -> str:
    """边界（北京时间，域层保证带时区）→ 上游口径墙钟 YYYY-MM-DDTHH:MM:SS。"""
    return value.astimezone(zone).strftime("%Y-%m-%dT%H:%M:%S")  # type: ignore[attr-defined]


def build_request(query: BarQuery) -> dict[str, Any]:
    """构造 td.time_series 的关键字参数；所有相关参数显式传递。

    不依赖上游默认值：interval 默认参数缺省时上游行为未知、
    outputsize 默认 30，均显式指定；边界从北京时间转换为上游
    按资产类型解释的墙钟口径（外汇 Sydney、加密货币 UTC）。
    """
    zone = display_zone(query.asset_type)
    return {
        "symbol": td_symbol(query),
        "interval": interval_code(query.interval),
        "outputsize": UPSTREAM_MAX_BARS,
        "start_date": _boundary_code(query.start, zone),
        "end_date": _boundary_code(query.end, zone),
    }


__all__ = [
    "UPSTREAM_MAX_BARS",
    "UPSTREAM_NO_DATA_FINGERPRINT",
    "build_request",
    "interval_code",
    "td_symbol",
]
