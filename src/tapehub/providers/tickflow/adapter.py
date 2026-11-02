"""TickFlow 适配器（已实现）。

约定：

- 官方 Python SDK ``tickflow`` 是可选依赖：SDK 只在 ``_fetch_sync`` 内部
  延迟导入，未安装时返回 PROVIDER_NOT_IMPLEMENTED，不影响服务启动；
- API Key 通过 ``TAPEHUB_TICKFLOW_API_KEY`` 环境变量注入（pydantic-settings），
  只经内存传给 SDK 客户端，禁止写入代码、manifest、日志或异常响应；
- 同步 SDK 调用通过 ``call_in_thread`` 隔离到线程池，不阻塞事件循环；
- 上游单次上限 10000 根 K 线：达到上限时批次标记 ``partial=true``，
  API 层据此在 warnings 中提示可能截断；
- 统一分页由共享 ``paginate_bars`` 完成，闭区间 [start, end] 在标准化后复核。
"""

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from tapehub.domain.errors import ProviderNotImplementedError
from tapehub.domain.models import BarBatch, BarQuery
from tapehub.providers.base import BaseProviderAdapter, call_in_thread
from tapehub.providers.pagination import paginate_bars
from tapehub.providers.tickflow.request_mapper import UPSTREAM_MAX_COUNT, build_request
from tapehub.providers.tickflow.response_mapper import to_standard_bars

if TYPE_CHECKING:
    pass


class ProviderAdapter(BaseProviderAdapter):
    """TickFlow 适配器。"""

    implemented = True

    async def fetch_bars(self, query: BarQuery) -> BarBatch:
        operation = self.resolve_operation(query)
        self._ensure_ready()
        return await call_in_thread(self._fetch_sync, operation, query)

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
            received_at=datetime.now(timezone.utc),
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

    def _payload_size(self, payload: Any) -> int:
        columns = getattr(payload, "get", None)
        if callable(columns):
            data = payload.get("data") if isinstance(payload, dict) else None
            target = data if isinstance(data, dict) else payload
            timestamps = target.get("timestamp") if isinstance(target, dict) else None
            if isinstance(timestamps, list):
                return len(timestamps)
        return 0
