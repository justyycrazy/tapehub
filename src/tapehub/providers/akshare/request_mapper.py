"""akshare 请求映射（RequestMapper）：标准请求 → akshare 接口参数。

原则：所有相关参数都显式传递，不依赖 akshare 的默认值——
例如 stock_zh_a_hist 的 adjust 默认 ""（不复权），与 tapehub 的
声明默认值（个股 qfq）不一致，因此 adjust 必须显式传参。

接口签名均已按官方文档核实（https://akshare.akfamily.xyz/）。
"""

from datetime import datetime

from tapehub.domain.errors import UnsupportedCapabilityError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import Adjust, BarQuery, DataVariant

# 指数/个股符号的交易所前缀（仅列出已在上游文档中确认的）
_EXCHANGE_PREFIXES = {
    "SSE": "sh",
    "SZSE": "sz",
    "CSI": "csi",
    "BSE": "bj",
}

# 各上游接口文档中确认支持的交易所前缀
_STOCK_PREFIX_EXCHANGES = ("SSE", "SZSE")
_INDEX_PREFIX_EXCHANGES = {
    DataVariant.EASTMONEY: ("SSE", "SZSE", "CSI", "BSE"),
    DataVariant.SINA: ("SSE", "SZSE"),
    DataVariant.TENCENT: ("SSE", "SZSE"),
}

_PERIOD_BY_INTERVAL = {
    Interval.parse("P1D"): "daily",
    Interval.parse("P1W"): "weekly",
    Interval.parse("P1M"): "monthly",
}

_ADJUST_CODES = {Adjust.NONE: "", Adjust.QFQ: "qfq", Adjust.HFQ: "hfq"}


def _unsupported(query: BarQuery, reason: str) -> UnsupportedCapabilityError:
    return UnsupportedCapabilityError(
        f"akshare does not support the requested symbol mapping: {reason}",
        details={
            "provider": "akshare",
            "instrument": query.instrument.code,
            "asset_type": query.asset_type.value,
            "variant": query.variant.value,
            "interval": query.interval.code,
            "adjust": query.adjust.value,
        },
    )


def _date_code(value: datetime) -> str:
    """边界转换为 akshare 的 YYYYMMDD 格式。"""
    return value.strftime("%Y%m%d")


def date_window(query: BarQuery) -> tuple[str, str]:
    """闭区间 [start, end] 转换为 (start_date, end_date)。

    上游按日期过滤，带时间的边界由适配器在标准化后再次精确过滤。
    """
    return _date_code(query.start), _date_code(query.end)


def adjust_param(adjust: Adjust) -> str:
    """复权参数显式映射；上游默认 ""（不复权）与 tapehub 默认值不同。"""
    return _ADJUST_CODES[adjust]


def period_param(interval: Interval) -> str:
    """周期参数映射；仅 stock_zh_a_hist 支持周/月线。"""
    period = _PERIOD_BY_INTERVAL.get(interval)
    if period is None:
        raise UnsupportedCapabilityError(
            f"akshare does not support interval {interval.code}.",
            details={"interval": interval.code},
        )
    return period


def _prefixed_symbol(query: BarQuery, allowed_exchanges: tuple[str, ...]) -> str:
    prefix = _EXCHANGE_PREFIXES.get(query.instrument.exchange)
    if prefix is None or query.instrument.exchange not in allowed_exchanges:
        raise _unsupported(query, f"exchange {query.instrument.exchange!r} is not supported here")
    return f"{prefix}{query.instrument.symbol}"


def stock_symbol(query: BarQuery) -> str:
    """个股符号：eastmoney 用纯 6 位代码；sina/tencent 用带市场前缀符号。"""
    if query.variant == DataVariant.EASTMONEY:
        if query.instrument.exchange not in ("SSE", "SZSE"):
            raise _unsupported(
                query, f"exchange {query.instrument.exchange!r} is not supported here"
            )
        return query.instrument.symbol
    return _prefixed_symbol(query, _STOCK_PREFIX_EXCHANGES)


def index_symbol(query: BarQuery) -> str:
    """指数符号：三个上游接口都用带前缀符号（sh000001 / sz399001 / csi000905）。"""
    allowed = _INDEX_PREFIX_EXCHANGES.get(query.variant, ())
    return _prefixed_symbol(query, allowed)


def futures_symbol(query: BarQuery) -> str:
    """期货符号（新浪）：单一合约原样传递；主力连续为品种代码 + "0"（如 RB → RB0）。"""
    if query.asset_type.value == "continuous":
        return f"{query.instrument.symbol}0"
    return query.instrument.symbol


def build_request(operation: str, query: BarQuery) -> dict[str, str]:
    """按内部 operation 构造 akshare 调用参数；所有参数显式传递。"""
    start_date, end_date = date_window(query)
    if operation == "stock_zh_a_hist":
        return {
            "symbol": stock_symbol(query),
            "period": period_param(query.interval),
            "start_date": start_date,
            "end_date": end_date,
            "adjust": adjust_param(query.adjust),
        }
    if operation == "stock_zh_a_daily":
        return {
            "symbol": stock_symbol(query),
            "start_date": start_date,
            "end_date": end_date,
            "adjust": adjust_param(query.adjust),
        }
    if operation == "stock_zh_a_hist_tx":
        return {
            "symbol": stock_symbol(query),
            "start_date": start_date,
            "end_date": end_date,
            "adjust": adjust_param(query.adjust),
        }
    if operation == "stock_zh_index_daily_em":
        return {"symbol": index_symbol(query), "start_date": start_date, "end_date": end_date}
    if operation == "stock_zh_index_daily":
        # 新浪指数接口不支持日期参数，返回全量历史，由适配器内存过滤
        return {"symbol": index_symbol(query)}
    if operation == "stock_zh_index_daily_tx":
        return {"symbol": index_symbol(query), "start_date": start_date, "end_date": end_date}
    if operation == "futures_zh_daily_sina":
        # 新浪期货接口不支持日期参数，返回全量历史，由适配器内存过滤
        return {"symbol": futures_symbol(query)}
    raise _unsupported(query, f"unknown operation {operation!r}")


__all__ = ["build_request", "date_window", "futures_symbol", "index_symbol", "stock_symbol"]
