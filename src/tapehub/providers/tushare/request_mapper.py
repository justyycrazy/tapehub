"""Tushare 请求映射（RequestMapper）：标准请求 → ts.pro_bar / pro.fut_daily 参数。

接口签名均已按官方文档核实（https://tushare.pro/document/2）：

- 股票：模块级 ``ts.pro_bar(ts_code, asset='E', freq, adj, start_date, end_date)``；
  freq 枚举 D/W/M（默认 D），adj 枚举 None/qfq/hfq（仅日线复权，默认 None 未复权）；
- 期货单一合约：``pro.fut_daily(ts_code, start_date, end_date)``（无复权）；
- ts_code 股票后缀已按文档示例核实 .SH/.SZ；期货后缀仅核实上期所 .SHF；
- 日期格式 YYYYMMDD。

原则：所有相关参数显式传递，不依赖上游默认值——
pro_bar 的 freq 默认 D、adj 默认 None（未复权），与 tapehub 的声明默认值
（股票 qfq）不同，必须显式传参（adjust=none 时传 Python None 表示不复权）。
"""

from typing import Any

from tapehub.domain.errors import UnsupportedCapabilityError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import Adjust, BarQuery

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

# 已按文档示例核实的后缀（其余交易所待逐所核实后补充）
_STOCK_SUFFIXES = {"SSE": "SH", "SZSE": "SZ"}
_FUTURES_SUFFIXES = {"SHFE": "SHF"}


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
    """期货合约 ts_code：CN-SHFE-CU2401 → CU2401.SHF（符号按交易所官方写法传递）。"""
    return f"{query.instrument.symbol}.{_suffix(query, _FUTURES_SUFFIXES)}"


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
