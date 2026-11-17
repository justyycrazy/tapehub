"""Tushare 请求映射（RequestMapper）：标准请求 → ts.pro_bar / pro.fut_daily /
pro.fund_daily / pro.opt_daily 参数。

映射规则与上游行为定案见 docs/providers/tushare.md（唯一出处，本文不复述）。
官方文档：https://tushare.pro/document/2。
"""

import re
from typing import Any

from tapehub.domain.errors import UnsupportedCapabilityError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import Adjust, AssetType, BarQuery, Instrument

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
# 期权三子类后缀表（asset_type 三分：commodity-option / index-option /
# etf-option，上游同为 pro.opt_daily、后缀码式零变化——2026-11-16 聚焦轮
# 目录+日 K 双证收编 INE/GFEX，doc_id=159 文档枚举未列这两所属文档滞后；
# CFFEX 期权 .CFX 与该所期货同后缀，非 ths 的 CFE）；子类×本所矛盾组合
# 已由 domain 层词汇表构造期拒绝，码式矩阵见 docs/providers/tushare.md
# 「标的代码映射」
_ETF_OPTION_SUFFIXES = {"SSE": "SH", "SZSE": "SZ"}
_INDEX_OPTION_SUFFIXES = {"CFFEX": "CFX"}
_COMMODITY_OPTION_SUFFIXES = {
    "DCE": "DCE",
    "SHFE": "SHF",
    "CZCE": "ZCE",
    "INE": "INE",
    "GFEX": "GFE",
}
_OPTION_SUFFIX_TABLES: dict[AssetType, dict[str, str]] = {
    AssetType.ETF_OPTION: _ETF_OPTION_SUFFIXES,
    AssetType.INDEX_OPTION: _INDEX_OPTION_SUFFIXES,
    AssetType.COMMODITY_OPTION: _COMMODITY_OPTION_SUFFIXES,
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


# 期权结构子类统一标准式解析（commodity-option / index-option，域层已
# fullmatch 校验；etf-option 数字合约码原样透传不走本式）。原生 ts_code
# 符号逐所还原（2026-11-16 聚焦轮八所探针定案，出处 docs/providers/tushare.md
# 「标的代码映射」）：SHFE/INE 无连字符直通（FU2612C6100 / SC2701C550）；
# CZCE 3 位月码（SR701C5900——统一式 4 位年月去年份十位）；DCE/GFEX/CFFEX
# 连字符式（M2612-C-3500 / LC2701-C-90000 / IO2611-C-4150）。品种一律大写。
_OPTION_SYMBOL_RE = re.compile(r"^([A-Z]+)(\d{2})(\d{2})([CP])(\d+(?:\.\d+)?)$")


def _option_native_symbol(instrument: Instrument) -> str:
    """统一标准式期权符号 → tushare 原生 ts_code 符号（逐所还原，见上方码式说明）。"""
    matched = _OPTION_SYMBOL_RE.match(instrument.symbol)
    if matched is None:
        # 域层已校验统一式，此分支仅防 model_construct 绕过路径
        raise UnsupportedCapabilityError(
            f"invalid option symbol {instrument.symbol!r}: expected "
            "'{PRODUCT}{YYMM}{C|P}{STRIKE}' (e.g. CU2701C102000)",
            details={"provider": "tushare", "instrument": instrument.code},
        )
    product, year, month, cp, strike = matched.groups()
    if instrument.exchange == "CZCE":
        return f"{product}{year[1]}{month}{cp}{strike}"
    if instrument.exchange in ("DCE", "GFEX", "CFFEX"):
        return f"{product}{year}{month}-{cp}-{strike}"
    return f"{product}{year}{month}{cp}{strike}"  # SHFE/INE（无连字符直通）


def option_ts_code(query: BarQuery) -> str:
    """期权 ts_code：etf-option 数字合约码原样透传（CN-SSE-10012437 →
    10012437.SH）；结构子类统一式 → 原生式（CN-CFFEX-IO2611C4150 →
    IO2611-C-4150.CFX 带连字符中段、CN-CZCE-SR2701C5900 → SR701C5900.ZCE
    3 位月码）；后缀按期权子类查分表（_OPTION_SUFFIX_TABLES）。"""
    suffix = _suffix(query, _OPTION_SUFFIX_TABLES[query.asset_type])
    if query.asset_type is AssetType.ETF_OPTION:
        return f"{query.instrument.symbol}.{suffix}"
    return f"{_option_native_symbol(query.instrument)}.{suffix}"


def futures_ts_code(query: BarQuery) -> str:
    """期货 ts_code：单一合约原样（CN-SHFE-CU2401 → CU2401.SHF；CZCE 与统一
    标准码同为 4 位年月，CN-CZCE-SR2601 → SR2601.ZCE 直通）；主力连续
    多数交易所品种+L（CN-SHFE-CU → CUL.SHF、CN-GFEX-LC → LCL.GFE）、
    CFFEX 直拼（CN-CFFEX-IF → IF.CFX）。CZCE 3 位旧写法与不规则形态自
    2026-11-17 起由域层构造期拒绝（contract×CZCE 4 位年月校验，见
    domain/models.py），不再到达本映射；不做年份猜测，映射层亦不设二次校验。"""
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
