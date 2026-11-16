"""同花顺金融数据 API（ths）适配器（已实现，纯 REST 直连）。

映射规则与上游行为定案见 docs/providers/ths.md（唯一出处，本文不复述）。
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
from tapehub.domain.models import CN_TIMEZONE, BarBatch, BarQuery, Quote, QuoteQuery
from tapehub.providers.base import BaseProviderAdapter, call_in_thread, truncate_upstream_message
from tapehub.providers.pagination import paginate_bars
from tapehub.providers.ths.request_mapper import build_quote_request, build_request
from tapehub.providers.ths.response_mapper import to_standard_bars, to_standard_quote

if TYPE_CHECKING:
    from tapehub.settings import Settings

logger = logging.getLogger(__name__)

# httpx2 客户端超时（秒）：应用层 upstream_timeout_seconds（默认 30s）为
# 整体兜底，客户端自身超时取更小的保守值，避免长挂线程占用 worker
_HTTP_TIMEOUT_SECONDS = 20.0

# 信封业务码：频率超限（官方错误码表）→ UPSTREAM_RATE_LIMITED（HTTP 429）
_UPSTREAM_RATE_LIMITED_CODE = 4001


class ProviderAdapter(BaseProviderAdapter):
    """同花顺金融数据 API（ths）适配器。"""

    implemented = True

    def __init__(self, name: str, manifest: Any, settings: "Settings | None" = None) -> None:
        super().__init__(name, manifest, settings)
        # 实例级长生命周期共享 HTTP 客户端（httpx2.Client 官方文档明确可跨
        # 线程共享——连接复用免去每请求 TLS 握手）；锁串行化首次创建
        self._client: Any = None
        self._client_lock = threading.Lock()

    async def fetch_bars(self, query: BarQuery) -> BarBatch:
        operation = self.resolve_operation(query)
        api_key = self._ensure_ready()
        return await call_in_thread(
            self._fetch_sync, api_key, operation, query, abandon_on_cancel=True
        )

    async def fetch_quote(self, query: QuoteQuery) -> Quote:
        operation = self.resolve_quote_operation(query)
        api_key = self._ensure_ready()
        return await call_in_thread(
            self._fetch_quote_sync, api_key, operation, query, abandon_on_cancel=True
        )

    def shutdown_runtime(self) -> None:
        """停机钩子（由应用 lifespan 调用）：关闭共享 HTTP 客户端。"""
        with self._client_lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    def _http_client(self, httpx2: Any) -> Any:
        """实例级共享 HTTP 客户端（懒创建，锁串行化首次构建防双创建泄漏）。"""
        with self._client_lock:
            if self._client is None:
                self._client = httpx2.Client(timeout=_HTTP_TIMEOUT_SECONDS)
            return self._client

    def _ensure_ready(self) -> str:
        """校验运行前提：API Key 配置。"""
        value = getattr(self.settings, "ths_api_key", "") if self.settings else ""
        if not value:
            raise ProviderNotImplementedError(
                "ths API key is not configured.",
                details={"provider": self.name, "setting": "TAPEHUB_THS_API_KEY"},
            )
        return value

    def _fetch_sync(self, api_key: str, operation: str, query: BarQuery) -> BarBatch:
        data = self._call_upstream(api_key, operation, query)
        rows = _rows_of(data)
        bars = to_standard_bars(rows)
        # 上游按毫秒时间过滤（闭区间），这里按闭区间 [start, end] 精确复核
        window = [bar for bar in bars if query.start <= bar.timestamp <= query.end]
        # 单次行数上限官方未标注，隐式截断无法可靠判定：诚实起见 partial 恒 false
        page = paginate_bars(window, query.offset, query.limit)
        return BarBatch(
            instrument=query.instrument,
            interval=query.interval,
            bars=page,
            provider=self.name,
            received_at=datetime.now(CN_TIMEZONE),
            offset=query.offset,
            limit=query.limit,
            partial=False,
        )

    def _fetch_quote_sync(self, api_key: str, operation: str, query: QuoteQuery) -> Quote:
        data = self._call_upstream(api_key, operation, query)
        return to_standard_quote(
            data, operation, provider=self.name, received_at=datetime.now(CN_TIMEZONE)
        )

    def _call_upstream(self, api_key: str, operation: str, query: Any) -> dict[str, Any]:
        """先完成参数映射，再延迟导入 httpx2 直连 REST（GET，X-api-key 头），
        解析统一 ApiResponse 信封并返回业务数据 ``data`` 容器。"""
        url, params = build_request(operation, query) if isinstance(
            query, BarQuery
        ) else build_quote_request(operation, query)
        try:
            import httpx2
        except ImportError as exc:
            raise ProviderNotImplementedError(
                "Provider dependency 'httpx2' is not installed.",
                details={"provider": self.name, "sdk": "httpx2"},
            ) from exc
        headers = {"X-api-key": api_key}
        client = self._http_client(httpx2)
        response = client.get(url, params=params, headers=headers)
        if response.status_code >= 400:
            # 官方约定业务错误走信封 code（HTTP 恒 200）；非 2xx 属契约外
            # 防御路径（网关/代理层错误等）
            raise UpstreamError(
                "ths upstream returned an error response.",
                details={"provider": self.name, "status_code": response.status_code},
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamError(
                "ths upstream returned a non-JSON response.",
                details={"provider": self.name, "status_code": response.status_code},
            ) from exc
        if not isinstance(payload, dict):
            raise UpstreamError(
                "ths upstream envelope is not a JSON object.",
                details={"provider": self.name, "status_code": response.status_code},
            )
        code = payload.get("code")
        if code != 0:
            message = payload.get("message")
            # 上游报文全文只进日志（可按 request_id 关联）；错误响应 details
            # 携带截断文本（上游可控文本不透传完整内容）
            logger.warning("ths upstream business error: code=%s message=%s", code, message)
            if code == _UPSTREAM_RATE_LIMITED_CODE:
                raise UpstreamRateLimitedError(
                    "ths upstream rate limit exceeded.",
                    details={
                        "provider": self.name,
                        "upstream_code": code,
                        "upstream_message": truncate_upstream_message(message),
                    },
                )
            raise UpstreamError(
                "ths upstream returned a business error.",
                details={
                    "provider": self.name,
                    "upstream_code": code,
                    "upstream_message": truncate_upstream_message(message),
                },
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise UpstreamError(
                "ths upstream envelope data is not a JSON object.",
                details={"provider": self.name},
            )
        return data


def _rows_of(data: dict[str, Any]) -> list[dict[str, Any]]:
    """信封 ``data.item`` 防御提取为行列表（合法无数据时上游返回空列表）。"""
    items = data.get("item")
    if items is None:
        return []
    if not isinstance(items, list):
        raise UpstreamError(
            "ths response data item is not a JSON array.",
            details={"provider": "ths"},
        )
    return [row for row in items if isinstance(row, dict)]
