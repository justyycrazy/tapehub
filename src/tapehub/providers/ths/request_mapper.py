"""同花顺（ths）请求映射（RequestMapper）：标准请求 → REST 端点参数。

映射规则与上游行为定案见 docs/providers/ths.md（唯一出处，本文不复述）。
探针运行记录见 probes/results/PROBE_LOG.md（本地文件，不入库）。
官方文档 URL 见 manifest.yaml 头注释。
"""

import re
from datetime import datetime
from typing import Any

from tapehub.domain.errors import UnsupportedCapabilityError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import Adjust, BarQuery, Instrument, QuoteQuery
from tapehub.domain.timeutil import datetime_to_epoch

# REST 基址（认证经 X-api-key 请求头）
REST_BASE = "https://fuyao.aicubes.cn"

# 历史 K 线周期映射：端外仅日线（其余周期能力目录即不声明）
_INTERVAL_CODES: dict[Interval, str] = {
    Interval.parse("P1D"): "1d",
}

# 复权方式映射（仅 A 股历史 K 线；none/forward/backward 官方枚举）
_ADJUST_CODES: dict[Adjust, str] = {
    Adjust.NONE: "none",
    Adjust.QFQ: "forward",
    Adjust.HFQ: "backward",
}

# 标准 exchange 段 → thscode 后缀（只收实证条目，收录依据见
# docs/providers/ths.md）；CZCE 合约年月码 4→3 还原（MA2701 → MA701，
# 规则与 tqsdk 同构）
_SUFFIXES: dict[str, dict[str, str]] = {
    "stock": {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"},
    "index": {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"},
    "etf": {"SSE": "SH", "SZSE": "SZ"},
    "contract": {
        "SHFE": "SHF",
        "CZCE": "CZC",
        "DCE": "DCE",
        "INE": "INE",
        "GFEX": "GFE",
        "CFFEX": "CFE",
    },
    # 期权三子类（asset_type 三分：commodity-option / index-option /
    # etf-option，2026-11-16 聚焦轮双候选探针实证收编，码式矩阵与落选
    # 否定证据见 docs/providers/ths.md「标的代码映射」）；子类×本所矛盾
    # 组合已由 domain 层词汇表构造期拒绝，映射层只处理合法组合
    "commodity-option": {
        "SHFE": "SHF",
        "INE": "INE",
        "DCE": "DCE",
        "CZCE": "CZC",
        "GFEX": "GFE",
    },
    "index-option": {"CFFEX": "CFE"},
    "etf-option": {"SSE": "SH", "SZSE": "SZ"},
}

# CZCE 合约「品种+2 位年+2 位月」形态（与 tqsdk 同构的 4→3 还原规则）；
# 主连/加权品种码（无年月段）与不规则代码不匹配、原样透传
_CZCE_MONTH_CODE_RE = re.compile(r"^([A-Z]{1,2})(\d{2})(\d{2})$")


def _rewrite_czce_symbol(symbol: str) -> str:
    """CZCE 合约 4 位年月码 → 3 位原生码（MA2701 → MA701）。

    去掉年份十位（MA2701 → MA7 + 01）；非「品种+2 位年+2 位月」形态
    （主连/加权品种码、3 位旧写法、不规则代码）原样透传。
    """
    match = _CZCE_MONTH_CODE_RE.match(symbol)
    if match is None:
        return symbol
    product, year, month = match.groups()
    return f"{product}{year[1]}{month}"

# 期权结构子类统一标准式解析（commodity-option / index-option，域层已
# fullmatch 校验；etf-option 数字合约码原样透传不走本式）。原生写法逐所
# 还原（2026-11-16 聚焦轮八所探针定案，出处 docs/providers/ths.md
# 「标的代码映射」）：SHFE/INE 无连字符直通（CU2701C102000 / SC2701C550）；
# CZCE 3 位月码（SR701C5900——统一式 4 位年月去年份十位）；DCE/GFEX/CFFEX
# 连字符式（M2701-C-3500 / LC2701-C-90000 / IO2612-C-4000）。品种一律大写
# （ths 原生与统一式同为大写）。
_OPTION_SYMBOL_RE = re.compile(r"^([A-Z]+)(\d{2})(\d{2})([CP])(\d+(?:\.\d+)?)$")


def _option_native_symbol(instrument: Instrument) -> str:
    """统一标准式期权符号 → ths 原生合约符号（逐所还原，见上方码式说明）。"""
    matched = _OPTION_SYMBOL_RE.match(instrument.symbol)
    if matched is None:
        # 域层已校验统一式，此分支仅防 model_construct 绕过路径
        raise UnsupportedCapabilityError(
            f"invalid option symbol {instrument.symbol!r}: expected "
            "'{PRODUCT}{YYMM}{C|P}{STRIKE}' (e.g. CU2701C102000)",
            details={"provider": "ths", "instrument": instrument.code},
        )
    product, year, month, cp, strike = matched.groups()
    if instrument.exchange == "CZCE":
        return f"{product}{year[1]}{month}{cp}{strike}"
    if instrument.exchange in ("DCE", "GFEX", "CFFEX"):
        return f"{product}{year}{month}-{cp}-{strike}"
    return f"{product}{year}{month}{cp}{strike}"  # SHFE/INE（无连字符直通）


# bars：operation → 端点路径（上游历史 K 线/日 K）
_BAR_PATHS: dict[str, str] = {
    "a_share_historical": "/api/a-share/prices/historical",
    "index_historical": "/api/a-share-index/prices/historical",
    "fund_historical": "/api/fund/market/historical",
    "futures_daily": "/api/futures/prices/daily",
    "options_daily": "/api/options/prices/daily",
}

# quotes：operation → 端点路径（快照 / 分时末点）
_QUOTE_PATHS: dict[str, str] = {
    "a_share_snapshot": "/api/a-share/prices/snapshot",
    "index_snapshot": "/api/a-share-index/prices/snapshot",
    "fund_snapshot": "/api/fund/market/snapshot",
    "futures_snapshot": "/api/futures/prices/intraday",
    "options_snapshot": "/api/options/prices/intraday",
}


def ths_symbol(query: BarQuery | QuoteQuery) -> str:
    """标准标的 → 完整 thscode（如 CN-SSE-600519 → "600519.SH"）。

    exchange 段按资产类型查后缀表；symbol 段除 CZCE 合约 4→3 年月码还原
    （``CN-CZCE-MA2701`` → ``MA701.CZC``）与期权结构子类统一式还原
    （``_option_native_symbol``）外原样透传。查表无果属命名空间外请求，
    明确拒绝。
    """
    asset_type = query.asset_type.value
    suffixes = _SUFFIXES.get(asset_type)
    if suffixes is None:
        raise UnsupportedCapabilityError(
            f"ths does not support asset type {asset_type}.",
            details={
                "provider": "ths",
                "instrument": query.instrument.code,
                "asset_type": asset_type,
            },
        )
    suffix = suffixes.get(query.instrument.exchange)
    if suffix is None:
        expected = "/".join(sorted(suffixes))
        raise UnsupportedCapabilityError(
            f"ths expects exchange segment to be one of [{expected}] "
            f"for asset type {asset_type}, got {query.instrument.exchange!r}.",
            details={
                "provider": "ths",
                "instrument": query.instrument.code,
            },
        )
    symbol = query.instrument.symbol
    if query.instrument.exchange == "CZCE" and asset_type == "contract":
        symbol = _rewrite_czce_symbol(symbol)
    elif asset_type in ("commodity-option", "index-option"):
        symbol = _option_native_symbol(query.instrument)
    return f"{symbol}.{suffix}"


def interval_code(interval: Interval) -> str:
    code = _INTERVAL_CODES.get(interval)
    if code is None:
        raise UnsupportedCapabilityError(
            f"ths does not support interval {interval.code}.",
            details={"interval": interval.code},
        )
    return code


def adjust_code(adjust: Adjust) -> str:
    code = _ADJUST_CODES.get(adjust)
    if code is None:
        raise UnsupportedCapabilityError(
            f"ths does not support adjust {adjust.value}.",
            details={"adjust": adjust.value},
        )
    return code


def _epoch_ms(value: datetime) -> int:
    """北京时间边界（域层保证 aware）→ 毫秒 Unix 时间戳。"""
    return datetime_to_epoch(value) // 1_000_000


def build_request(operation: str, query: BarQuery) -> tuple[str, dict[str, Any]]:
    """构造历史 K 线/日 K 请求 ``(url, params)``；所有相关参数显式传递。

    - ``*_historical``：``thscode`` + ``interval`` + ``start``/``end``（毫秒）
      + ``adjust``（仅 A 股端点有该参数）；
    - ``futures_daily``/``options_daily``：``thscode`` + ``start``/``end`` +
      ``time_period=day_1``（端外仅日线，显式传参不依赖上游默认值）。
    """
    url = _BAR_PATHS.get(operation)
    if url is None:
        raise UnsupportedCapabilityError(
            f"ths has no REST endpoint for operation {operation!r}.",
            details={"provider": "ths", "operation": operation},
        )
    code = ths_symbol(query)
    params: dict[str, Any] = {
        "start": _epoch_ms(query.start),
        "end": _epoch_ms(query.end),
    }
    if operation in ("a_share_historical", "index_historical", "fund_historical"):
        params["thscode"] = code
        params["interval"] = interval_code(query.interval)
        if operation == "a_share_historical":
            params["adjust"] = adjust_code(query.adjust)
    else:  # futures_daily / options_daily
        params["thscode"] = code
        params["time_period"] = "day_1"
    return f"{REST_BASE}{url}", params


def build_quote_request(operation: str, query: QuoteQuery) -> tuple[str, dict[str, Any]]:
    """构造实时快照请求 ``(url, params)``。

    - ``a_share_snapshot``/``index_snapshot``：批量参数 ``thscodes``
      （本网关快照为单标的语义，传单个完整 thscode）；
    - ``fund_snapshot``：单值参数 ``thscode``（仅 ETF）；
    - ``futures_snapshot``/``options_snapshot``：分时端点取末点作快照——
      ``session=intraday``、``trade_date=0`` 显式传参（端外仅当前交易日）。
    """
    url = _QUOTE_PATHS.get(operation)
    if url is None:
        raise UnsupportedCapabilityError(
            f"ths has no REST endpoint for operation {operation!r}.",
            details={"provider": "ths", "operation": operation},
        )
    code = ths_symbol(query)
    params: dict[str, Any]
    if operation in ("a_share_snapshot", "index_snapshot"):
        params = {"thscodes": code}
    elif operation == "fund_snapshot":
        params = {"thscode": code}
    else:  # futures_snapshot / options_snapshot：分时端点（当日口径）
        params = {"thscode": code, "session": "intraday", "trade_date": "0"}
    return f"{REST_BASE}{url}", params


__all__ = [
    "REST_BASE",
    "build_quote_request",
    "build_request",
    "interval_code",
    "ths_symbol",
]
