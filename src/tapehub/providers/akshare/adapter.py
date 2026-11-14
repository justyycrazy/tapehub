"""akshare 适配器（已实现）。

映射规则与上游行为定案见 docs/providers/akshare.md（唯一出处，本文不复述）。
探针运行记录见 probes/results/PROBE_LOG.md（本地文件，不入库）。
"""

from datetime import datetime
from typing import Any

from tapehub.domain.errors import ProviderNotImplementedError
from tapehub.domain.models import CN_TIMEZONE, BarBatch, BarQuery
from tapehub.providers.akshare.request_mapper import build_request
from tapehub.providers.akshare.response_mapper import to_standard_bars
from tapehub.providers.base import BaseProviderAdapter, call_in_thread
from tapehub.providers.pagination import paginate_bars


class ProviderAdapter(BaseProviderAdapter):
    """akshare 适配器。"""

    implemented = True

    async def fetch_bars(self, query: BarQuery) -> BarBatch:
        operation = self.resolve_operation(query)
        return await call_in_thread(self._fetch_sync, operation, query, abandon_on_cancel=True)

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
            received_at=datetime.now(CN_TIMEZONE),
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
