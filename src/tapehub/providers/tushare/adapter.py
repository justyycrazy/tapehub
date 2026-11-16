"""Tushare 适配器（已实现）。

映射规则与上游行为定案见 docs/providers/tushare.md（唯一出处，本文不复述）。
官方文档：https://tushare.pro/document/2。
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any

from tapehub.domain.errors import ProviderNotImplementedError
from tapehub.domain.models import CN_TIMEZONE, BarBatch, BarQuery
from tapehub.providers.base import BaseProviderAdapter, call_in_thread
from tapehub.providers.pagination import paginate_bars
from tapehub.providers.tushare.request_mapper import (
    FUND_DAILY_MAX_ROWS,
    FUT_DAILY_MAX_ROWS,
    OPT_DAILY_MAX_ROWS,
    build_request,
)
from tapehub.providers.tushare.response_mapper import to_standard_bars

if TYPE_CHECKING:
    pass

# 各接口单次返回上限（达到即无法确认是否截断）
_ROW_LIMITS = {
    "pro_bar": None,
    "fut_daily": FUT_DAILY_MAX_ROWS,
    "fund_daily": FUND_DAILY_MAX_ROWS,
    "opt_daily": OPT_DAILY_MAX_ROWS,
}


class ProviderAdapter(BaseProviderAdapter):
    """Tushare 适配器。"""

    implemented = True

    async def fetch_bars(self, query: BarQuery) -> BarBatch:
        operation = self.resolve_operation(query)
        self._ensure_ready()
        return await call_in_thread(self._fetch_sync, operation, query, abandon_on_cancel=True)

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
        bars = to_standard_bars(operation, query.interval, rows)
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
            partial=partial,
        )

    def _call_upstream(self, token: str, operation: str, query: BarQuery) -> Any:
        """先完成参数映射，再延迟导入官方 SDK 调用对应接口。

        pro_bar 为模块级函数（股票/指数），fut_daily/fund_daily/opt_daily
        为 pro 对象方法（期货/ETF/期权）。
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
        if operation in ("fut_daily", "fund_daily", "opt_daily"):
            return getattr(pro, operation)(**kwargs)
        raise ProviderNotImplementedError(
            f"unknown operation {operation!r}",
            details={"provider": self.name, "operation": operation},
        )

    def _to_rows(self, frame: Any) -> list[dict[str, Any]]:
        """上游 DataFrame → 字典行列表；空结果（含 None）返回空列表。"""
        if frame is None:
            return []
        return list(frame.to_dict("records"))
