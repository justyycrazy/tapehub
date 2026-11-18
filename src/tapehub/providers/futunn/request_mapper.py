"""富途（futunn）请求映射（RequestMapper）：标准请求 → REST 端点参数。

映射规则与上游行为定案见 docs/providers/futunn.md（唯一出处，本文不复述）。
官方文档 URL 见 manifest.yaml 头注释。
"""

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from tapehub.domain.errors import UnsupportedCapabilityError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import Adjust, BarQuery, QuoteQuery

# REST 基址（认证经签名请求头）
REST_BASE = "https://webapi.futunn.com"

# 单次返回行数上限（官方文档：num 默认 370，最大 370）
PAGE_SIZE = 370

# bars：历史 K 线端点（code 为上游前缀写法，如 HK.00700）
_HISTORY_PATH_TEMPLATE = "/api/v1.0/quote/{code}/history-kline"
# quotes：实时快照端点（POST，code_list 单次最多 400）
_QUOTE_PATH = "/api/v1.0/quote/stock-quote"

# 历史 K 线周期映射（ktype 数字枚举，官方文档核实）
_INTERVAL_KTYPES: dict[Interval, int] = {
    Interval.parse("PT1M"): 1,
    Interval.parse("PT5M"): 6,
    Interval.parse("PT15M"): 7,
    Interval.parse("PT30M"): 8,
    Interval.parse("PT1H"): 9,  # 上游 60 分
    Interval.parse("P1D"): 2,
    Interval.parse("P1W"): 3,
    Interval.parse("P1M"): 4,
}

# 分钟周期集合：上游 end 以该日 00:00 为截止、不含当日（end 日期 +1 天补偿）
_MINUTE_INTERVALS: frozenset[Interval] = frozenset(
    {Interval.parse(code) for code in ("PT1M", "PT5M", "PT15M", "PT30M", "PT1H")}
)

# 复权方式映射（autype 0/1/2，官方文档核实）
_ADJUST_AUTYPES: dict[Adjust, int] = {
    Adjust.NONE: 0,
    Adjust.QFQ: 1,
    Adjust.HFQ: 2,
}

# 上游市场前缀：HK/US 为市场级（美股前缀不区分交易所，NASDAQ/NYSE/AMEX
# 统一 US）；CN 按资产类型 × 交易所收窄（上游品类表 SH/SZ/BJ 支持正股/
# 指数、ETF 仅 SH/SZ——BJ 无 ETF 实证）。美股仅接上市交易所（OTC/IEX
# 上游不支持获取，白名单外拒绝）。market/exchange 一致性由域层词汇表
# 校验，此处只按接入范围收窄（docs/providers/futunn.md「标的代码映射」）。
_MARKET_PREFIXES = {"HK": "HK", "US": "US"}
_US_LISTED_EXCHANGES = frozenset({"NYSE", "NASDAQ", "AMEX"})
_CN_PREFIXES_BY_ASSET_TYPE: dict[str, dict[str, str]] = {
    "stock": {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"},
    "index": {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"},
    "etf": {"SSE": "SH", "SZSE": "SZ"},
}

# 标的市场时区（请求边界日期换算口径；上游时间戳为绝对时刻，归一化由领域层完成）
_MARKET_ZONES: dict[str, ZoneInfo] = {
    "CN": ZoneInfo("Asia/Shanghai"),
    "HK": ZoneInfo("Asia/Hong_Kong"),
    "US": ZoneInfo("America/New_York"),
}


@dataclass(frozen=True)
class UpstreamRequest:
    """经签名直连的上游请求描述。

    ``query_string`` 与 ``url`` 拼接、签名原文共用同一字符串（签名一致性
    单点保证）；``body`` 为原始请求体字节（POST JSON），GET 为 None。
    """

    method: str
    request_path: str
    query_string: str
    body: bytes | None

    @property
    def url(self) -> str:
        if self.query_string:
            return f"{REST_BASE}{self.request_path}?{self.query_string}"
        return f"{REST_BASE}{self.request_path}"


def futu_code(query: BarQuery | QuoteQuery) -> str:
    """标准标的 → 上游前缀代码（如 HK-HKEX-00700 → "HK.00700"、
    CN-SSE-600000 → "SH.600000"、US-NASDAQ-AAPL → "US.AAPL"）。

    US/HK 按市场段取前缀（美股仍校验上市交易所白名单——OTC 柜台上游
    不支持获取）；CN 按资产类型 × 交易所查子表（品类语义真实：ETF 仅
    SH/SZ）。白名单外明确拒绝，不透传上游猜错更隐蔽；数字代码原样
    透传、港股短码零填充 5 位（上游写法 00700）、美股 ticker 原样
    （域层已大写归一）。
    """
    market = query.instrument.market
    if market == "CN":
        asset_type = query.asset_type.value
        prefixes = _CN_PREFIXES_BY_ASSET_TYPE.get(asset_type)
        if prefixes is None:
            raise UnsupportedCapabilityError(
                f"futunn does not support asset type {asset_type}.",
                details={
                    "provider": "futunn",
                    "instrument": query.instrument.code,
                    "asset_type": asset_type,
                },
            )
        prefix = prefixes.get(query.instrument.exchange)
        if prefix is None:
            expected = "/".join(sorted(prefixes))
            raise UnsupportedCapabilityError(
                f"futunn expects exchange segment to be one of [{expected}] "
                f"for asset type {asset_type}, got {query.instrument.exchange!r}.",
                details={
                    "provider": "futunn",
                    "instrument": query.instrument.code,
                },
            )
    elif market in _MARKET_PREFIXES:
        if market == "US" and query.instrument.exchange not in _US_LISTED_EXCHANGES:
            raise UnsupportedCapabilityError(
                f"futunn does not support US exchange {query.instrument.exchange!r} "
                "(OTC 柜台上游不支持获取).",
                details={
                    "provider": "futunn",
                    "instrument": query.instrument.code,
                },
            )
        prefix = _MARKET_PREFIXES[market]
    else:
        raise UnsupportedCapabilityError(
            f"futunn does not support market {market!r}.",
            details={
                "provider": "futunn",
                "instrument": query.instrument.code,
            },
        )
    symbol = query.instrument.symbol
    if prefix in ("SH", "SZ", "BJ", "HK"):
        if not symbol.isdigit():
            raise UnsupportedCapabilityError(
                f"futunn expects digit-only symbol for market prefix {prefix}, "
                f"got {symbol!r}.",
                details={
                    "provider": "futunn",
                    "instrument": query.instrument.code,
                },
            )
        if prefix == "HK":
            return f"HK.{symbol.zfill(5)}"  # 上游港股 5 位数字代码
        return f"{prefix}.{symbol}"
    if "/" in symbol:
        # `/` 会拼入请求 path（{code}/history-kline）形成多余路径段，上游
        # 行为未定义——显式拒绝优于静默畸形。BRK/A 类 class-share 记法的
        # 上游原生写法未探针核实（开放问题见 docs/providers/futunn.md），
        # 核实前不接受任何含 `/` 的替代表示
        raise UnsupportedCapabilityError(
            f"futunn does not accept '/' in US symbol {symbol!r} "
            "(class-share notation like 'BRK/A' is unverified upstream); "
            "see docs/providers/futunn.md.",
            details={
                "provider": "futunn",
                "instrument": query.instrument.code,
            },
        )
    return f"US.{symbol}"  # 美股 ticker 原样透传（上游不区分交易所）


def interval_code(interval: Interval) -> int:
    code = _INTERVAL_KTYPES.get(interval)
    if code is None:
        raise UnsupportedCapabilityError(
            f"futunn does not support interval {interval.code}.",
            details={"interval": interval.code},
        )
    return code


def adjust_code(adjust: Adjust) -> int:
    code = _ADJUST_AUTYPES.get(adjust)
    if code is None:
        raise UnsupportedCapabilityError(
            f"futunn does not support adjust {adjust.value}.",
            details={"adjust": adjust.value},
        )
    return code


def _local_date(value: datetime, zone: ZoneInfo) -> date:
    """北京时间边界（域层保证 aware）→ 标的市场时区日历日期。"""
    return value.astimezone(zone).date()


def build_request(
    operation: str, query: BarQuery, *, page_token: str | None = None
) -> UpstreamRequest:
    """构造历史 K 线请求；所有相关参数显式传递（不依赖上游默认值）。

    - ``ktype``/``autype``/``num`` 显式传参（上游 autype 缺省 1=前复权、
      num 缺省 370，均不依赖）；
    - 边界日期按标的市场时区换算；分钟 K 线 end 日期 +1 天（上游 end 以
      该日 00:00 截止不含当日，闭区间复核在适配器完成）；
    - ``page_token`` 有值时置于查询串末位（续页参数不变原则：start/ktype/
      autype/num 逐字节一致，15 分钟有效）。
    """
    if operation != "history_kline":
        raise UnsupportedCapabilityError(
            f"futunn has no REST endpoint for operation {operation!r}.",
            details={"provider": "futunn", "operation": operation},
        )
    code = futu_code(query)
    request_path = _HISTORY_PATH_TEMPLATE.format(code=code)
    zone = _MARKET_ZONES.get(query.instrument.market)
    if zone is None:
        raise UnsupportedCapabilityError(
            f"futunn does not support market {query.instrument.market!r}.",
            details={
                "provider": "futunn",
                "instrument": query.instrument.code,
            },
        )
    start_date = _local_date(query.start, zone)
    end_date = _local_date(query.end, zone)
    if query.interval in _MINUTE_INTERVALS:
        end_date += timedelta(days=1)  # 上游分钟线 end 不含当日（00:00 截止）
    params: list[tuple[str, str]] = [
        ("start", start_date.isoformat()),
        ("end", end_date.isoformat()),
        ("ktype", str(interval_code(query.interval))),
        ("autype", str(adjust_code(query.adjust))),
        ("num", str(PAGE_SIZE)),
    ]
    if page_token:
        params.append(("page_token", page_token))
    return UpstreamRequest(
        method="GET",
        request_path=request_path,
        query_string=urlencode(params),
        body=None,
    )


def build_quote_request(operation: str, query: QuoteQuery) -> UpstreamRequest:
    """构造实时快照请求（POST JSON body，code_list 单标的传 1 个）。

    body 序列化采用紧凑分隔符与官方示例形态一致；同一字节串同时用于
    签名（body_part=SHA256 hex）与发送，保证签名一致性。
    """
    if operation != "stock_quote":
        raise UnsupportedCapabilityError(
            f"futunn has no REST endpoint for operation {operation!r}.",
            details={"provider": "futunn", "operation": operation},
        )
    code = futu_code(query)
    body = json.dumps({"code_list": [code]}, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return UpstreamRequest(method="POST", request_path=_QUOTE_PATH, query_string="", body=body)


__all__ = [
    "PAGE_SIZE",
    "REST_BASE",
    "UpstreamRequest",
    "adjust_code",
    "build_quote_request",
    "build_request",
    "futu_code",
    "interval_code",
]
