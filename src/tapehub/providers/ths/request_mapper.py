"""同花顺（ths）请求映射（RequestMapper）：标准请求 → REST 端点参数。

接口签名已按官方文档核实（2026-11-13，URL 见 manifest.yaml 头注释）：

- 认证经 ``X-api-key`` 请求头（adapter 层注入，key 不落 URL）；
- 时间边界统一为毫秒 Unix 时间戳显式传参（不依赖上游默认值；上游口径
  Asia/Shanghai，域层 ``BarQuery`` 已保证边界为北京时间 aware datetime）；
- 周期：历史 K 线端外仅日线（``interval=1d``），其余周期在能力目录即不声明
  （operation 二级路由拒绝）；期货/期权日 K 端外仅 ``time_period=day_1``
  （显式传参）；
- 复权：仅 A 股历史 K 线有复权语义，``none/forward/backward`` 直映
  ``none/qfq/hfq``；指数/ETF/期货/期权不传 adjust（指数响应 data.adjust
  固定 null）；
- symbol：标准代码三段中 exchange 段按资产类型查后缀表（只收实证条目，
  见 manifest 头注释），symbol 段除 CZCE 合约 4→3 年月码还原外原样透传
  （contract/option 亦原样，``CN-CFFEX-IO2612-C-4000`` →
  ``IO2612-C-4000.CFE``）；查表无果属命名空间外请求，明确拒绝（不透传
  上游猜错更隐蔽）；
- 快照：A 股/指数走 ``thscodes``（本网关快照为单标的语义，传单个）、
  ETF 走 ``thscode``（单值）；期货/期权快照走分时端点（``session=intraday``、
  ``trade_date=0`` 显式传参，端外仅当前交易日）；
- historical 的 ``offset`` 分页参数文档已列但行为未核实，不启用（单窗口
  期望完整返回，隐式截断待探针）。
"""

import re
from datetime import datetime
from typing import Any

from tapehub.domain.errors import UnsupportedCapabilityError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import Adjust, BarQuery, QuoteQuery
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

# 标准 exchange 段 → thscode 后缀（只收实证条目——文档示例 + 探针价格
# 端点实证；见 manifest 头注释的诚实边界说明）：
# - 2026-11-14 探针第三轮实证 BSE→BJ 价格端点（920000.BJ a-share
#   historical / 899050.BJ a-share-index historical 各 243 行完整，报告
#   20261114T135118+0800）后收编；
# - 期货五所后缀 2026-11-14 探针实证（用户指正形态后复探，EG2701.DCE /
#   BC2612.INE / LC2701.GFE / MA612.CZC 均 code=0 且 68~69 行）——首轮
#   四所 3001 的根因修正：ZCE/GFEX 系探针后缀形态错（ZCE 实为 CZC、
#   GFEX 实为 GFE），DCE/INE 系探针误用已摘牌合约（M2601/SC2606/LC2606
#   均已到期 → 3001，与 CU2601.SHF 已摘牌对照一致）；
# - CZCE 合约年月码 4→3 还原（MA2701 → MA701，MA2701.CZC 3001 /
#   MA701.CZC 5003→复验 code=0 实证，规则与 tqsdk 同构）
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
    },
    "option": {"CFFEX": "CFE"},
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
    （``CN-CZCE-MA2701`` → ``MA701.CZC``）外原样透传（期权行权价等结构
    不做改写）。查表无果属命名空间外请求，明确拒绝。
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
