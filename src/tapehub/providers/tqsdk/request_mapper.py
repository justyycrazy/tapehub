"""TqSdk 请求映射（RequestMapper）：标准请求 → api.get_kline_serial 参数。

接口签名均已按官方文档核实：

- 合约代码（交易所全大写，品种代码遵从交易所大小写约定）：
  单一合约 ``SHFE.cu2607``；主力连续 ``KQ.m@SHFE.cu``；
  指数连续/加权 ``KQ.i@SHFE.bu``；
- ``get_kline_serial(symbol, duration_seconds, data_length)``：
  duration_seconds 为不超过 1 日的任意秒数（P1D=86400）；
  data_length 默认 200，每序列最大 10000（API 参考）；
- 期货无复权概念（adj_type 仅对股票/基金有效），请求不含复权参数。
"""

from typing import Any

from tapehub.domain.errors import UnsupportedCapabilityError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import AssetType, BarQuery

# 单次请求上限（API 参考：每个序列最大支持请求 10000 个数据）
UPSTREAM_MAX_BARS = 10000

_DURATION_SECONDS: dict[Interval, int] = {
    Interval.parse("PT1M"): 60,
    Interval.parse("PT5M"): 300,
    Interval.parse("PT15M"): 900,
    Interval.parse("PT30M"): 1800,
    Interval.parse("PT1H"): 3600,
    Interval.parse("P1D"): 86400,
}

# 持续性合约前缀（官方文档：主力连续/指数连续）
_CONTINUOUS_PREFIX = "KQ.m@"
_WEIGHTED_PREFIX = "KQ.i@"


def kline_symbol(query: BarQuery) -> str:
    """标准标的 → TqSdk 合约代码。"""
    base = f"{query.instrument.exchange}.{query.instrument.symbol}"
    if query.asset_type == AssetType.CONTRACT:
        return base
    if query.asset_type == AssetType.CONTINUOUS:
        return f"{_CONTINUOUS_PREFIX}{base}"
    if query.asset_type == AssetType.WEIGHTED_INDEX:
        return f"{_WEIGHTED_PREFIX}{base}"
    raise UnsupportedCapabilityError(
        f"TqSdk does not support asset type {query.asset_type.value}.",
        details={
            "provider": "tqsdk",
            "instrument": query.instrument.code,
            "asset_type": query.asset_type.value,
        },
    )


def duration_seconds(interval: Interval) -> int:
    """ISO 周期 → K 线周期秒数（仅支持不超过 1 日的周期）。"""
    seconds = _DURATION_SECONDS.get(interval)
    if seconds is None:
        raise UnsupportedCapabilityError(
            f"TqSdk does not support interval {interval.code}.",
            details={"interval": interval.code},
        )
    return seconds


def build_request(query: BarQuery) -> dict[str, Any]:
    """构造 get_kline_serial 的关键字参数；所有相关参数显式传递。

    TqSdk 的序列从“最新一根”往回取 data_length 根，
    不支持按时间范围请求：窗口过滤由适配器在标准化后完成，
    覆盖不足时标记 partial（见 adapter）。
    """
    return {
        "symbol": kline_symbol(query),
        "duration_seconds": duration_seconds(query.interval),
        "data_length": UPSTREAM_MAX_BARS,
    }


__all__ = ["UPSTREAM_MAX_BARS", "build_request", "duration_seconds", "kline_symbol"]
