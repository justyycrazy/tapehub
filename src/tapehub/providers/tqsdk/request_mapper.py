"""TqSdk 请求映射（RequestMapper）：标准请求 → 上游 API 参数。

映射规则与上游行为定案见 docs/providers/tqsdk.md（唯一出处，本文不复述）。
探针运行记录见 probes/results/PROBE_LOG.md（本地文件，不入库）。
"""

import re
from collections.abc import Callable
from typing import Any

from tapehub.domain.errors import UnsupportedCapabilityError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import Adjust, AssetType, BarQuery, Instrument, QuoteQuery

# 单次请求上限（实测 PT1M/PT5M 恰 5000 根，低于文档 8000/10000 口径，
# 疑受当前套餐限制；升级套餐后需重新核实）
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
# 原生写法（品种大小写 / 年月码位数）在此还原。只收实证条目（依据见
# docs/providers/tqsdk.md），绝不预填未核实约定（写错比透传更隐蔽）：
# - SHFE：品种小写；整段小写；
# - CZCE：合约 4 位年月码 → 3 位（RM2701 → RM701；上游只认 3 位、4 位
#   写法无响应挂起直至超时）。转换只作用于匹配「品种+2 位年+2 位月」
#   的合约代码；主连/加权品种码（无年月段）与不规则代码原样透传。
#   3 位旧写法输入（SR601）不二次加工，如实透传；
# - DCE/INE/GFEX：写法约定未核实，暂直通，实证后逐所回填。
# 注意：期权代码结构均与该所期货不同（品种小写混合大小写、连字符形态、
# CZCE 期权 3 位月码直通），不沿用本表规则——期权还原见下方
# _OPTION_PRODUCT_LOWER_EXCHANGES（2026-11-16 聚焦轮八所探针定案）。
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

# 期权品种小写还原交易所集合（2026-11-16 聚焦轮八所探针定案）：SHFE/INE/
# DCE/GFEX 期权品种字母段小写（cu2701C102000 / sc2701C550 / m2701-C-3500 /
# lc2701-C-90000）——注意 DCE 期权品种小写与该所期货大写实证相反；SSE/SZSE
# （数字合约码）、CFFEX/CZCE（大写品种）直通。月码与连字符形态在标准码中
# 已与上游一致（CZCE 期权 3 位月码直通），不做改写。
_OPTION_PRODUCT_LOWER_EXCHANGES = frozenset({"SHFE", "INE", "DCE", "GFEX"})
_OPTION_PRODUCT_RE = re.compile(r"^([A-Za-z]+)(.+)$")


def _rewrite_option_symbol(symbol: str) -> str:
    """期权品种字母段小写还原（CU2701C102000 → cu2701C102000）。"""
    matched = _OPTION_PRODUCT_RE.match(symbol)
    if matched is None:
        return symbol
    product, rest = matched.groups()
    return product.lower() + rest


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
    if asset_type is AssetType.OPTION:
        option = instrument.symbol
        if instrument.exchange in _OPTION_PRODUCT_LOWER_EXCHANGES:
            option = _rewrite_option_symbol(option)
        return f"{instrument.exchange}.{option}"
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
