"""TickFlow 适配器（已实现）。

映射规则与上游行为定案见 docs/providers/tickflow.md（唯一出处，本文不复述）。
探针运行记录见 probes/results/PROBE_LOG.md（本地文件，不入库）。
官方文档：https://docs.tickflow.org/zh-hans/。
"""

from datetime import datetime
from typing import Any

from tapehub.domain.errors import ProviderNotImplementedError
from tapehub.domain.models import CN_TIMEZONE, BarBatch, BarQuery, Quote, QuoteQuery
from tapehub.providers.base import BaseProviderAdapter, call_in_thread
from tapehub.providers.pagination import paginate_bars
from tapehub.providers.tickflow.request_mapper import (
    UPSTREAM_MAX_COUNT,
    build_quote_request,
    build_request,
)
from tapehub.providers.tickflow.response_mapper import to_standard_bars, to_standard_quote


class ProviderAdapter(BaseProviderAdapter):
    """TickFlow 适配器。"""

    implemented = True

    async def fetch_bars(self, query: BarQuery) -> BarBatch:
        operation = self.resolve_operation(query)
        self._ensure_ready()
        return await call_in_thread(self._fetch_sync, operation, query, abandon_on_cancel=True)

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

    def _call_upstream(self, api_key: str, query: BarQuery) -> Any:
        """延迟导入官方 SDK 并调用 tf.klines.get。"""
        try:
            from tickflow import TickFlow
        except ImportError as exc:
            raise ProviderNotImplementedError(
                "Provider SDK 'tickflow' is not installed.",
                details={"provider": self.name, "sdk": "tickflow"},
            ) from exc
        kwargs = build_request(query)
        client = TickFlow(api_key=api_key)
        return client.klines.get(**kwargs)

    async def fetch_quote(self, query: QuoteQuery) -> Quote:
        self.resolve_quote_operation(query)
        self._ensure_ready()
        return await call_in_thread(self._fetch_quote_sync, query, abandon_on_cancel=True)

    def _fetch_quote_sync(self, query: QuoteQuery) -> Quote:
        payload = self._call_quote_upstream(self._ensure_ready(), query)
        return to_standard_quote(
            payload, provider=self.name, received_at=datetime.now(CN_TIMEZONE)
        )

    def _call_quote_upstream(self, api_key: str, query: QuoteQuery) -> Any:
        """延迟导入官方 SDK 并调用 tf.quotes.get（单标的单次调用）。"""
        try:
            from tickflow import TickFlow
        except ImportError as exc:
            raise ProviderNotImplementedError(
                "Provider SDK 'tickflow' is not installed.",
                details={"provider": self.name, "sdk": "tickflow"},
            ) from exc
        client = TickFlow(api_key=api_key)
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
