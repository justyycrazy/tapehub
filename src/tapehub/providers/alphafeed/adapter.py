"""AlphaFeed 适配器（已实现）。

约定：

- 官方 Python SDK ``alphafeed`` 是可选依赖：SDK 只在 ``_fetch_sync`` /
  ``_fetch_quote_sync`` 内部延迟导入，未安装时返回 PROVIDER_NOT_IMPLEMENTED，
  不影响服务启动；
- API Key 通过 ``TAPEHUB_ALPHAFEED_API_KEY`` 环境变量注入（pydantic-settings），
  只经内存传给 SDK 客户端，禁止写入代码、manifest、日志或异常响应；
- 同步 SDK 调用通过 ``call_in_thread`` 隔离到线程池，不阻塞事件循环；
- 上游单次上限 10000 根 K 线：达到上限时批次标记 ``partial=true``，
  API 层据此在 warnings 中提示可能截断；
- 统一分页由共享 ``paginate_bars`` 完成，闭区间 [start, end] 在标准化后复核；
- 上游 429（限频）映射为 UPSTREAM_RATE_LIMITED（SDK 对 429/5xx 自带最多
  3 次退避重试，映射发生在重试耗尽后），401/403 等其余 SDK 异常由应用层
  统一包装为 UPSTREAM_ERROR；
- 快照为纯 REST 单次调用语义：无首包超时/QUOTE_NOT_READY（与 tickflow
  一致），上游无推送接口，不实现 stream_quote（流式请求返回
  UNSUPPORTED_CAPABILITY）。
"""

from datetime import datetime
from typing import Any

from tapehub.domain.errors import ProviderNotImplementedError
from tapehub.domain.models import CN_TIMEZONE, BarBatch, BarQuery, Quote, QuoteQuery
from tapehub.providers.alphafeed.request_mapper import (
    UPSTREAM_MAX_COUNT,
    build_quote_request,
    build_request,
)
from tapehub.providers.alphafeed.response_mapper import to_standard_bars, to_standard_quote
from tapehub.providers.base import BaseProviderAdapter, call_in_thread
from tapehub.providers.pagination import paginate_bars


class ProviderAdapter(BaseProviderAdapter):
    """AlphaFeed 适配器。"""

    implemented = True

    async def fetch_bars(self, query: BarQuery) -> BarBatch:
        operation = self.resolve_operation(query)
        self._ensure_ready()
        return await call_in_thread(self._fetch_sync, operation, query)

    async def fetch_quote(self, query: QuoteQuery) -> Quote:
        operation = self.resolve_quote_operation(query)
        self._ensure_ready()
        return await call_in_thread(self._fetch_quote_sync, operation, query)

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
            from alphafeed import AlphaFeed
        except ImportError as exc:
            raise ProviderNotImplementedError(
                "Provider SDK 'alphafeed' is not installed.",
                details={"provider": self.name, "sdk": "alphafeed"},
            ) from exc
        kwargs = build_request(query)
        client = AlphaFeed(api_key=api_key)
        return client.klines.get(**kwargs)

    def _fetch_quote_sync(self, operation: str, query: QuoteQuery) -> Quote:
        payload = self._call_quote_upstream(self._ensure_ready(), query)
        return to_standard_quote(
            payload, provider=self.name, received_at=datetime.now(CN_TIMEZONE)
        )

    def _call_quote_upstream(self, api_key: str, query: QuoteQuery) -> Any:
        """延迟导入官方 SDK 并调用 af.quotes.get（list[Quote dict] 默认返回）。"""
        try:
            from alphafeed import AlphaFeed
        except ImportError as exc:
            raise ProviderNotImplementedError(
                "Provider SDK 'alphafeed' is not installed.",
                details={"provider": self.name, "sdk": "alphafeed"},
            ) from exc
        kwargs = build_quote_request(query)
        client = AlphaFeed(api_key=api_key)
        return client.quotes.get(**kwargs)

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
