"""TqSdk 请求映射（RequestMapper）：标准请求 → 上游 API 参数。

接口签名均已按官方文档核实：

- 合约代码统一为 ``交易所代码.品种代码``（交易所全大写）：单一合约
  ``SHFE.cu2607``；主力连续 ``KQ.m@SHFE.cu``；指数连续/加权
  ``KQ.i@SHFE.bu``。标准 symbol 恒为大写 4 位年月（domain 层归一），交易所
  原生写法由本适配器还原：SHFE 品种小写（SHFE.cu）；CZCE 合约为 3 位年月码
  （CZCE.RM701，2026-11-12 探针实证只认 3 位、4 位 CZCE.RM2701 挂起无响应）；
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

import re
from collections.abc import Callable
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

# 上游写法还原表：标准 symbol 恒为大写 4 位年月（domain 层归一），交易所
# 原生写法（品种大小写 / 年月码位数）在此还原。只收录探针实证条目，
# 绝不预填未核实约定（写错比透传更隐蔽）：
# - SHFE：品种小写（2026-11-03/04 探针 cu/au/bu 实证；2026-11-12 大写
#   SHFE.AU2612 探针实证失败）；整段小写；
# - CZCE：合约 4 位年月码 → 3 位（RM2701 → RM701，2026-11-12 探针实证
#   TianQin 只认 3 位、4 位 CZCE.RM2701 无响应挂起直至超时）。转换只作用
#   于匹配「品种+2 位年+2 位月」的合约代码；主连/加权品种码（无年月段）
#   与不规则代码原样透传。3 位旧写法输入（SR601）不二次加工，如实透传；
# - DCE/INE/GFEX：写法约定未核实，暂直通，探针实证后逐所回填。
# 注意：SHFE 期权（cu2607C50000 混合大小写）、CZCE 期权（品种+年月+C/P+
# 行权价）代码结构均不同，接入 option 资产类型时须另行核实，不得沿用
# 本表的整段小写 / 4→3 规则。
_CZCE_MONTH_CODE_RE = re.compile(r"^([A-Z]{1,2})(\d{2})(\d{2})$")


def _rewrite_shfe_symbol(symbol: str) -> str:
    """SHFE 品种整段小写还原（cu2607 / cu）。"""
    return symbol.lower()


def _rewrite_czce_symbol(symbol: str) -> str:
    """CZCE 合约 4 位年月码 → 3 位原生码（RM2701 → RM701）。

    去掉年份十位（RM2701 → RM7 + 01）；非「品种+2 位年+2 位月」形态
    （主连/加权品种码、3 位旧写法、不规则代码）原样透传。
    """
    matched = _CZCE_MONTH_CODE_RE.match(symbol)
    if matched is None:
        return symbol
    product, year, month = matched.group(1), matched.group(2), matched.group(3)
    return f"{product}{year[1]}{month}"


_SYMBOL_REWRITES: dict[str, Callable[[str], str]] = {
    "SHFE": _rewrite_shfe_symbol,
    "CZCE": _rewrite_czce_symbol,
}


def upstream_symbol(instrument: Instrument, asset_type: AssetType) -> str:
    """标准标的 → TqSdk 合约代码（K 线与快照共用同一映射）。"""
    symbol = instrument.symbol
    rewrite = _SYMBOL_REWRITES.get(instrument.exchange)
    if rewrite is not None:
        symbol = rewrite(symbol)
    base = f"{instrument.exchange}.{symbol}"
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
