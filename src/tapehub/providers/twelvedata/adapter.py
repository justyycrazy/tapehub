"""Twelve Data 适配器（已实现）。

约定：

- 官方 Python 库 ``twelvedata``（twelvedata-python）是可选依赖：只在
  ``_fetch_rows`` 内部延迟导入，未安装时返回 PROVIDER_NOT_IMPLEMENTED，
  不影响服务启动；
- API Key 通过 ``TAPEHUB_TWELVEDATA_API_KEY`` 环境变量注入
  （pydantic-settings），只经内存传给 TDClient，禁止写入代码、manifest、
  日志或异常响应；
- 官方库为同步 SDK：调用通过 ``call_in_thread`` 隔离到线程池；
- 上游 outputsize 上限 5000 且按“最新一根”往回返回：采用覆盖判定——
  返回数量达到上限且最早一根仍晚于请求 start 时标记 partial=true，
  API 层据此在 warnings 中提示可能截断；
- values 默认倒序，统一升序排序与分页由共享 ``paginate_bars`` 完成，
  闭区间 [start, end] 在标准化后复核；
- 上游 SDK/HTTP 异常类型未在文档中核实，统一由应用层包装为 UPSTREAM_ERROR。
"""

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from tapehub.domain.errors import ProviderNotImplementedError
from tapehub.domain.models import BarBatch, BarQuery
from tapehub.providers.base import BaseProviderAdapter, call_in_thread
from tapehub.providers.pagination import paginate_bars
from tapehub.providers.twelvedata.request_mapper import UPSTREAM_MAX_BARS, build_request
from tapehub.providers.twelvedata.response_mapper import to_standard_bars

if TYPE_CHECKING:
    pass


class ProviderAdapter(BaseProviderAdapter):
    """Twelve Data 适配器。"""

    implemented = True

    async def fetch_bars(self, query: BarQuery) -> BarBatch:
        operation = self.resolve_operation(query)
        api_key = self._ensure_ready()
        return await call_in_thread(self._fetch_sync, api_key, operation, query)

    def _ensure_ready(self) -> str:
        """校验运行前提：API Key 配置。"""
        value = getattr(self.settings, "twelvedata_api_key", "") if self.settings else ""
        if not value:
            raise ProviderNotImplementedError(
                "Twelve Data API key is not configured.",
                details={"provider": self.name, "setting": "TAPEHUB_TWELVEDATA_API_KEY"},
            )
        return value

    def _fetch_sync(self, api_key: str, operation: str, query: BarQuery) -> BarBatch:
        rows = self._fetch_rows(api_key, query)
        bars = to_standard_bars(rows)
        # 上游按日期范围过滤，这里按闭区间 [start, end] 精确复核统一时间语义
        window = [bar for bar in bars if query.start <= bar.timestamp <= query.end]
        # 覆盖不足：返回数量达到上限且最早一根仍晚于请求 start，更早的数据取不到
        partial = len(bars) >= UPSTREAM_MAX_BARS and (
            not bars or min(bar.timestamp for bar in bars) > query.start
        )
        page = paginate_bars(window, query.offset, query.limit)
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

    def _fetch_rows(self, api_key: str, query: BarQuery) -> list[dict[str, Any]]:
        """先完成参数映射，再延迟导入官方库调用 td.time_series(...).as_json()。"""
        kwargs = build_request(query)
        try:
            from twelvedata import TDClient
        except ImportError as exc:
            raise ProviderNotImplementedError(
                "Provider SDK 'twelvedata' is not installed.",
                details={"provider": self.name, "sdk": "twelvedata"},
            ) from exc
        client = TDClient(apikey=api_key)
        series = client.time_series(**kwargs)
        payload = series.as_json()
        if payload is None:
            return []
        return list(payload)
