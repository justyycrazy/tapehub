"""TickFlow 适配器（已实现）。

映射规则与上游行为定案见 docs/providers/tickflow.md（唯一出处，本文不复述）。
官方文档：https://docs.tickflow.org/zh-hans/。
"""

import threading
from datetime import datetime
from typing import TYPE_CHECKING, Any

from tapehub.domain.errors import ProviderNotImplementedError
from tapehub.domain.models import CN_TIMEZONE, BarBatch, BarQuery, Quote, QuoteQuery
from tapehub.providers.base import BaseProviderAdapter
from tapehub.providers.pagination import paginate_bars
from tapehub.providers.tickflow.request_mapper import (
    UPSTREAM_MAX_COUNT,
    build_quote_request,
    build_request,
)
from tapehub.providers.tickflow.response_mapper import to_standard_bars, to_standard_quote

if TYPE_CHECKING:
    from tapehub.settings import Settings


class ProviderAdapter(BaseProviderAdapter):
    """TickFlow 适配器。"""

    implemented = True

    def __init__(self, name: str, manifest: Any, settings: "Settings | None" = None) -> None:
        super().__init__(name, manifest, settings)
        # SDK 客户端实例级缓存（懒创建，锁串行化首次构建）：TickFlow 持有
        # httpx.Client 连接池——httpx 官方声明 Client 可跨线程共享
        # （httpx/_client.py "It can be shared between threads."），SDK 请求
        # 路径无实例状态写入（见 docs/providers/tickflow.md「SDK 客户端生命
        # 周期」）；apikey 进程内不变，apikey 或 SDK 入口身份变更即重建——
        # 后者保证测试经 sys.modules 替换桩模块后缓存自动失效
        self._client: Any = None
        self._client_apikey: str | None = None
        self._client_entry: Any = None
        self._client_lock = threading.Lock()

    async def fetch_bars(self, query: BarQuery) -> BarBatch:
        operation = self.resolve_operation(query)
        self._ensure_ready()
        return await self.call_in_thread(self._fetch_sync, operation, query, abandon_on_cancel=True)

    def shutdown_runtime(self) -> None:
        """停机钩子（由应用 lifespan 调用）：关闭共享 SDK 客户端。"""
        with self._client_lock:
            if self._client is not None:
                self._client.close()
                self._client = None
                self._client_apikey = None
                self._client_entry = None

    def _ensure_ready(self) -> str:
        """校验运行前提：SDK 可选依赖与 API Key 配置。"""
        api_key = self._api_key()
        if not api_key:
            raise ProviderNotImplementedError(
                "TickFlow API key is not configured.",
                details={"provider": self.name, "setting": "TAPEHUB_TICKFLOW_API_KEY"},
            )
        return api_key

    def _api_key(self) -> str:
        value = getattr(self.settings, "tickflow_api_key", "") if self.settings else ""
        return value or ""

    def _fetch_sync(self, operation: str, query: BarQuery) -> BarBatch:
        payload = self._call_upstream(self._ensure_ready(), query)
        rows_count = self._payload_size(payload)
        bars = to_standard_bars(payload)
        # 上游按毫秒时间过滤，这里按闭区间 [start, end] 精确复核统一时间语义
        window = [bar for bar in bars if query.start <= bar.timestamp <= query.end]
        page = paginate_bars(window, query.offset, query.limit)
        # 达到上游单次上限时无法确认是否被截断，诚实标记 partial
        partial = rows_count >= UPSTREAM_MAX_COUNT
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

    def _sdk_client(self, tickflow_client: Any, api_key: str) -> Any:
        """SDK 客户端懒创建缓存（apikey 或 SDK 入口身份变更即重建）。"""
        with self._client_lock:
            if (
                self._client is None
                or self._client_apikey != api_key
                or self._client_entry is not tickflow_client
            ):
                self._client = tickflow_client(api_key=api_key)
                self._client_apikey = api_key
                self._client_entry = tickflow_client
            return self._client

    def _call_upstream(self, api_key: str, query: BarQuery) -> Any:
        """延迟导入官方 SDK 并调用 tf.klines.get（客户端实例级缓存复用）。"""
        try:
            from tickflow import TickFlow
        except ImportError as exc:
            raise ProviderNotImplementedError(
                "Provider SDK 'tickflow' is not installed.",
                details={"provider": self.name, "sdk": "tickflow"},
            ) from exc
        kwargs = build_request(query)
        client = self._sdk_client(TickFlow, api_key)
        return client.klines.get(**kwargs)

    async def fetch_quote(self, query: QuoteQuery) -> Quote:
        self.resolve_quote_operation(query)
        self._ensure_ready()
        return await self.call_in_thread(self._fetch_quote_sync, query, abandon_on_cancel=True)

    def _fetch_quote_sync(self, query: QuoteQuery) -> Quote:
        payload = self._call_quote_upstream(self._ensure_ready(), query)
        return to_standard_quote(
            payload, provider=self.name, received_at=datetime.now(CN_TIMEZONE)
        )

    def _call_quote_upstream(self, api_key: str, query: QuoteQuery) -> Any:
        """延迟导入官方 SDK 并调用 tf.quotes.get（单标的单次调用，客户端缓存复用）。"""
        try:
            from tickflow import TickFlow
        except ImportError as exc:
            raise ProviderNotImplementedError(
                "Provider SDK 'tickflow' is not installed.",
                details={"provider": self.name, "sdk": "tickflow"},
            ) from exc
        client = self._sdk_client(TickFlow, api_key)
        return client.quotes.get(**build_quote_request(query))

    def _payload_size(self, payload: Any) -> int:
        columns = getattr(payload, "get", None)
        if callable(columns):
            data = payload.get("data") if isinstance(payload, dict) else None
            target = data if isinstance(data, dict) else payload
            timestamps = target.get("timestamp") if isinstance(target, dict) else None
            if isinstance(timestamps, list):
                return len(timestamps)
        return 0
