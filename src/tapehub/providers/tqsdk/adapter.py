"""TqSdk 适配器（已实现）。

约定：

- 官方 Python SDK ``tqsdk`` 是可选依赖：SDK 只在取数路径内部延迟导入，
  未安装时返回 PROVIDER_NOT_IMPLEMENTED，不影响服务启动；
- 快期账户凭据（TqAuth）通过 ``TAPEHUB_TQSDK_AUTH_USER`` /
  ``TAPEHUB_TQSDK_AUTH_PASSWORD`` 环境变量注入（pydantic-settings），
  只经内存传给 SDK 客户端，禁止写入代码、manifest、日志或异常响应；
- 连接模式 ``tqsdk_conn_mode``（settings）：
  - ``per_request``（默认）：每次请求在线程池中独立创建并关闭 TqApi
    （同步阻塞经 ``call_in_thread`` 隔离），无跨请求状态；
  - ``shared``：进程内唯一 ``TqSdkRuntime`` 长连接（懒启动），请求提交
    为并发协程；取数在 Runtime 线程完成，不占用线程池线程；
  两种模式下标准化与语义完全一致（见下）；
- get_kline_serial 从“最新一根”往回取 data_length 根：当返回数量达到
  上限且最早一根仍晚于请求 start 时，无法覆盖请求窗口，标记 partial=true，
  API 层据此在 warnings 中提示可能截断；
- 统一分页由共享 ``paginate_bars`` 完成，闭区间 [start, end] 在标准化后复核；
- 上游 SDK 异常类型未在文档中核实，统一由应用层包装为 UPSTREAM_ERROR。
"""

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any

from tapehub.domain.errors import ProviderNotImplementedError
from tapehub.domain.models import CN_TIMEZONE, BarBatch, BarQuery
from tapehub.providers.base import BaseProviderAdapter, call_in_thread
from tapehub.providers.pagination import paginate_bars
from tapehub.providers.tqsdk.request_mapper import UPSTREAM_MAX_BARS, build_request
from tapehub.providers.tqsdk.response_mapper import to_standard_bars
from tapehub.providers.tqsdk.runtime import TqSdkRuntime

if TYPE_CHECKING:
    from tapehub.settings import Settings

_DEFAULT_CONN_MODE = "per_request"


class ProviderAdapter(BaseProviderAdapter):
    """TqSdk 适配器。"""

    implemented = True

    def __init__(self, name: str, manifest: Any, settings: "Settings | None" = None) -> None:
        super().__init__(name, manifest, settings)
        self._runtime: TqSdkRuntime | None = None
        self._runtime_lock = asyncio.Lock()

    async def fetch_bars(self, query: BarQuery) -> BarBatch:
        self.resolve_operation(query)
        auth = self._ensure_ready()
        kwargs = build_request(query)
        if self._conn_mode() == "shared":
            rows = await self._shared_rows(auth, kwargs)
        else:
            rows = await call_in_thread(self._fetch_rows_per_request, auth, kwargs)
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
            received_at=datetime.now(CN_TIMEZONE),
            offset=query.offset,
            limit=query.limit,
            partial=partial,
        )

    def shutdown_runtime(self) -> None:
        """停机钩子（由应用 lifespan 调用）：关闭共享运行时（若已启动）。"""
        if self._runtime is not None:
            self._runtime.shutdown()

    # ------------------------------------------------------------------
    # 连接模式
    # ------------------------------------------------------------------

    def _conn_mode(self) -> str:
        value = getattr(self.settings, "tqsdk_conn_mode", _DEFAULT_CONN_MODE)
        return value or _DEFAULT_CONN_MODE

    def _max_inflight(self) -> int:
        return int(getattr(self.settings, "tqsdk_max_inflight_fetch", 16) or 16)

    async def _shared_rows(
        self, auth: tuple[str, str], kwargs: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """shared 模式取数：懒启动 Runtime（构造在线程内，不阻塞事件循环）。"""
        async with self._runtime_lock:
            if self._runtime is None:
                user, password = auth
                runtime = TqSdkRuntime(
                    auth_user=user,
                    auth_password=password,
                    max_inflight=self._max_inflight(),
                )
                # start 阻塞至构造完成（实测 ~1s），放到线程里避免冻结事件循环
                await call_in_thread(runtime.start)
                self._runtime = runtime
        return await self._runtime.fetch_async(kwargs)

    # ------------------------------------------------------------------
    # 运行前提与配置
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # per_request 路径（每请求独立实例）
    # ------------------------------------------------------------------

    def _fetch_rows_per_request(
        self, auth: tuple[str, str], kwargs: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """延迟导入官方 SDK：每请求独立创建并关闭实例（线程池中执行）。"""
        try:
            from tqsdk import TqApi, TqAuth
        except ImportError as exc:
            raise ProviderNotImplementedError(
                "Provider SDK 'tqsdk' is not installed.",
                details={"provider": self.name, "sdk": "tqsdk"},
            ) from exc
        user, password = auth
        api = TqApi(auth=TqAuth(user, password), disable_print=True)
        try:
            frame = api.get_kline_serial(**kwargs)
        finally:
            api.close()
        if frame is None:
            return []
        return list(frame.to_dict("records"))
