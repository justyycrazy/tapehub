"""Tushare 请求映射（RequestMapper）：标准请求 → ts.pro_bar / pro.fut_daily /
pro.fund_daily / pro.opt_daily 参数。

映射规则与上游行为定案见 docs/providers/tushare.md（唯一出处，本文不复述）。
探针运行记录见 probes/results/PROBE_LOG.md（本地文件，不入库）。
官方文档：https://tushare.pro/document/2。
"""

from typing import Any

from tapehub.domain.errors import UnsupportedCapabilityError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import Adjust, AssetType, BarQuery

# 接口单次上限（官方文档：fut_daily 单次最大 2000 条、fund_daily 单次最大
# 5000 条、opt_daily 单次最大 15000 条）
FUT_DAILY_MAX_ROWS = 2000
FUND_DAILY_MAX_ROWS = 5000
OPT_DAILY_MAX_ROWS = 15000

_FREQ_CODES: dict[Interval, str] = {
    Interval.parse("P1D"): "D",
    Interval.parse("P1W"): "W",
    Interval.parse("P1M"): "M",
}

# adj 枚举：None 表示不复权（官方默认），qfq/hfq 原样传递
_ADJUST_CODES: dict[Adjust, str | None] = {
    Adjust.NONE: None,
    Adjust.QFQ: "qfq",
    Adjust.HFQ: "hfq",
}

# 股票/期货 ts_code 后缀（标准代码 exchange 段 → tushare 后缀）：实证条目表
# （收录依据见 docs/providers/tushare.md）；证伪即移除，绝不预填未核实条目
_STOCK_SUFFIXES = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}
# 指数后缀：pro_bar asset='I' 官方枚举仅「沪深指数」（doc_id=109），BSE 指数
# 不声明、映射层明确拒绝
_INDEX_SUFFIXES = {"SSE": "SH", "SZSE": "SZ"}
# ETF 后缀：fund_daily 为场内基金接口（doc_id=127，沪深两所同构）
_ETF_SUFFIXES = {"SSE": "SH", "SZSE": "SZ"}
# 期权后缀：opt_daily 六所（doc_id=159 交易所枚举 + 五所探针实证码格式）；
# CFFEX 期权 .CFX 与该所期货同后缀（非 ths 的 CFE）
_OPTION_SUFFIXES = {
    "SSE": "SH",
    "SZSE": "SZ",
    "CFFEX": "CFX",
    "DCE": "DCE",
    "SHFE": "SHF",
    "CZCE": "ZCE",
}
_FUTURES_SUFFIXES = {
    "SHFE": "SHF",
    "DCE": "DCE",
    "INE": "INE",
    "CFFEX": "CFX",
    "CZCE": "ZCE",
    "GFEX": "GFE",
}

# 主力连续 ts_code 规则：多数交易所主连 = 品种 + "L"（CUL.SHF/SRL.ZCE/
# ML.DCE/SCL.INE/LCL.GFE）；CFFEX 主力直拼品种（IF.CFX，该所连续后缀
# IFL/IFL1-3 为当月/次月/当季/下季月份系列）
_CONTINUOUS_PRODUCT_SUFFIX = "L"
_CONTINUOUS_DIRECT_EXCHANGES = frozenset({"CFFEX"})


def _suffix(query: BarQuery, table: dict[str, str]) -> str:
    suffix = table.get(query.instrument.exchange)
    if suffix is None:
        raise UnsupportedCapabilityError(
            f"Tushare does not support exchange {query.instrument.exchange!r} here.",
            details={
                "provider": "tushare",
                "instrument": query.instrument.code,
                "asset_type": query.asset_type.value,
            },
        )
    return suffix


def stock_ts_code(query: BarQuery) -> str:
    """股票 ts_code：CN-SSE-600000 → 600000.SH。"""
    return f"{query.instrument.symbol}.{_suffix(query, _STOCK_SUFFIXES)}"


def index_ts_code(query: BarQuery) -> str:
    """指数 ts_code：CN-SSE-000001 → 000001.SH、CN-SZSE-399001 → 399001.SZ。"""
    return f"{query.instrument.symbol}.{_suffix(query, _INDEX_SUFFIXES)}"


def etf_ts_code(query: BarQuery) -> str:
    """ETF ts_code：CN-SSE-510050 → 510050.SH、CN-SZSE-159915 → 159915.SZ。"""
    return f"{query.instrument.symbol}.{_suffix(query, _ETF_SUFFIXES)}"


def option_ts_code(query: BarQuery) -> str:
    """期权 ts_code（合约符号原样透传，不做年份猜测）：CN-SSE-10012437 →
    10012437.SH、CN-CFFEX-IO2611-C-4150 → IO2611-C-4150.CFX（CFFEX 带连字符
    中段）、CN-CZCE-SR701C5900 → SR701C5900.ZCE（3 位月码直通）。"""
    return f"{query.instrument.symbol}.{_suffix(query, _OPTION_SUFFIXES)}"


def futures_ts_code(query: BarQuery) -> str:
    """期货 ts_code：单一合约原样（CN-SHFE-CU2401 → CU2401.SHF；CZCE 与统一
    标准码同为 4 位年月，CN-CZCE-SR2601 → SR2601.ZCE 直通）；主力连续
    多数交易所品种+L（CN-SHFE-CU → CUL.SHF、CN-GFEX-LC → LCL.GFE）、
    CFFEX 直拼（CN-CFFEX-IF → IF.CFX）。非 4 位年月的 CZCE 合约输入
    （3 位旧写法等）如实透传给上游，查空/报错如实表现，不做静默年份猜测。"""
    suffix = _suffix(query, _FUTURES_SUFFIXES)
    symbol = query.instrument.symbol
    if (
        query.asset_type is AssetType.CONTINUOUS
        and query.instrument.exchange not in _CONTINUOUS_DIRECT_EXCHANGES
    ):
        symbol = f"{symbol}{_CONTINUOUS_PRODUCT_SUFFIX}"
    return f"{symbol}.{suffix}"


def freq_code(interval: Interval) -> str:
    freq = _FREQ_CODES.get(interval)
    if freq is None:
        raise UnsupportedCapabilityError(
            f"Tushare pro_bar does not support interval {interval.code}.",
            details={"interval": interval.code},
        )
    return freq


def _date_code(value: object) -> str:
    return value.strftime("%Y%m%d")  # type: ignore[union-attr]


def build_request(operation: str, query: BarQuery) -> dict[str, Any]:
    """按内部 operation 构造调用参数；所有相关参数显式传递。"""
    if operation == "pro_bar":
        # asset 官方枚举（doc_id=109）：E=股票、I=沪深指数，按资产类型显式传递；
        # adj 官方语义「只针对股票」，指数能力仅声明 none（上游忽略 adj），恒 None
        is_index = query.asset_type is AssetType.INDEX
        return {
            "ts_code": index_ts_code(query) if is_index else stock_ts_code(query),
            "asset": "I" if is_index else "E",
            "freq": freq_code(query.interval),
            "adj": _ADJUST_CODES[query.adjust],
            "start_date": _date_code(query.start),
            "end_date": _date_code(query.end),
        }
    if operation == "fut_daily":
        return {
            "ts_code": futures_ts_code(query),
            "start_date": _date_code(query.start),
            "end_date": _date_code(query.end),
        }
    if operation == "fund_daily":
        return {
            "ts_code": etf_ts_code(query),
            "start_date": _date_code(query.start),
            "end_date": _date_code(query.end),
        }
    if operation == "opt_daily":
        return {
            "ts_code": option_ts_code(query),
            "start_date": _date_code(query.start),
            "end_date": _date_code(query.end),
        }
    raise UnsupportedCapabilityError(
        f"unknown operation {operation!r}",
        details={"provider": "tushare", "operation": operation},
    )


__all__ = [
    "FUND_DAILY_MAX_ROWS",
    "FUT_DAILY_MAX_ROWS",
    "OPT_DAILY_MAX_ROWS",
    "build_request",
    "etf_ts_code",
    "futures_ts_code",
    "index_ts_code",
    "option_ts_code",
    "stock_ts_code",
]
