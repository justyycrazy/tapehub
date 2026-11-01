"""akshare 适配器（已实现）。

约定：

- akshare 是可选依赖：SDK 只在 ``_fetch_sync`` 内部延迟导入，
  未安装时返回 PROVIDER_NOT_IMPLEMENTED，不影响服务启动；
- 同步阻塞调用通过 ``call_in_thread`` 隔离到线程池，不阻塞事件循环；
- 接口名仅存在于本适配器与 manifest 的 operations 中，不暴露到公共 API；
- 上游接口不支持 offset/limit，统一分页由共享 ``paginate_bars`` 完成；
  上游按日期过滤的边界由适配器在标准化后按闭区间 [start, end] 精确复核。
"""

from datetime import datetime, timezone
from typing import Any

from tapehub.domain.errors import ProviderNotImplementedError
from tapehub.domain.models import BarBatch, BarQuery
from tapehub.providers.akshare.request_mapper import build_request
from tapehub.providers.akshare.response_mapper import to_standard_bars
from tapehub.providers.base import BaseProviderAdapter, call_in_thread
from tapehub.providers.pagination import paginate_bars


class ProviderAdapter(BaseProviderAdapter):
    """akshare 适配器。"""

    implemented = True

    async def fetch_bars(self, query: BarQuery) -> BarBatch:
        operation = self.resolve_operation(query)
        return await call_in_thread(self._fetch_sync, operation, query)

    def _fetch_sync(self, operation: str, query: BarQuery) -> BarBatch:
        frame = self._call_upstream(operation, query)
        rows = self._to_rows(frame)
        bars = to_standard_bars(operation, rows)
        # 上游按日期过滤，这里按闭区间 [start, end] 精确复核统一时间语义
        window = [bar for bar in bars if query.start <= bar.timestamp <= query.end]
        page = paginate_bars(window, query.offset, query.limit)
        return BarBatch(
            instrument=query.instrument,
            interval=query.interval,
            bars=page,
            provider=self.name,
            received_at=datetime.now(timezone.utc),
            offset=query.offset,
            limit=query.limit,
        )

    def _call_upstream(self, operation: str, query: BarQuery) -> Any:
        """先完成参数映射，再延迟导入 akshare 并调用对应接口。"""
        kwargs = build_request(operation, query)
        try:
            import akshare as ak
        except ImportError as exc:
            raise ProviderNotImplementedError(
                "Provider SDK 'akshare' is not installed.",
                details={"provider": self.name, "sdk": "akshare"},
            ) from exc
        func = getattr(ak, operation)
        return func(**kwargs)

    def _to_rows(self, frame: Any) -> list[dict[str, Any]]:
        """上游 DataFrame → 字典行列表；空结果返回空列表。"""
        if frame is None:
            return []
        return list(frame.to_dict("records"))
