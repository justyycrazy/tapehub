"""Tushare 适配器（已实现）。

约定：

- 官方 Python SDK ``tushare`` 是可选依赖：SDK 只在 ``_fetch_sync`` 内部
  延迟导入，未安装时返回 PROVIDER_NOT_IMPLEMENTED，不影响服务启动；
- Token 通过 ``TAPEHUB_TUSHARE_TOKEN`` 环境变量注入（pydantic-settings），
  只经内存传给 SDK 客户端（ts.pro_api），禁止写入代码、manifest、日志或异常响应；
- 同步 SDK 调用通过 ``call_in_thread`` 隔离到线程池，不阻塞事件循环；
- 两级路由：manifest operations 选中 pro_bar（股票）或 fut_daily（期货单一合约），
  上游函数名仅存在于本适配器与 manifest 内部；
- fut_daily 单次上限 2000 条：达到上限时批次标记 ``partial=true``，
  API 层据此在 warnings 中提示可能截断；
- 统一分页由共享 ``paginate_bars`` 完成，闭区间 [start, end] 在标准化后复核；
- 上游 SDK 异常/积分限频的具体类型未在文档中核实，统一由应用层包装为
  UPSTREAM_ERROR（40203 等积分错误码待核实后补充）。
"""

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from tapehub.domain.errors import ProviderNotImplementedError
from tapehub.domain.models import BarBatch, BarQuery
from tapehub.providers.base import BaseProviderAdapter, call_in_thread
from tapehub.providers.pagination import paginate_bars
from tapehub.providers.tushare.request_mapper import FUT_DAILY_MAX_ROWS, build_request
from tapehub.providers.tushare.response_mapper import to_standard_bars

if TYPE_CHECKING:
    pass

# 各接口单次返回上限（达到即无法确认是否截断）
_ROW_LIMITS = {"pro_bar": None, "fut_daily": FUT_DAILY_MAX_ROWS}


class ProviderAdapter(BaseProviderAdapter):
    """Tushare 适配器。"""

    implemented = True

    async def fetch_bars(self, query: BarQuery) -> BarBatch:
        operation = self.resolve_operation(query)
        self._ensure_ready()
        return await call_in_thread(self._fetch_sync, operation, query)

    def _ensure_ready(self) -> str:
        """校验运行前提：Token 配置。"""
        token = self._token()
        if not token:
            raise ProviderNotImplementedError(
                "Tushare token is not configured.",
                details={"provider": self.name, "setting": "TAPEHUB_TUSHARE_TOKEN"},
            )
        return token

    def _token(self) -> str:
        value = getattr(self.settings, "tushare_token", "") if self.settings else ""
        return value or ""

    def _fetch_sync(self, operation: str, query: BarQuery) -> BarBatch:
        frame = self._call_upstream(self._token(), operation, query)
        rows = self._to_rows(frame)
        row_limit = _ROW_LIMITS.get(operation)
        partial = row_limit is not None and len(rows) >= row_limit
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
            partial=partial,
        )

    def _call_upstream(self, token: str, operation: str, query: BarQuery) -> Any:
        """先完成参数映射，再延迟导入官方 SDK 调用对应接口。

        pro_bar 为模块级函数，fut_daily 为 pro 对象方法。
        """
        kwargs = build_request(operation, query)
        try:
            import tushare as ts
        except ImportError as exc:
            raise ProviderNotImplementedError(
                "Provider SDK 'tushare' is not installed.",
                details={"provider": self.name, "sdk": "tushare"},
            ) from exc
        pro = ts.pro_api(token)
        if operation == "pro_bar":
            return ts.pro_bar(api=pro, **kwargs)
        if operation == "fut_daily":
            return pro.fut_daily(**kwargs)
        raise ProviderNotImplementedError(
            f"unknown operation {operation!r}",
            details={"provider": self.name, "operation": operation},
        )

    def _to_rows(self, frame: Any) -> list[dict[str, Any]]:
        """上游 DataFrame → 字典行列表；空结果（含 None）返回空列表。"""
        if frame is None:
            return []
        return list(frame.to_dict("records"))
