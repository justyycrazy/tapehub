"""Tushare 请求映射（RequestMapper）：标准请求 → ts.pro_bar / pro.fut_daily 参数。

接口签名均已按官方文档核实（https://tushare.pro/document/2）：

- 股票：模块级 ``ts.pro_bar(ts_code, asset='E', freq, adj, start_date, end_date)``；
  freq 枚举 D/W/M（默认 D），adj 枚举 None/qfq/hfq（仅日线复权，默认 None 未复权）；
- 期货单一合约：``pro.fut_daily(ts_code, start_date, end_date)``（无复权）；
- ts_code 股票后缀 .SH/.SZ 已按文档示例核实；北交所 BSE→BJ 经探针实证
  （报告 20261112T172921+0800，全市场 920 段）；期货后缀五所经探针实证
  （报告 20261112T124404+0800）：SHF/DCE/INE/CFX/ZCE——其中 CZCE 在 tushare
  为 4 位年月码（SR2601），标准代码用交易所原生 3 位（CN-CZCE-SR601），
  由 :func:`_czce_symbol` 转换；
- 主力连续（总览页 doc_id=134 连续合约格式 XX=主力 / XXL=主连）：多数交易所
  主连为品种+L（CUL.SHF/SRL.ZCE/ML.DCE/SCL.INE），CFFEX 连续后缀是月份系列
  （IFL=当月、IFL1-3=次月/当季/下季），主力即主连候选直拼品种（IF.CFX）——
  探针实证报告 20261112T133250+0800；
- 日期格式 YYYYMMDD。

原则：所有相关参数显式传递，不依赖上游默认值——
pro_bar 的 freq 默认 D、adj 默认 None（未复权），与 tapehub 的声明默认值
（股票 qfq）不同，必须显式传参（adjust=none 时传 Python None 表示不复权）。
"""

import re
from datetime import datetime
from typing import Any

from tapehub.domain.errors import UnsupportedCapabilityError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import CN_TIMEZONE, Adjust, AssetType, BarQuery

# 期货接口单次上限（官方文档：单次最大 2000 条）
FUT_DAILY_MAX_ROWS = 2000

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

# 股票/期货 ts_code 后缀（标准代码 exchange 段 → tushare 后缀）：探针实证条目。
# BSE→BJ 经探针实证（报告 20261112T172921+0800：目录 355 在市标的全部 920 段
# + 920000.BJ daily 真实日线；文档 doc_id=14 后缀表核实，83 段老代码已迁移）；
# 期货五所（报告 20261112T124404+0800，.SHF 出自文档示例，其余四所 raw 实证）；
# 证伪即移除，绝不预填未核实条目
_STOCK_SUFFIXES = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}
_FUTURES_SUFFIXES = {
    "SHFE": "SHF",
    "DCE": "DCE",
    "INE": "INE",
    "CFFEX": "CFX",
    "CZCE": "ZCE",
}

# 主力连续 ts_code 规则：多数交易所主连 = 品种 + "L"（CUL.SHF/SRL.ZCE/
# ML.DCE/SCL.INE，探针实证 20261112T133250+0800）；CFFEX 主力直拼品种
# （IF.CFX，该所连续后缀 IFL/IFL1-3 为当月/次月/当季/下季月份系列）
_CONTINUOUS_PRODUCT_SUFFIX = "L"
_CONTINUOUS_DIRECT_EXCHANGES = frozenset({"CFFEX"})

# CZCE 合约代码：tushare 用 4 位年月（SR2601）；标准代码用交易所原生 3 位
# （SR601 = 年末位 + 月）。转换在 [当前年-4, 当前年+5] 的 10 年窗口内取
# 年末位匹配的唯一 4 位年份——连续 10 年的末位恰好互不相同，结果唯一；
# 偏向未来（活跃合约通常挂在未来月份），近月已摘牌合约（最远 4 年前）
# 亦在窗口内
_CZCE_MONTH_CODE_RE = re.compile(r"^([A-Z]{1,2})(\d)(\d{2})$")  # 品种 + 年(1) + 月(2)
_CZCE_YM_CODE_RE = re.compile(r"^[A-Z]{1,2}\d{4}$")  # 已是 4 位年月（直通）
_CZCE_YEAR_WINDOW_PAST = 4
_CZCE_YEAR_WINDOW_FUTURE = 5


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


def futures_ts_code(query: BarQuery) -> str:
    """期货 ts_code：单一合约原样（CN-SHFE-CU2401 → CU2401.SHF）；主力连续
    多数交易所品种+L（CN-SHFE-CU → CUL.SHF）、CFFEX 直拼（CN-CFFEX-IF →
    IF.CFX）；CZCE 原生 3 位年月码转为 tushare 4 位（CN-CZCE-SR601 →
    SR2601.ZCE，探针实证）。"""
    suffix = _suffix(query, _FUTURES_SUFFIXES)
    symbol = query.instrument.symbol
    if (
        query.asset_type is AssetType.CONTINUOUS
        and query.instrument.exchange not in _CONTINUOUS_DIRECT_EXCHANGES
    ):
        symbol = f"{symbol}{_CONTINUOUS_PRODUCT_SUFFIX}"
    # 3→4 位年月码转换只作用于带月份码的单合约；主连代码（品种[+L]）
    # 无月份段，直通
    if query.asset_type is AssetType.CONTRACT and query.instrument.exchange == "CZCE":
        symbol = _czce_symbol(symbol, query)
    return f"{symbol}.{suffix}"


def _czce_symbol(symbol: str, query: BarQuery) -> str:
    """CZCE 原生 3 位年月码 → tushare 4 位年月码（SR601 → SR2601）。

    4 位年月码直通（防御：调用方已按 4 位书写时不二次加工）；无法唯一解析
    抛 UnsupportedCapabilityError。转换规则的实证：SR2601.ZCE 探针有数据、
    原生 3 位 SR601.ZCE 全空（报告 20261112T124404+0800）。"""
    if _CZCE_YM_CODE_RE.match(symbol):
        return symbol
    matched = _CZCE_MONTH_CODE_RE.match(symbol)
    if matched is None:
        raise UnsupportedCapabilityError(
            f"Tushare CZCE symbol {symbol!r} is not a valid month code "
            "(expected product + 3-digit year/month, e.g. SR601).",
            details={
                "provider": "tushare",
                "instrument": query.instrument.code,
                "asset_type": query.asset_type.value,
            },
        )
    product, year_digit, month = matched.group(1), int(matched.group(2)), matched.group(3)
    now_year = datetime.now(CN_TIMEZONE).year
    candidates = [
        year
        for year in range(
            now_year - _CZCE_YEAR_WINDOW_PAST, now_year + _CZCE_YEAR_WINDOW_FUTURE + 1
        )
        if year % 10 == year_digit
    ]
    if len(candidates) != 1:
        raise UnsupportedCapabilityError(
            f"Tushare CZCE symbol {symbol!r} cannot be resolved to a unique "
            f"4-digit year (candidates: {candidates}).",
            details={
                "provider": "tushare",
                "instrument": query.instrument.code,
                "asset_type": query.asset_type.value,
            },
        )
    return f"{product}{candidates[0] % 100:02d}{month}"


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
        return {
            "ts_code": stock_ts_code(query),
            "asset": "E",  # E=股票（官方枚举，默认 E，显式传递）
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
    raise UnsupportedCapabilityError(
        f"unknown operation {operation!r}",
        details={"provider": "tushare", "operation": operation},
    )


__all__ = [
    "FUT_DAILY_MAX_ROWS",
    "build_request",
    "futures_ts_code",
    "stock_ts_code",
]
