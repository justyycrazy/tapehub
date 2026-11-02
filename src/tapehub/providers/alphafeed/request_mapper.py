"""AlphaFeed 请求映射（RequestMapper）：标准请求 → af.klines.get 参数。

接口签名均已按官方文档核实（https://docs.alphafeed.org/zh-hans/）：

- ``af.klines.get(symbol, period=..., count=..., adjust=..., start_time=..., end_time=...)``；
- symbol 格式 ``代码.市场后缀``，示例 600519.SH / 000001.SZ / 920047.BJ（US/HK 暂不声明）；
- period 枚举 1m/5m/15m/30m/60m/1d/1w/1M/1Q/1Y——**无 10m**（1Q 无 ISO 对应，不声明）；
- adjust 枚举 none/forward/backward/forward_additive/backward_additive；
- start_time/end_time 为毫秒时间戳（含端点）；count 上限 10000。

原则：所有相关参数显式传递，不依赖上游默认值
（注意 SDK/REST 的 period 默认 1d、adjust 默认 forward，均与 tapehub
的声明默认值无关，必须显式传参）。
"""

from typing import Any

from tapehub.domain.errors import UnsupportedCapabilityError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import Adjust, BarQuery

# 单次请求上限（官方文档：count 最大 10000）
UPSTREAM_MAX_COUNT = 10000

_PERIOD_CODES: dict[Interval, str] = {
    Interval.parse("PT1M"): "1m",
    Interval.parse("PT5M"): "5m",
    Interval.parse("PT15M"): "15m",
    Interval.parse("PT30M"): "30m",
    Interval.parse("PT1H"): "60m",
    Interval.parse("P1D"): "1d",
    Interval.parse("P1W"): "1w",
    Interval.parse("P1M"): "1M",
    Interval.parse("P1Y"): "1Y",
}

_ADJUST_CODES = {Adjust.NONE: "none", Adjust.QFQ: "forward", Adjust.HFQ: "backward"}

# 已核实的市场后缀（第一版仅声明 A 股交易所）
_EXCHANGE_SUFFIXES = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}


def symbol_code(query: BarQuery) -> str:
    """标准标的 → AlphaFeed 代码，如 CN-SSE-600519 → 600519.SH。"""
    suffix = _EXCHANGE_SUFFIXES.get(query.instrument.exchange)
    if suffix is None:
        raise UnsupportedCapabilityError(
            f"AlphaFeed does not support exchange {query.instrument.exchange!r}.",
            details={
                "provider": "alphafeed",
                "instrument": query.instrument.code,
            },
        )
    return f"{query.instrument.symbol}.{suffix}"


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


def _to_millis(value: object) -> int:
    return int(value.timestamp() * 1000)  # type: ignore[union-attr]


def build_request(query: BarQuery) -> dict[str, Any]:
    """构造 af.klines.get 的关键字参数；所有参数显式传递。"""
    return {
        "symbol": symbol_code(query),
        "period": period_code(query.interval),
        "adjust": adjust_code(query.adjust),
        "start_time": _to_millis(query.start),
        "end_time": _to_millis(query.end),
        "count": UPSTREAM_MAX_COUNT,
    }


__all__ = ["UPSTREAM_MAX_COUNT", "adjust_code", "build_request", "period_code", "symbol_code"]
