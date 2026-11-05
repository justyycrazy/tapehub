"""TqSdk 请求映射（RequestMapper）：标准请求 → 上游 API 参数。

接口签名均已按官方文档核实：

- 合约代码统一为 ``交易所代码.品种代码``（交易所全大写，品种代码遵从交易所
  大小写约定）：单一合约 ``SHFE.cu2607``；主力连续 ``KQ.m@SHFE.cu``；
  指数连续/加权 ``KQ.i@SHFE.bu``；
- 股票/指数/ETF 同格式：``SSE.600000`` / ``SZSE.000001``、指数 ``SSE.000016``、
  ETF ``SSE.510050``（usage/mddatas 文档示例）；
- ``get_kline_serial(symbol, duration_seconds, data_length, adj_type)``：
  duration_seconds 为不超过 1 日的任意秒数（P1D=86400）；
  data_length 默认 200，每序列最大 10000（API 参考）；
  ``adj_type`` 仅对股票/基金类型有效（F=前复权、B=后复权、None=不复权），
  指数无复权概念，始终传 None；
- ``get_quote(symbol)``：订阅并返回指定合约的盘口行情快照
  （协程用法 ``await api.get_quote(symbol)`` 首包到达才返回，官方
  api.py Example2；字段口径见 response_mapper）；
- 权限边界（诚实声明）：免费版仅期货/期权与四大指数实时行情；
  A股股票/ETF 实时五档需专业版账户（运行期事实，不作为能力缺口声明）。
"""

from typing import Any

from tapehub.domain.errors import UnsupportedCapabilityError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import Adjust, AssetType, BarQuery, Instrument, QuoteQuery

# 单次请求上限（2026-11-03 探针实测：PT1M/PT5M 同时恰好返回 5000 根，
# 低于文档 8000/10000 口径，疑受当前套餐限制；升级套餐后需重新实证）
UPSTREAM_MAX_BARS = 5000

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

# 复权映射（API 参考：adj_type 只对股票和基金类型合约有效）
_ADJ_TYPE: dict[Adjust, str | None] = {
    Adjust.NONE: None,
    Adjust.QFQ: "F",
    Adjust.HFQ: "B",
}


def upstream_symbol(instrument: Instrument, asset_type: AssetType) -> str:
    """标准标的 → TqSdk 合约代码（K 线与快照共用同一映射）。"""
    base = f"{instrument.exchange}.{instrument.symbol}"
    if asset_type == AssetType.CONTRACT:
        return base
    if asset_type == AssetType.CONTINUOUS:
        return f"{_CONTINUOUS_PREFIX}{base}"
    if asset_type == AssetType.WEIGHTED_INDEX:
        return f"{_WEIGHTED_PREFIX}{base}"
    if asset_type in (AssetType.STOCK, AssetType.INDEX, AssetType.ETF):
        return base
    raise UnsupportedCapabilityError(
        f"TqSdk does not support asset type {asset_type.value}.",
        details={
            "provider": "tqsdk",
            "instrument": instrument.code,
            "asset_type": asset_type.value,
        },
    )


def kline_symbol(query: BarQuery) -> str:
    """标准标的 → TqSdk 合约代码（K 线链路）。"""
    return upstream_symbol(query.instrument, query.asset_type)


def quote_symbol(query: QuoteQuery) -> str:
    """标准标的 → TqSdk 合约代码（实时快照链路）。"""
    return upstream_symbol(query.instrument, query.asset_type)


def duration_seconds(interval: Interval) -> int:
    """ISO 周期 → K 线周期秒数（仅支持不超过 1 日的周期）。"""
    seconds = _DURATION_SECONDS.get(interval)
    if seconds is None:
        raise UnsupportedCapabilityError(
            f"TqSdk does not support interval {interval.code}.",
            details={"interval": interval.code},
        )
    return seconds


def adj_type(adjust: Adjust) -> str | None:
    """复权方式 → adj_type（none→None、qfq→F、hfq→B）；显式传参不依赖默认值。"""
    return _ADJ_TYPE[adjust]


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
        "adj_type": adj_type(query.adjust),
    }


__all__ = [
    "UPSTREAM_MAX_BARS",
    "adj_type",
    "build_request",
    "duration_seconds",
    "kline_symbol",
    "quote_symbol",
    "upstream_symbol",
]
