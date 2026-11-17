"""AlphaFeed 适配器（已实现）。

映射规则与上游行为定案见 docs/providers/alphafeed.md（唯一出处，本文不复述）。
"""

from datetime import datetime
from typing import Any

from tapehub.domain.errors import ProviderNotImplementedError, UpstreamRateLimitedError
from tapehub.domain.models import CN_TIMEZONE, BarBatch, BarQuery, Quote, QuoteQuery
from tapehub.providers.alphafeed.request_mapper import (
    UPSTREAM_MAX_COUNT,
    build_quote_request,
    build_request,
)
from tapehub.providers.alphafeed.response_mapper import to_standard_bars, to_standard_quote
from tapehub.providers.base import BaseProviderAdapter
from tapehub.providers.pagination import paginate_bars


class ProviderAdapter(BaseProviderAdapter):
    """AlphaFeed 适配器。"""

    implemented = True

    async def fetch_bars(self, query: BarQuery) -> BarBatch:
        operation = self.resolve_operation(query)
        self._ensure_ready()
        return await self.call_in_thread(self._fetch_sync, operation, query, abandon_on_cancel=True)

    async def fetch_quote(self, query: QuoteQuery) -> Quote:
        operation = self.resolve_quote_operation(query)
        self._ensure_ready()
        return await self.call_in_thread(
            self._fetch_quote_sync, operation, query, abandon_on_cancel=True
        )

    def _ensure_ready(self) -> str:
        """校验运行前提：API Key 配置。"""
        api_key = self._api_key()
        if not api_key:
            raise ProviderNotImplementedError(
                "AlphaFeed API key is not configured.",
                details={"provider": self.name, "setting": "TAPEHUB_ALPHAFEED_API_KEY"},
            )
        return api_key

    def _api_key(self) -> str:
        value = getattr(self.settings, "alphafeed_api_key", "") if self.settings else ""
        return value or ""

    def _fetch_sync(self, operation: str, query: BarQuery) -> BarBatch:
        payload = self._call_upstream(self._ensure_ready(), query)
        rows_count = self._payload_size(payload)
        bars = to_standard_bars(payload)
        # 上游按毫秒时间过滤（含端点），这里按闭区间 [start, end] 精确复核
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
        """延迟导入官方 SDK 并调用 af.klines.get（原始列式返回）。"""
        try:
            from alphafeed import AlphaFeed, RateLimitError
        except ImportError as exc:
            raise ProviderNotImplementedError(
                "Provider SDK 'alphafeed' is not installed.",
                details={"provider": self.name, "sdk": "alphafeed"},
            ) from exc
        kwargs = build_request(query)
        client = AlphaFeed(api_key=api_key)
        try:
            return client.klines.get(**kwargs)
        except RateLimitError as exc:
            raise self._rate_limited(exc) from exc

    def _fetch_quote_sync(self, operation: str, query: QuoteQuery) -> Quote:
        payload = self._call_quote_upstream(self._ensure_ready(), query)
        return to_standard_quote(
            payload, provider=self.name, received_at=datetime.now(CN_TIMEZONE)
        )

    def _call_quote_upstream(self, api_key: str, query: QuoteQuery) -> Any:
        """延迟导入官方 SDK 并调用 af.quotes.get（list[Quote dict] 默认返回）。"""
        try:
            from alphafeed import AlphaFeed, RateLimitError
        except ImportError as exc:
            raise ProviderNotImplementedError(
                "Provider SDK 'alphafeed' is not installed.",
                details={"provider": self.name, "sdk": "alphafeed"},
            ) from exc
        kwargs = build_quote_request(query)
        client = AlphaFeed(api_key=api_key)
        try:
            return client.quotes.get(**kwargs)
        except RateLimitError as exc:
            raise self._rate_limited(exc) from exc

    def _rate_limited(self, exc: Exception) -> UpstreamRateLimitedError:
        """SDK 限频异常（429，重试耗尽后）→ UPSTREAM_RATE_LIMITED。

        ``retry_after_ms`` 从上游错误 details 取得则透传，取不到不编造。
        """
        details: dict[str, Any] = {"provider": self.name}
        upstream_details = getattr(exc, "details", None)
        if isinstance(upstream_details, dict):
            retry_after = upstream_details.get("retry_after_ms")
            if isinstance(retry_after, int):
                details["retry_after_ms"] = retry_after
        return UpstreamRateLimitedError(
            "AlphaFeed upstream rate limit exceeded.", details=details
        )

    def _payload_size(self, payload: Any) -> int:
        if not isinstance(payload, dict):
            return 0
        target = payload
        data = payload.get("data")
        if isinstance(data, dict):
            target = data
        if isinstance(target, dict) and isinstance(target.get("timestamp"), list):
            return len(target["timestamp"])
        # 批量包装 {symbol: CompactKlineData}
        for value in target.values():
            if isinstance(value, dict) and isinstance(value.get("timestamp"), list):
                return len(value["timestamp"])
        return 0
