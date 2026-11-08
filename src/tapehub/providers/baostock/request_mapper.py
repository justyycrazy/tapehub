"""baostock 请求映射（RequestMapper）：标准请求 → query_history_k_data_plus 参数。

原则：所有相关参数都显式传递，不依赖 baostock 的默认值——例如
adjustflag 上游默认 "3"（不复权）、start 默认 2015-01-01，与 tapehub
的声明默认值（个股 qfq、显式时间范围）不一致，因此必须显式传参。

接口签名均已按官方文档核实（https://baostock.com/mainContent?file=pythonAPI.md）。

文档矛盾备注：参数说明章节写作 ``start/end``，示例代码使用
``start_date/end_date``——适配器采用示例风格（与全部官方示例一致），
实际生效行为由数据探针实证（probes/cases/baostock.yaml）。
"""

from datetime import datetime

from tapehub.domain.errors import UnsupportedCapabilityError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import Adjust, BarQuery

# 个股/指数/ETF 统一使用「前缀.6 位代码」格式（sh.600000 / sz.000001）；
# baostock 仅覆盖沪深两市，北交所（BSE）未支持
_EXCHANGE_PREFIXES = {"SSE": "sh", "SZSE": "sz"}

_PERIOD_BY_INTERVAL = {
    Interval.parse("P1D"): "d",
    Interval.parse("P1W"): "w",
    Interval.parse("P1M"): "m",
    Interval.parse("PT5M"): "5",
    Interval.parse("PT15M"): "15",
    Interval.parse("PT30M"): "30",
    # 上游 60 分钟线；项目周期代码 PT1H ≠ PT60M，不做等价换算
    Interval.parse("PT1H"): "60",
}

_ADJUST_CODES = {Adjust.NONE: "3", Adjust.QFQ: "2", Adjust.HFQ: "1"}

# 分钟族周期集合（映射上游 5/15/30/60 分钟线）
_MINUTE_INTERVALS = frozenset(
    Interval.parse(code) for code in ("PT5M", "PT15M", "PT30M", "PT1H")
)

# 周期族 → 返回字段集合（官方三套 fields，全部显式写全）：
# 日线（含 preclose/turn/tradestatus/pctChg/isST）；估值四项仅个股日线有值，
# 指数/ETF 请求它们只会得到空串，因此按资产类型拆分
_DAILY_STOCK_FIELDS = (
    "date,code,open,high,low,close,preclose,volume,amount,adjustflag,"
    "turn,tradestatus,pctChg,isST,peTTM,pbMRQ,psTTM,pcfNcfTTM"
)
_DAILY_INDEX_FIELDS = (
    "date,code,open,high,low,close,preclose,volume,amount,adjustflag,pctChg"
)
# 周月线：无 preclose/tradestatus/isST/估值
_WEEKLY_MONTHLY_FIELDS = (
    "date,code,open,high,low,close,volume,amount,adjustflag,turn,pctChg"
)
# 分钟线：仅 date/time + OHLC + 量额（不包含指数）
_MINUTE_FIELDS = "date,time,code,open,high,low,close,volume,amount,adjustflag"


def _unsupported(query: BarQuery, reason: str) -> UnsupportedCapabilityError:
    return UnsupportedCapabilityError(
        f"baostock does not support the requested symbol mapping: {reason}",
        details={
            "provider": "baostock",
            "instrument": query.instrument.code,
            "asset_type": query.asset_type.value,
            "variant": query.variant.value,
            "interval": query.interval.code,
            "adjust": query.adjust.value,
        },
    )


def _date_code(value: datetime) -> str:
    """边界转换为 baostock 的 YYYY-MM-DD 格式。"""
    return value.strftime("%Y-%m-%d")


def date_window(query: BarQuery) -> tuple[str, str]:
    """闭区间 [start, end] 转换为 (start_date, end_date)。

    上游按日期过滤；end 的闭/开区间语义官方文档自相矛盾（stockKData 页
    称「包含」、indexData 页称「不包含」），按示例风格实现为闭区间，
    待探针实证后修正口径并回填 README。
    """
    return _date_code(query.start), _date_code(query.end)


def adjust_param(adjust: Adjust) -> str:
    """复权参数显式映射；上游默认 "3"（不复权）与个股声明默认 qfq 不同。"""
    return _ADJUST_CODES[adjust]


def frequency_param(interval: Interval) -> str:
    """周期参数映射；仅映射 manifest 已声明的周期。"""
    frequency = _PERIOD_BY_INTERVAL.get(interval)
    if frequency is None:
        raise UnsupportedCapabilityError(
            f"baostock does not support interval {interval.code}.",
            details={"interval": interval.code},
        )
    return frequency


def fields_param(query: BarQuery) -> str:
    """按 (资产类型, 周期族) 选择官方 fields 集合；全部显式写全。"""
    if query.interval in _MINUTE_INTERVALS:
        return _MINUTE_FIELDS
    if query.interval == Interval.parse("P1D") and query.asset_type.value == "stock":
        return _DAILY_STOCK_FIELDS
    if query.interval == Interval.parse("P1D"):
        return _DAILY_INDEX_FIELDS
    return _WEEKLY_MONTHLY_FIELDS


def code_param(query: BarQuery) -> str:
    """标的代码：沪深两市统一「前缀.6 位代码」（sh.600000 / sz.000001）。"""
    prefix = _EXCHANGE_PREFIXES.get(query.instrument.exchange)
    if prefix is None:
        raise _unsupported(query, f"exchange {query.instrument.exchange!r} is not supported")
    return f"{prefix}.{query.instrument.symbol}"


def build_request(operation: str, query: BarQuery) -> dict[str, str]:
    """按内部 operation 构造 baostock 调用参数；所有参数显式传递。"""
    if operation != "history_k_data_plus":
        raise _unsupported(query, f"unknown operation {operation!r}")
    start_date, end_date = date_window(query)
    return {
        "code": code_param(query),
        "fields": fields_param(query),
        "start_date": start_date,
        "end_date": end_date,
        "frequency": frequency_param(query.interval),
        "adjustflag": adjust_param(query.adjust),
    }


__all__ = ["build_request", "code_param", "date_window", "fields_param"]
