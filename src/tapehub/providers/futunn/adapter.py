"""富途 OpenAPI（futunn）适配器（已实现，纯 REST 直连 + AppKey 签名）。

映射规则与上游行为定案见 docs/providers/futunn.md（唯一出处，本文不复述）。
"""

import logging
import threading
from datetime import datetime
from typing import TYPE_CHECKING, Any

from tapehub.domain.errors import (
    ProviderNotImplementedError,
    UpstreamError,
    UpstreamRateLimitedError,
)
from tapehub.domain.models import CN_TIMEZONE, Bar, BarBatch, BarQuery, Quote, QuoteQuery
from tapehub.providers.base import BaseProviderAdapter, truncate_upstream_message
from tapehub.providers.futunn.request_mapper import (
    PAGE_SIZE,
    UpstreamRequest,
    build_quote_request,
    build_request,
    futu_code,
)
from tapehub.providers.futunn.response_mapper import to_standard_bars, to_standard_quote
from tapehub.providers.futunn.signing import build_signed_headers, load_signing_key
from tapehub.providers.pagination import paginate_bars

if TYPE_CHECKING:
    from tapehub.settings import Settings

logger = logging.getLogger(__name__)

# httpx2 客户端超时（秒）：应用层 upstream_timeout_seconds（默认 30s）为
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

    def __init__(self, name: str, manifest: Any, settings: "Settings | None" = None) -> None:
        super().__init__(name, manifest, settings)
        # 长生命周期资源（懒创建）：签名私钥对象（每请求只签名不重复解析）
        # 与共享 HTTP 客户端（httpx2.Client 官方文档明确可跨线程共享——
        # 连接复用免去每请求 TLS 握手）；均经锁串行化首次创建
        self._signing_key: object | None = None
        self._client: Any = None
        self._resource_lock = threading.Lock()

    async def fetch_bars(self, query: BarQuery) -> BarBatch:
        operation = self.resolve_operation(query)
        app_key, signing_key = self._ensure_ready()
        return await self.call_in_thread(
            self._fetch_sync, app_key, signing_key, operation, query, abandon_on_cancel=True
        )

    async def fetch_quote(self, query: QuoteQuery) -> Quote:
        operation = self.resolve_quote_operation(query)
        app_key, signing_key = self._ensure_ready()
        return await self.call_in_thread(
            self._fetch_quote_sync, app_key, signing_key, operation, query, abandon_on_cancel=True
        )

    def shutdown_runtime(self) -> None:
        """停机钩子（由应用 lifespan 调用）：关闭共享 HTTP 客户端。"""
        with self._resource_lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    def _http_client(self, httpx2: Any) -> Any:
        """实例级共享 HTTP 客户端（懒创建，锁串行化首次构建防双创建泄漏）。"""
        with self._resource_lock:
            if self._client is None:
                self._client = httpx2.Client(timeout=_HTTP_TIMEOUT_SECONDS)
            return self._client

    def _ensure_ready(self) -> tuple[str, object]:
        """校验运行前提：AppKey 配置 + 私钥文件路径配置且可解析（缓存签名私钥）。"""
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
        # 文件缺失/不可读/非 PEM/算法不受支持均归运行前提失败（signing 模块）；
        # 私钥对象解析一次进程内复用（文件内容视为生命周期内不变）
        if self._signing_key is None:
            with self._resource_lock:
                if self._signing_key is None:
                    self._signing_key = load_signing_key(key_path, provider=self.name)
        return app_key, self._signing_key

    def _fetch_sync(
        self, app_key: str, signing_key: object, operation: str, query: BarQuery
    ) -> BarBatch:
        """历史 K 线：page_token 分页循环 → 闭区间复核 → partial 判定 → 内存分页。"""
        rows: list[dict[str, Any]] = []
        precision = 0
        page_token: str | None = None
        for _ in range(_MAX_PAGES):
            request = build_request(operation, query, page_token=page_token)
            payload = self._call_upstream(app_key, signing_key, request)
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
        self, app_key: str, signing_key: object, operation: str, query: QuoteQuery
    ) -> Quote:
        """实时快照：单标的 code_list POST → 匹配 code 行 → 标准映射。"""
        request = build_quote_request(operation, query)
        payload = self._call_upstream(app_key, signing_key, request)
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
        self, app_key: str, signing_key: object, request: UpstreamRequest
    ) -> dict[str, Any]:
        """延迟导入 httpx2 直连 REST（签名请求头），解析统一信封并返回载荷。

        query_string 由 request_mapper 统一生成：URL 拼接与签名原文共用
        同一字符串（签名一致性单点保证）。HTTP 客户端为实例级长生命周期
        共享对象（官方文档明确可跨线程共享），首次请求懒创建、停机钩子关闭。
        """
        try:
            import httpx2
        except ImportError as exc:
            raise ProviderNotImplementedError(
                "Provider dependency 'httpx2' is not installed.",
                details={"provider": self.name, "sdk": "httpx2"},
            ) from exc
        headers = build_signed_headers(
            app_key,
            signing_key,
            method=request.method,
            request_path=request.request_path,
            query_string=request.query_string,
            body=request.body,
        )
        client = self._http_client(httpx2)
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
            # 上游报文全文只进日志（可按 request_id 关联）；错误响应 details
            # 携带截断文本（上游可控文本不透传完整内容）
            logger.warning("futunn upstream business error: code=%s message=%s", code, message)
            if code == _UPSTREAM_RATE_LIMITED_CODE:
                details: dict[str, Any] = {
                    "provider": self.name,
                    "upstream_code": code,
                    "upstream_message": truncate_upstream_message(message),
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
                    "upstream_message": truncate_upstream_message(message),
                },
            )
        return payload


def _kline_page(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int, str | None]:
    """单页信封 → ``(行列表, volume_precision, 下一页令牌)``。

    kline_list 合法无数据为空列表；next_page_token 非空且 has_more 非 False
    才续页；volume_precision 缺失/非法按 0（官方默认口径，不缩放）。
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
