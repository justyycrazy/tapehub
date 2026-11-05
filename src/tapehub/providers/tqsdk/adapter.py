"""TqSdk 适配器（已实现：历史 K 线 + 实时快照）。

约定：

- 官方 Python SDK ``tqsdk`` 是可选依赖：SDK 只在取数路径内部延迟导入，
  未安装时返回 PROVIDER_NOT_IMPLEMENTED，不影响服务启动；
- 快期账户凭据（TqAuth）通过 ``TAPEHUB_TQSDK_AUTH_USER`` /
  ``TAPEHUB_TQSDK_AUTH_PASSWORD`` 环境变量注入（pydantic-settings），
  只经内存传给 SDK 客户端，禁止写入代码、manifest、日志或异常响应；
- 连接模式 ``tqsdk_conn_mode``（settings）：
  - ``per_request``：每次请求在线程池中独立创建并关闭 TqApi
    （同步阻塞经 ``call_in_thread`` 隔离），无跨请求状态；
  - ``shared``（默认）：进程内唯一 ``TqSdkRuntime`` 长连接（懒启动），
    K 线请求提交为并发协程，取数在 Runtime 线程完成，不占线程池线程；
  两种模式下标准化与语义完全一致（见下）；
- 实时快照（M2）：shared 模式经 Runtime 订阅（引用计数 + 首包等待），
    订阅存活期间快照由 tick 循环持续保鲜，重复请求即时返回；M2 快照
    请求不退订（快照是订阅状态而非一次性取数，退订由 SSE 流式与回收
    阈值管理）。首包在 ``tqsdk_first_snapshot_timeout`` 内未到达则抛
    ``QUOTE_NOT_READY``；per_request 模式退化为每请求独立订阅取快照；
- get_kline_serial 从“最新一根”往回取 data_length 根：当返回数量达到
  上限且最早一根仍晚于请求 start 时，无法覆盖请求窗口，标记 partial=true，
  API 层据此在 warnings 中提示可能截断；
- 统一分页由共享 ``paginate_bars`` 完成，闭区间 [start, end] 在标准化后复核；
- 上游 SDK 异常类型未在文档中核实，统一由应用层包装为 UPSTREAM_ERROR。
"""

import asyncio
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from tapehub.domain.errors import ProviderNotImplementedError, QuoteNotReadyError
from tapehub.domain.models import CN_TIMEZONE, BarBatch, BarQuery, Quote, QuoteQuery
from tapehub.providers.base import BaseProviderAdapter, call_in_thread
from tapehub.providers.pagination import paginate_bars
from tapehub.providers.tqsdk.request_mapper import (
    UPSTREAM_MAX_BARS,
    build_request,
    quote_symbol,
)
from tapehub.providers.tqsdk.response_mapper import to_standard_bars, to_standard_quote
from tapehub.providers.tqsdk.runtime import TqSdkRuntime

if TYPE_CHECKING:
    from tapehub.settings import Settings

_DEFAULT_CONN_MODE = "per_request"
# 快照首包等待的轮询间隔（秒）：M2 快照链路为轮询语义，SSE（M3）改为推送
_SNAPSHOT_POLL_INTERVAL = 0.05


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

    async def fetch_quote(self, query: QuoteQuery) -> Quote:
        """实时快照：读 Runtime 最新缓存；未订阅则订阅并等待首包。"""
        self.resolve_quote_operation(query)
        auth = self._ensure_ready()
        symbol = quote_symbol(query)
        if self._conn_mode() == "shared":
            runtime = await self._ensure_runtime(auth)
            runtime.subscribe(symbol)
            snapshot = await self._await_snapshot(runtime, symbol)
        else:
            snapshot = await call_in_thread(self._fetch_quote_per_request, auth, symbol)
        return to_standard_quote(
            snapshot, provider=self.name, received_at=datetime.now(CN_TIMEZONE)
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

    def _first_snapshot_timeout(self) -> float:
        return float(getattr(self.settings, "tqsdk_first_snapshot_timeout", 5.0) or 5.0)

    def _subscription_limit(self) -> int:
        return int(getattr(self.settings, "tqsdk_subscription_limit", 64) or 64)

    async def _ensure_runtime(self, auth: tuple[str, str]) -> TqSdkRuntime:
        """shared 模式：懒启动进程内唯一 Runtime（构造在线程内，不阻塞事件循环）。"""
        async with self._runtime_lock:
            if self._runtime is None:
                user, password = auth
                runtime = TqSdkRuntime(
                    auth_user=user,
                    auth_password=password,
                    max_inflight=self._max_inflight(),
                    subscription_limit=self._subscription_limit(),
                )
                # start 阻塞至构造完成（实测 ~1s），放到线程里避免冻结事件循环
                await call_in_thread(runtime.start)
                self._runtime = runtime
        return self._runtime

    async def _shared_rows(
        self, auth: tuple[str, str], kwargs: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """shared 模式取数。"""
        runtime = await self._ensure_runtime(auth)
        return await runtime.fetch_async(kwargs)

    async def _await_snapshot(
        self, runtime: TqSdkRuntime, symbol: str
    ) -> dict[str, Any]:
        """等待首包快照；超时抛 ``QUOTE_NOT_READY``（不编造部分字段快照）。"""
        timeout = self._first_snapshot_timeout()
        deadline = time.monotonic() + timeout
        while True:
            snapshot = runtime.latest(symbol)
            if snapshot is not None:
                return snapshot
            if time.monotonic() >= deadline:
                raise QuoteNotReadyError(
                    "Quote snapshot not ready within the first-packet timeout.",
                    details={
                        "provider": self.name,
                        "symbol": symbol,
                        "timeout_s": timeout,
                    },
                )
            await asyncio.sleep(_SNAPSHOT_POLL_INTERVAL)

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

    def _fetch_quote_per_request(
        self, auth: tuple[str, str], symbol: str
    ) -> dict[str, Any]:
        """per_request 快照（回滚模式）：独立实例订阅取首包后立即关闭。

        真实 SDK 的同步 get_quote 会阻塞至首包（内部 wait_update 驱动，
        上游 30s 兜底超时）；快照在 close 之前完成拷贝。
        """
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
            quote = api.get_quote(symbol)
            snapshot = dict(quote)
        finally:
            api.close()
        return snapshot
