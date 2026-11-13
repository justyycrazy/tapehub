"""Tiingo 请求映射（RequestMapper）：标准请求 → REST 端点参数 / WS 订阅参数。

接口签名已按官方文档核实（2026-11-13，URL 见 manifest.yaml 头注释）：

- 历史K线 REST（``Authorization: Token <key>`` 请求头认证，token 不落 URL）：
  加密 ``GET /tiingo/crypto/prices``（多 ticker 查询参数 ``tickers``）；
  外汇 ``GET /tiingo/fx/<ticker>/prices``（每 ticker 独立路径端点）；
  两端点均显式传 ``startDate``/``endDate``/``resampleFreq``（不依赖上游默认值）；
- ``resampleFreq`` 合法格式 ``<n>min|hour|day``、最小 ``1min``，上游**无**
  周/月线——声明集见 manifest（其余格式合法值待探针实证后扩容）；
- ``startDate``/``endDate`` 官方文档示例为 ``YYYY-MM-DD``，同时接受完整
  ISO 时间戳：适配器统一传 UTC 墙钟 ``YYYY-MM-DDTHH:MM:SS``（无时区后缀）
  保留日内精度——加密货币 ``date`` 字段官方口径为 UTC；外汇墙钟口径未
  核实，首版同按 UTC 解释（待探针实证后修正）；
- symbol：标准代码三段中 exchange 段白名单（外汇 ``OTC``、加密 ``CRYPTO``），
  symbol 段去斜杠转小写（``EUR/USD`` → ``eurusd``、``BTC/USD`` → ``btcusd``，
  官方示例均为小写连写）；
- WS 实时快照/快照流端点与订阅级别（官方文档 websockets 页）：
  crypto ``wss://api.tiingo.com/crypto`` thresholdLevel=2（top-of-book +
  成交）、forex ``wss://api.tiingo.com/fx`` thresholdLevel=7（top-of-book）；
  订阅/退订帧构造见 runtime（authorization 随帧传递，绝不写日志）。
"""

from datetime import datetime, timezone

from tapehub.domain.errors import UnsupportedCapabilityError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import BarQuery, QuoteQuery

# REST 基址（历史K线；认证经 Authorization: Token 请求头）
REST_BASE = "https://api.tiingo.com"

# WS 端点（实时快照与快照流共用；按资产类型分流，token 随 subscribe 帧传递）
WS_CRYPTO_ENDPOINT = "wss://api.tiingo.com/crypto"
WS_FX_ENDPOINT = "wss://api.tiingo.com/fx"

# WS 订阅级别（官方文档 websockets 页）：crypto 2 = 逐交易所 top-of-book +
# 成交更新；forex 7 = top-of-book 更新（bid/ask 或 size 变动）
WS_CRYPTO_THRESHOLD_LEVEL = 2
WS_FX_THRESHOLD_LEVEL = 7

# 时间边界统一解释为 UTC 墙钟（2026-11-13 探针实证：crypto date 带 +00:00、
# forex date 带 Z，两端点均为 UTC 口径）
BOUNDARY_ZONE = timezone.utc

# 区间早于上游档案起点的错误指纹（2026-11-13 探针实证 + 目录外受控复现）：
# fx 端点档案硬边界 2020-01-01（UTC 墙钟），窗口越界返回两种 400 变体——
# end < 2020 → {"detail": "End date must be >= 2020-01-01"}；start < 2020
# 且 end >= 2020 → {"detail": "Start date must be >= 2020-01-01"}。主处理
# 路径为 FX_ARCHIVE_FLOOR 钳制（见下），本指纹归一化仅作防御兜底（上游
# 若上移边界，指纹失配即报错 loud 提示更新）；crypto 端点对历史外窗口
# 优雅返回 200 []，无需处理
UPSTREAM_NO_DATA_FINGERPRINT = "must be >= 2020-01-01"

# fx 端点档案硬边界（UTC 墙钟字符串，与 _boundary_code 输出同构，可按
# 字典序比较）：上游对越界窗口拒绝而非裁剪，适配器对 fx 请求钳制——
# startDate 早于档案起点时钳到起点（窗口与档案有交集则如实返回边界后
# 数据，闭区间复核兜底）；完全越界（endDate 亦早于起点）的窗口由适配器
# 直接返回空批不发请求。上游档案若前移（边界下移），钳制值需经探针
# 复核后人工更新（case notes 记录变更探针方法）
FX_ARCHIVE_FLOOR = "2020-01-01T00:00:00"

_INTERVAL_CODES: dict[Interval, str] = {
    Interval.parse("PT1M"): "1min",
    Interval.parse("PT5M"): "5min",
    Interval.parse("PT15M"): "15min",
    Interval.parse("PT30M"): "30min",
    Interval.parse("PT1H"): "1hour",
    Interval.parse("P1D"): "1day",
}

# 标准代码中 exchange 段的约定命名空间（与 twelvedata 同构：
# 外汇聚合报价 OTC、加密聚合对 CRYPTO）
_ASSET_EXCHANGES: dict[str, frozenset[str]] = {
    "forex": frozenset({"OTC"}),
    "crypto": frozenset({"CRYPTO"}),
}

# WS 端点与订阅级别（按资产类型分流；查表 key 用 asset_type.value）
_WS_ROUTES: dict[str, tuple[str, int]] = {
    "crypto": (WS_CRYPTO_ENDPOINT, WS_CRYPTO_THRESHOLD_LEVEL),
    "forex": (WS_FX_ENDPOINT, WS_FX_THRESHOLD_LEVEL),
}


def tiingo_symbol(query: BarQuery | QuoteQuery) -> str:
    """标准标的 → Tiingo 上游 ticker（如 GLOBAL-OTC-EUR/USD → "eurusd"）。"""
    allowed = _ASSET_EXCHANGES.get(query.asset_type.value)
    if allowed is None:
        raise UnsupportedCapabilityError(
            f"Tiingo does not support asset type {query.asset_type.value}.",
            details={
                "provider": "tiingo",
                "instrument": query.instrument.code,
                "asset_type": query.asset_type.value,
            },
        )
    if query.instrument.exchange not in allowed:
        expected = "/".join(sorted(allowed))
        raise UnsupportedCapabilityError(
            f"Tiingo expects exchange segment to be one of [{expected}] "
            f"for asset type {query.asset_type.value}, "
            f"got {query.instrument.exchange!r}.",
            details={
                "provider": "tiingo",
                "instrument": query.instrument.code,
            },
        )
    # 官方 ticker 均为小写连写（eurusd/btcusd）：去斜杠统一小写
    return query.instrument.symbol.replace("/", "").lower()


def ws_route(asset_type_value: str) -> tuple[str, int]:
    """资产类型 → (WS 端点, thresholdLevel)；未声明资产类型明确拒绝。"""
    route = _WS_ROUTES.get(asset_type_value)
    if route is None:
        raise UnsupportedCapabilityError(
            f"Tiingo does not support asset type {asset_type_value}.",
            details={"provider": "tiingo", "asset_type": asset_type_value},
        )
    return route


def interval_code(interval: Interval) -> str:
    code = _INTERVAL_CODES.get(interval)
    if code is None:
        raise UnsupportedCapabilityError(
            f"Tiingo does not support interval {interval.code}.",
            details={"interval": interval.code},
        )
    return code


def _boundary_code(value: datetime) -> str:
    """边界（北京时间，域层保证带时区）→ UTC 墙钟 YYYY-MM-DDTHH:MM:SS。"""
    return value.astimezone(BOUNDARY_ZONE).strftime("%Y-%m-%dT%H:%M:%S")


def build_request(operation: str, query: BarQuery) -> tuple[str, dict[str, str]]:
    """构造 REST 请求 ``(url, params)``；所有相关参数显式传递。

    - ``fx_prices``：GET /tiingo/fx/<ticker>/prices（ticker 走路径）；
    - ``crypto_prices``：GET /tiingo/crypto/prices（ticker 走查询参数）；
    - ``startDate``/``endDate``/``resampleFreq`` 均显式传（不依赖上游默认值：
      resampleFreq 缺省时上游返回原始频度而非声明周期口径）。
    """
    ticker = tiingo_symbol(query)
    code = interval_code(query.interval)
    start_code = _boundary_code(query.start)
    if operation == "fx_prices":
        url = f"{REST_BASE}/tiingo/fx/{ticker}/prices"
        # fx 档案硬边界钳制（见 FX_ARCHIVE_FLOOR 注释）：起点早于档案时
        # 钳到档案起点（同格式字符串字典序 = 时间序）
        params: dict[str, str] = {"startDate": max(start_code, FX_ARCHIVE_FLOOR)}
    elif operation == "crypto_prices":
        url = f"{REST_BASE}/tiingo/crypto/prices"
        params = {"tickers": ticker, "startDate": start_code}
    else:
        raise UnsupportedCapabilityError(
            f"Tiingo has no REST endpoint for operation {operation!r}.",
            details={"provider": "tiingo", "operation": operation},
        )
    params["endDate"] = _boundary_code(query.end)
    params["resampleFreq"] = code
    return url, params


__all__ = [
    "BOUNDARY_ZONE",
    "FX_ARCHIVE_FLOOR",
    "REST_BASE",
    "UPSTREAM_NO_DATA_FINGERPRINT",
    "WS_CRYPTO_ENDPOINT",
    "WS_CRYPTO_THRESHOLD_LEVEL",
    "WS_FX_ENDPOINT",
    "WS_FX_THRESHOLD_LEVEL",
    "build_request",
    "interval_code",
    "tiingo_symbol",
    "ws_route",
]
