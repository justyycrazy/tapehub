"""富途 OpenAPI（futunn）适配器（已实现，纯 REST 直连 + AppKey 签名）。

约定：

- futunn 无官方 Python SDK：``httpx`` 同步客户端（可选依赖）只在
  ``_call_upstream`` 内部延迟导入，未安装时返回 PROVIDER_NOT_IMPLEMENTED，
  不影响服务启动；同步调用经 ``call_in_thread`` 隔离到线程池；
- 凭据经环境变量注入（pydantic-settings）：``TAPEHUB_FUTUNN_APP_KEY``
  （AppKey ID）与 ``TAPEHUB_FUTUNN_PRIVATE_KEY_PATH``（Ed25519/RSA 私钥
  PEM 路径），两者全部非空才启用（凭据驱动注册）；签名头经 signing 模块
  构造（「方式二：传统 API Key」，签名算法按私钥类型自动检测），AppKey/
  私钥绝不写入日志、manifest 或异常响应；
- 统一信封（官方约定：业务结果含错误 HTTP 200，经 ret_code 表达）：
  ``-11``（rate_limited）→ UPSTREAM_RATE_LIMITED（携 Retry-After，如有）；
  其余非 0（-3/-7/-8/-12/-13/-12006 等）→ UPSTREAM_ERROR 携
  upstream_code/upstream_message（ret_code 缺失时防御读 code 键——新旧
  两种封装并存）；HTTP >=400 与 JSON 解析失败为防御性 UPSTREAM_ERROR
  （携 status_code）——应用层对 TapehubError 直接透传，SDK 异常不穿透 API；
- bars 分页：官方文档 num 最大 370、page_token 续页；**2026-11-14 探针
  实证（报告 20261114T111229+0800）：num 参数被上游忽略、单查询恒返回
  窗口起点起 ≈1000 行、pagination.has_more 恒 False 且无 next_page_token
  ——page_token 分页实际不可用**；适配器分页循环与 _MAX_PAGES=15 保留为
  防御（上游若恢复签发 token 即生效）；触页数上限且最早一根晚于 start
  判 partial=True 的防御判定保留，但**分钟族窗口 >≈1000 根时上游静默
  截断、partial 无法可靠判定（恒 false）**——人工裁决 2026-11-14：README
  如实说明、partial 维持（ths 先例），分钟族建议窄窗口使用；
- 分钟 K 线 end 语义：上游 end 以该日 00:00 截止不含当日，request_mapper
  对分钟周期 end 日期 +1 天补偿，闭区间 [start, end] 精确复核在适配器
  完成（日/周/月 K 线 end 含当日，不补偿）；
- 快照为纯 REST 单次调用语义：无首包超时/QUOTE_NOT_READY（与 ths/
  alphafeed/tickflow 一致）；上游 REST 无订阅接口（WS 行情推送专区本期
  不接入，第一版不做流式——用户拍板 2026-11-14），不实现 stream_quote
  （流式请求返回 UNSUPPORTED_CAPABILITY）；
- X-Timestamp 与服务端偏差 >5s 报 -12006（HTTP 401 鉴权失败）：第一版
  不做自动校时（依赖宿主机时钟）；校时端点 GET /api/v1.0/server-time
  待探针实证后按需补充；
- 单标的快照 code 匹配：上游对 code_list 中非法 code 静默丢弃（ret_code
  仍 0），匹配行缺失属无有效行情（UPSTREAM_ERROR）；
- 统一分页由共享 ``paginate_bars`` 完成，bars/quotes 时间戳统一北京时间
  归一化由领域层模型完成。
"""

from datetime import datetime
from typing import Any

from tapehub.domain.errors import (
    ProviderNotImplementedError,
    UpstreamError,
    UpstreamRateLimitedError,
)
from tapehub.domain.models import CN_TIMEZONE, Bar, BarBatch, BarQuery, Quote, QuoteQuery
from tapehub.providers.base import BaseProviderAdapter, call_in_thread
from tapehub.providers.futunn.request_mapper import (
    PAGE_SIZE,
    UpstreamRequest,
    build_quote_request,
    build_request,
    futu_code,
)
from tapehub.providers.futunn.response_mapper import to_standard_bars, to_standard_quote
from tapehub.providers.futunn.signing import build_signed_headers, load_private_key_pem
from tapehub.providers.pagination import paginate_bars

# httpx 客户端超时（秒）：应用层 upstream_timeout_seconds（默认 30s）为
# 整体兜底，客户端自身超时取更小的保守值，避免长挂线程占用 worker
_HTTP_TIMEOUT_SECONDS = 20.0

# 信封业务码：限频（官方错误码表，-11 rate_limited）→ UPSTREAM_RATE_LIMITED
_UPSTREAM_RATE_LIMITED_CODE = -11

# bars 分页页数上限（防御性：page_token 15 分钟有效、同 token 循环异常时
# 兜底终止）；UPSTREAM_MAX_BARS = 370 × 15 = 5550 根，触上限且最早一根
# 晚于 start 判 partial
_MAX_PAGES = 15
UPSTREAM_MAX_BARS = PAGE_SIZE * _MAX_PAGES


class ProviderAdapter(BaseProviderAdapter):
    """富途 OpenAPI（futunn）适配器。"""

    implemented = True

    async def fetch_bars(self, query: BarQuery) -> BarBatch:
        operation = self.resolve_operation(query)
        app_key, pem = self._ensure_ready()
        return await call_in_thread(
            self._fetch_sync, app_key, pem, operation, query, abandon_on_cancel=True
        )

    async def fetch_quote(self, query: QuoteQuery) -> Quote:
        operation = self.resolve_quote_operation(query)
        app_key, pem = self._ensure_ready()
        return await call_in_thread(
            self._fetch_quote_sync, app_key, pem, operation, query, abandon_on_cancel=True
        )

    def _ensure_ready(self) -> tuple[str, bytes]:
        """校验运行前提：AppKey 配置 + 私钥文件路径配置且可读。"""
        app_key = getattr(self.settings, "futunn_app_key", "") if self.settings else ""
        if not app_key:
            raise ProviderNotImplementedError(
                "futunn AppKey is not configured.",
                details={"provider": self.name, "setting": "TAPEHUB_FUTUNN_APP_KEY"},
            )
        key_path = (
            getattr(self.settings, "futunn_private_key_path", "") if self.settings else ""
        )
        if not key_path:
            raise ProviderNotImplementedError(
                "futunn private key path is not configured.",
                details={"provider": self.name, "setting": "TAPEHUB_FUTUNN_PRIVATE_KEY_PATH"},
            )
        # 文件缺失/不可读/非 PEM/算法不受支持均归运行前提失败（signing 模块）
        pem = load_private_key_pem(key_path, provider=self.name)
        return app_key, pem

    def _fetch_sync(
        self, app_key: str, pem: bytes, operation: str, query: BarQuery
    ) -> BarBatch:
        """历史 K 线：page_token 分页循环 → 闭区间复核 → partial 判定 → 内存分页。"""
        rows: list[dict[str, Any]] = []
        precision = 0
        page_token: str | None = None
        for _ in range(_MAX_PAGES):
            request = build_request(operation, query, page_token=page_token)
            payload = self._call_upstream(app_key, pem, request)
            page_rows, precision, page_token = _kline_page(payload)
            rows.extend(page_rows)
            if page_token is None:
                break
        bars = to_standard_bars(rows, volume_precision=precision)
        # 上游按标的市场日历日期过滤（分钟线 end 语义已由 request_mapper
        # 补偿），这里按闭区间 [start, end] 精确复核
        window = [bar for bar in bars if query.start <= bar.timestamp <= query.end]
        partial = _is_partial(bars, query)
        page = paginate_bars(window, query.offset, query.limit)
        return BarBatch(
            instrument=query.instrument,
            interval=query.interval,
            bars=page,
            provider=self.name,
            received_at=datetime.now(CN_TIMEZONE),
            offset=query.offset,
            limit=query.limit,
            partial=partial,
        )

    def _fetch_quote_sync(
        self, app_key: str, pem: bytes, operation: str, query: QuoteQuery
    ) -> Quote:
        """实时快照：单标的 code_list POST → 匹配 code 行 → 标准映射。"""
        request = build_quote_request(operation, query)
        payload = self._call_upstream(app_key, pem, request)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise UpstreamError(
                "futunn response data is not a JSON object.",
                details={"provider": self.name},
            )
        items = data.get("quote_list")
        if not isinstance(items, list):
            raise UpstreamError(
                "futunn response has no quote_list.",
                details={"provider": self.name},
            )
        code = futu_code(query)
        # 上游对非法 code 静默丢弃（ret_code 仍 0）：匹配行缺失属无有效行情
        row = next(
            (item for item in items if isinstance(item, dict) and item.get("code") == code),
            None,
        )
        if row is None:
            raise UpstreamError(
                "futunn quote payload has no matching code.",
                details={"provider": self.name},
            )
        return to_standard_quote(
            row, provider=self.name, received_at=datetime.now(CN_TIMEZONE)
        )

    def _call_upstream(
        self, app_key: str, pem: bytes, request: UpstreamRequest
    ) -> dict[str, Any]:
        """延迟导入 httpx 直连 REST（签名请求头），解析统一信封并返回载荷。

        query_string 由 request_mapper 统一生成：URL 拼接与签名原文共用
        同一字符串（GET 无 params 二次编码，POST 体即签名输入），保证签名
        一致性；GET 显式传 num/autype（不依赖上游默认值）。
        """
        try:
            import httpx
        except ImportError as exc:
            raise ProviderNotImplementedError(
                "Provider dependency 'httpx' is not installed.",
                details={"provider": self.name, "sdk": "httpx"},
            ) from exc
        headers = build_signed_headers(
            app_key,
            pem,
            method=request.method,
            request_path=request.request_path,
            query_string=request.query_string,
            body=request.body,
        )
        with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            if request.method == "POST":
                response = client.post(request.url, content=request.body, headers=headers)
            else:
                response = client.get(request.url, headers=headers)
        if response.status_code >= 400:
            # 官方约定业务错误走信封 ret_code（HTTP 200）；非 2xx 属契约外
            # 防御路径（网关/代理层错误、鉴权失败 401 等）
            raise UpstreamError(
                "futunn upstream returned an error response.",
                details={"provider": self.name, "status_code": response.status_code},
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamError(
                "futunn upstream returned a non-JSON response.",
                details={"provider": self.name, "status_code": response.status_code},
            ) from exc
        if not isinstance(payload, dict):
            raise UpstreamError(
                "futunn upstream envelope is not a JSON object.",
                details={"provider": self.name},
            )
        # 新旧两种封装并存：ret_code/ret_msg（旧）与 code/message（新）
        code = payload.get("ret_code", payload.get("code"))
        if code is None:
            raise UpstreamError(
                "futunn upstream envelope has no ret_code.",
                details={"provider": self.name},
            )
        if code != 0:
            message = payload.get("ret_msg") or payload.get("message")
            if code == _UPSTREAM_RATE_LIMITED_CODE:
                details: dict[str, Any] = {
                    "provider": self.name,
                    "upstream_code": code,
                    "upstream_message": message,
                }
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    details["retry_after"] = retry_after
                raise UpstreamRateLimitedError(
                    "futunn upstream rate limit exceeded.", details=details
                )
            raise UpstreamError(
                "futunn upstream returned a business error.",
                details={
                    "provider": self.name,
                    "upstream_code": code,
                    "upstream_message": message,
                },
            )
        return payload


def _kline_page(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int, str | None]:
    """单页信封 → ``(行列表, volume_precision, 下一页令牌)``。

    - ``data.kline_list`` 合法无数据为空列表（映射为空批次，不报错）；
    - ``pagination.next_page_token`` 非空且 has_more 非 False 才续页
      （page_token 15 分钟有效；续页免计历史「首请求」额度）；
    - ``data.volume_precision`` 缺失/非法按 0（官方默认口径，不缩放）。
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        raise UpstreamError(
            "futunn response data is not a JSON object.",
            details={"provider": "futunn"},
        )
    rows = data.get("kline_list")
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise UpstreamError(
            "futunn response kline_list is not a JSON array.",
            details={"provider": "futunn"},
        )
    page_rows = [row for row in rows if isinstance(row, dict)]
    precision = _precision_of(data.get("volume_precision"))
    next_token = _next_page_token(payload.get("pagination"))
    return page_rows, precision, next_token


def _precision_of(value: Any) -> int:
    """``data.volume_precision`` → int（缺失/非法按 0；布尔防御排除）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _next_page_token(pagination: Any) -> str | None:
    """续页令牌提取：非空字符串且 has_more 非 False 才返回（否则 None）。"""
    if not isinstance(pagination, dict):
        return None
    token = pagination.get("next_page_token")
    if not isinstance(token, str) or not token:
        return None
    if pagination.get("has_more") is False:
        return None
    return token


def _is_partial(bars: list[Bar], query: BarQuery) -> bool:
    """触分页上限且最早一根晚于 start → partial（从最新往回取型的隐式截断）。"""
    if len(bars) < UPSTREAM_MAX_BARS or not bars:
        return False
    earliest = min(bar.timestamp for bar in bars)
    return earliest > query.start
