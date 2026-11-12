"""Tushare 请求映射（RequestMapper）：标准请求 → ts.pro_bar / pro.fut_daily 参数。

接口签名均已按官方文档核实（https://tushare.pro/document/2）：

- 股票：模块级 ``ts.pro_bar(ts_code, asset='E', freq, adj, start_date, end_date)``；
  freq 枚举 D/W/M（默认 D），adj 枚举 None/qfq/hfq（仅日线复权，默认 None 未复权）；
- 期货单一合约：``pro.fut_daily(ts_code, start_date, end_date)``（无复权）；
- ts_code 股票后缀 .SH/.SZ 已按文档示例核实；北交所 BSE→BJ 经探针实证
  （报告 20261112T172921+0800，全市场 920 段）；期货后缀五所经探针实证
  （报告 20261112T124404+0800）：SHF/DCE/INE/CFX/ZCE——CZCE 在 tushare
  为 4 位年月码，与统一标准码同形（CN-CZCE-SR2601 → SR2601.ZCE，直通；
  2026-11-12 探针实证 4 位可用、3 位旧写法全空）；
- 主力连续（总览页 doc_id=134 连续合约格式 XX=主力 / XXL=主连）：多数交易所
  主连为品种+L（CUL.SHF/SRL.ZCE/ML.DCE/SCL.INE），CFFEX 连续后缀是月份系列
  （IFL=当月、IFL1-3=次月/当季/下季），主力即主连候选直拼品种（IF.CFX）——
  探针实证报告 20261112T133250+0800；
- 日期格式 YYYYMMDD。

原则：所有相关参数显式传递，不依赖上游默认值——
pro_bar 的 freq 默认 D、adj 默认 None（未复权），与 tapehub 的声明默认值
（股票 qfq）不同，必须显式传参（adjust=none 时传 Python None 表示不复权）。
"""

from typing import Any

from tapehub.domain.errors import UnsupportedCapabilityError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import Adjust, AssetType, BarQuery

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
    """期货 ts_code：单一合约原样（CN-SHFE-CU2401 → CU2401.SHF；CZCE 与统一
    标准码同为 4 位年月，CN-CZCE-SR2601 → SR2601.ZCE 直通）；主力连续
    多数交易所品种+L（CN-SHFE-CU → CUL.SHF）、CFFEX 直拼（CN-CFFEX-IF →
    IF.CFX）。非 4 位年月的 CZCE 合约输入（3 位旧写法等）如实透传给上游，
    查空/报错如实表现，不做静默年份猜测。"""
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
