"""Twelve Data 请求映射（RequestMapper）：标准请求 → td.time_series / WS 订阅参数。

接口签名均已按官方文档与探针实证核实：

- ``td.time_series(symbol, interval, outputsize, start_date, end_date).as_json()``
  （REST，历史 K 线；美股额外显式传 ``exchange`` 与 ``adj``，见下）；
- 实时快照/快照流走 WS 订阅（``wss://ws.twelvedata.com/v1/quotes/price``），
  subscribe/unsubscribe 的 symbol 即 ``td_symbol`` 映射结果（2026-11-05
  探针实证，报告 probes/results/twelvedata/ws-behavior-20261105T221000+0800.md
  不入库）；REST /quote 路径已退役；
- symbol：外汇/加密货币为斜杠分隔货币对（``EUR/USD``、``BTC/USD``），
  美股为交易所原生 ticker（``AAPL``、``SPY``）原样透传，exchange 段
  白名单 NYSE/NASDAQ/CBOE（OTC 粉单与 IEX 不接入，2026-11-05 目录探针
  实证分布见 manifest 注释；美股显式传 ``exchange`` 消歧——官方文档
  推荐目录 symbol 配合 exchange/mic_code 定位）；
- interval 枚举 1min/5min/15min/30min/45min/1h/2h/4h/8h/1day/1week/1month，
  本适配器声明全部 12 个周期（45min/2h/4h/8h 经探针实测后补充声明）；
- datetime 墙钟口径按资产类型区分（外汇 Sydney / 加密 UTC 实测于
  2026-11-04；美股 America/New_York 出处
  https://twelvedata.com/exchanges/stocks + meta.exchange_timezone 实测，
  2026-11-05）：start_date/end_date 无时区后缀，格式 YYYY-MM-DD 或
  YYYY-MM-DDTHH:MM:SS，故北京时间边界须先转换为对应口径的墙钟再传递；
- adj（仅美股传，2026-11-06 探针实证）：``none``→``"none"``（原始价）、
  ``qfq``→``"splits"``（仅拆股复权——上游 ``adj=dividends`` 实测全年零
  效果，分红复权未生效，与国内前复权口径不同，README 已标注）；
  外汇/加密货币无复权概念不传；
- outputsize 范围 1~5000：显式传最大值 5000（不依赖上游默认 30）。
"""

from datetime import datetime
from typing import Any

from tapehub.domain.errors import UnsupportedCapabilityError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import Adjust, AssetType, BarQuery, QuoteQuery
from tapehub.providers.twelvedata.response_mapper import display_zone

# 单次请求上限（官方文档：outputsize 范围 1~5000）
UPSTREAM_MAX_BARS = 5000

# 区间无数据指纹（2026-11-04 数据探针实测核实，报告 20261104T065529Z）：
# 查询区间内无数据时上游返回 400 BadRequestError，报文含此句，而非空列表
UPSTREAM_NO_DATA_FINGERPRINT = "No data is available on the specified dates"

# WS 端点（实时快照与快照流共用；API key 经查询参数注入，绝不写日志）
WS_ENDPOINT = "wss://ws.twelvedata.com/v1/quotes/price"

_INTERVAL_CODES: dict[Interval, str] = {
    Interval.parse("PT1M"): "1min",
    Interval.parse("PT5M"): "5min",
    Interval.parse("PT15M"): "15min",
    Interval.parse("PT30M"): "30min",
    Interval.parse("PT45M"): "45min",
    Interval.parse("PT1H"): "1h",
    Interval.parse("PT2H"): "2h",
    Interval.parse("PT4H"): "4h",
    Interval.parse("PT8H"): "8h",
    Interval.parse("P1D"): "1day",
    Interval.parse("P1W"): "1week",
    Interval.parse("P1M"): "1month",
}

# 统一复权 → 上游 adj 取值（仅美股资产类型使用；口径见模块 docstring）
_ADJUST_CODES: dict[Adjust, str] = {
    Adjust.NONE: "none",
    Adjust.QFQ: "splits",
}

# 标准代码中 exchange 段的约定命名空间（美股为白名单集合：
# NYSE/NASDAQ/CBOE，经目录探针 + CBOE 准入探针核实）
_ASSET_EXCHANGES: dict[AssetType, frozenset[str]] = {
    AssetType.FOREX: frozenset({"OTC"}),
    AssetType.CRYPTO: frozenset({"CRYPTO"}),
    AssetType.STOCK: frozenset({"NYSE", "NASDAQ", "CBOE"}),
    AssetType.ETF: frozenset({"NYSE", "NASDAQ", "CBOE"}),
}

# 需要显式传 exchange 与 adj 的资产类型（美股；货币对不传）
_US_EQUITY_TYPES = frozenset({AssetType.STOCK, AssetType.ETF})


def td_symbol(query: BarQuery | QuoteQuery) -> str:
    """标准标的 → Twelve Data 上游 symbol（如 GLOBAL-OTC-EUR/USD → EUR/USD）。"""
    allowed = _ASSET_EXCHANGES.get(query.asset_type)
    if allowed is None:
        raise UnsupportedCapabilityError(
            f"Twelve Data does not support asset type {query.asset_type.value}.",
            details={
                "provider": "twelvedata",
                "instrument": query.instrument.code,
                "asset_type": query.asset_type.value,
            },
        )
    if query.instrument.exchange not in allowed:
        expected = "/".join(sorted(allowed))
        raise UnsupportedCapabilityError(
            f"Twelve Data expects exchange segment to be one of [{expected}] "
            f"for asset type {query.asset_type.value}, "
            f"got {query.instrument.exchange!r}.",
            details={
                "provider": "twelvedata",
                "instrument": query.instrument.code,
            },
        )
    return query.instrument.symbol


def interval_code(interval: Interval) -> str:
    code = _INTERVAL_CODES.get(interval)
    if code is None:
        raise UnsupportedCapabilityError(
            f"Twelve Data does not support interval {interval.code}.",
            details={"interval": interval.code},
        )
    return code


def adjust_code(adjust: Adjust) -> str:
    """统一复权 → 上游 adj 取值（hfq 上游无等价物，能力目录即不声明）。"""
    code = _ADJUST_CODES.get(adjust)
    if code is None:
        raise UnsupportedCapabilityError(
            f"Twelve Data does not support adjust {adjust.value}.",
            details={"adjust": adjust.value},
        )
    return code


def _boundary_code(value: datetime, zone: object) -> str:
    """边界（北京时间，域层保证带时区）→ 上游口径墙钟 YYYY-MM-DDTHH:MM:SS。"""
    return value.astimezone(zone).strftime("%Y-%m-%dT%H:%M:%S")  # type: ignore[attr-defined]


def build_request(query: BarQuery) -> dict[str, Any]:
    """构造 td.time_series 的关键字参数；所有相关参数显式传递。

    不依赖上游默认值：interval 默认参数缺省时上游行为未知、
    outputsize 默认 30、美股 adj 默认 splits，均显式指定；边界从北京
    时间转换为上游按资产类型解释的墙钟口径（外汇 Sydney、加密货币 UTC、
    美股 New York）。美股额外显式传 exchange（同名 ticker 消歧）。
    """
    zone = display_zone(query.asset_type)
    kwargs: dict[str, Any] = {
        "symbol": td_symbol(query),
        "interval": interval_code(query.interval),
        "outputsize": UPSTREAM_MAX_BARS,
        "start_date": _boundary_code(query.start, zone),
        "end_date": _boundary_code(query.end, zone),
    }
    if query.asset_type in _US_EQUITY_TYPES:
        kwargs["exchange"] = query.instrument.exchange
        kwargs["adj"] = adjust_code(query.adjust)
    return kwargs


__all__ = [
    "UPSTREAM_MAX_BARS",
    "UPSTREAM_NO_DATA_FINGERPRINT",
    "WS_ENDPOINT",
    "adjust_code",
    "build_request",
    "interval_code",
    "td_symbol",
]
