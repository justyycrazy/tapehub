"""TqSdk 适配器（已实现）。

约定：

- 官方 Python SDK ``tqsdk`` 是可选依赖：SDK 只在 ``_fetch_sync`` 内部
  延迟导入，未安装时返回 PROVIDER_NOT_IMPLEMENTED，不影响服务启动；
- 快期账户凭据（TqAuth）通过 ``TAPEHUB_TQSDK_AUTH_USER`` /
  ``TAPEHUB_TQSDK_AUTH_PASSWORD`` 环境变量注入（pydantic-settings），
  只经内存传给 SDK 客户端，禁止写入代码、manifest、日志或异常响应；
- TqApi 为同步 SDK 且一个线程只应有一个实例：每次请求在线程池中
  独立创建并关闭实例（同步阻塞通过 ``call_in_thread`` 隔离）；
- get_kline_serial 从“最新一根”往回取 data_length 根：当返回数量达到
  上限且最早一根仍晚于请求 start 时，无法覆盖请求窗口，标记 partial=true，
  API 层据此在 warnings 中提示可能截断；
- 统一分页由共享 ``paginate_bars`` 完成，闭区间 [start, end] 在标准化后复核；
- 上游 SDK 异常类型未在文档中核实，统一由应用层包装为 UPSTREAM_ERROR。
"""

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from tapehub.domain.errors import ProviderNotImplementedError
from tapehub.domain.models import BarBatch, BarQuery
from tapehub.providers.base import BaseProviderAdapter, call_in_thread
from tapehub.providers.pagination import paginate_bars
from tapehub.providers.tqsdk.request_mapper import UPSTREAM_MAX_BARS, build_request
from tapehub.providers.tqsdk.response_mapper import to_standard_bars

if TYPE_CHECKING:
    pass


class ProviderAdapter(BaseProviderAdapter):
    """TqSdk 适配器。"""

    implemented = True

    async def fetch_bars(self, query: BarQuery) -> BarBatch:
        operation = self.resolve_operation(query)
        auth = self._ensure_ready()
        return await call_in_thread(self._fetch_sync, auth, operation, query)

    def _ensure_ready(self) -> tuple[str, str]:
        """校验运行前提：快期账户用户名与密码均已配置。"""
        user = self._setting("tqsdk_auth_user")
        password = self._setting("tqsdk_auth_password")
        missing = [
            name for name, value in (("user", user), ("password", password)) if not value
        ]
        if missing:
            raise ProviderNotImplementedError(
                "TqSdk auth is not fully configured.",
                details={
                    "provider": self.name,
                    "settings": ["TAPEHUB_TQSDK_AUTH_USER", "TAPEHUB_TQSDK_AUTH_PASSWORD"],
                    "missing": missing,
                },
            )
        return user, password

    def _setting(self, field: str) -> str:
        value = getattr(self.settings, field, "") if self.settings else ""
        return value or ""

    def _fetch_sync(self, auth: tuple[str, str], operation: str, query: BarQuery) -> BarBatch:
        rows = self._fetch_rows(auth, query)
        bars = to_standard_bars(rows)
        # 上游按“最新 N 根”返回，这里按闭区间 [start, end] 精确复核统一时间语义
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

    def _fetch_rows(self, auth: tuple[str, str], query: BarQuery) -> list[dict[str, Any]]:
        """先完成参数映射，再延迟导入官方 SDK 调用 get_kline_serial。"""
        kwargs = build_request(query)
        try:
            from tqsdk import TqApi, TqAuth
        except ImportError as exc:
            raise ProviderNotImplementedError(
                "Provider SDK 'tqsdk' is not installed.",
                details={"provider": self.name, "sdk": "tqsdk"},
            ) from exc
        user, password = auth
        # 一个线程只应有一个 TqApi 实例：每次请求独立创建并关闭
        api = TqApi(auth=TqAuth(user, password), disable_print=True)
        try:
            frame = api.get_kline_serial(**kwargs)
        finally:
            api.close()
        if frame is None:
            return []
        return list(frame.to_dict("records"))
