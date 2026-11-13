"""TqSdk 适配器（已实现：历史 K 线 + 实时快照 + 实时快照流）。

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
- 实时快照流（M3，``stream_quote``）：仅 shared 模式支持（流依赖长连接
  订阅；per_request 返回 UNSUPPORTED_CAPABILITY）。Runtime 流监听器
  事件经 ``loop.call_soon_threadsafe`` 桥接进本适配器的有界队列，队列满
  丢旧保新（行情「最新」比「完整」有价值，见 README）；迭代器关闭时
  释放订阅引用并注销监听器；
- get_kline_serial 从“最新一根”往回取 data_length 根：当返回数量达到
  上限且最早一根仍晚于请求 start 时，无法覆盖请求窗口，标记 partial=true，
  API 层据此在 warnings 中提示可能截断；
- 统一分页由共享 ``paginate_bars`` 完成，闭区间 [start, end] 在标准化后复核；
- 上游 SDK 异常类型未在文档中核实，统一由应用层包装为 UPSTREAM_ERROR。
"""

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import datetime
from typing import TYPE_CHECKING, Any

from tapehub.domain.errors import (
    ProviderNotImplementedError,
    QuoteNotReadyError,
    UnsupportedCapabilityError,
)
from tapehub.domain.models import (
    CN_TIMEZONE,
    BarBatch,
    BarQuery,
    Quote,
    QuoteQuery,
    QuoteStreamEvent,
    StreamStatusKind,
)
from tapehub.providers.base import BaseProviderAdapter, call_in_thread
from tapehub.providers.pagination import paginate_bars
from tapehub.providers.stream_buffer import StreamBuffer
from tapehub.providers.tqsdk.request_mapper import (
    UPSTREAM_MAX_BARS,
    build_request,
    quote_symbol,
)
from tapehub.providers.tqsdk.response_mapper import to_standard_bars, to_standard_quote
from tapehub.providers.tqsdk.runtime import RuntimeStatus, TqSdkRuntime

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
            rows = await call_in_thread(
                self._fetch_rows_per_request, auth, kwargs, abandon_on_cancel=True
            )
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
        """实时快照：按请求租约订阅（读 Runtime 最新缓存或等首包）。

        租约语义：请求结束（成功/首包超时/客户端断开）即退订——引用归零后
        Quote 转入休眠账本、缓存丢弃，``TAPEHUB_TQSDK_SUBSCRIPTION_LIMIT``
        的休眠回收阀门保持可达（否则纯 M2 流量下 refs 永不归零、回收永不
        触发）。重复请求经休眠唤醒即时返回新鲜快照：unsubscribe 归零即丢弃
        缓存（``latest`` 此后返回 None），唤醒时 ``_handle_subscribe`` 发布
        Quote 当前值（上游持续推送，唤醒即新鲜），无陈旧快照问题。
        """
        self.resolve_quote_operation(query)
        auth = self._ensure_ready()
        symbol = quote_symbol(query)
        if self._conn_mode() == "shared":
            runtime = await self._ensure_runtime(auth)
            runtime.subscribe(symbol)
            try:
                snapshot = await self._await_snapshot(runtime, symbol)
            finally:
                runtime.unsubscribe(symbol)
        else:
            snapshot = await call_in_thread(
                self._fetch_quote_per_request, auth, symbol, abandon_on_cancel=True
            )
        return to_standard_quote(
            snapshot, provider=self.name, received_at=datetime.now(CN_TIMEZONE)
        )

    def shutdown_runtime(self) -> None:
        """停机钩子（由应用 lifespan 调用）：关闭共享运行时（若已启动）。

        同步完成（内部 join 上限 10s）：tqsdk 停机必须等待线程 close 完整
        TqApi，停机期短暂阻塞事件循环属可接受代价；返回 None（无待 await
        的停机任务，与 twelvedata 的异步钩子协议兼容）。
        """
        if self._runtime is not None:
            self._runtime.shutdown()

    @property
    def runtime_status(self) -> str | None:
        """共享运行时生命周期状态（鸭子类型，供能力目录回填）。

        值域即 ``RuntimeStatus``：``idle``（懒启动前）/``starting``/``ready``/
        ``recycling``/``fatal``；``per_request`` 模式无常驻长连接，返回
        ``None``（能力目录回退 ``unknown``，不伪造状态）。
        """
        if self._conn_mode() != "shared":
            return None
        runtime = self._runtime
        if runtime is None:
            return RuntimeStatus.IDLE.value
        return runtime.status.value

    def stream_quote(self, query: QuoteQuery) -> AsyncIterator[QuoteStreamEvent]:
        """实时快照流（M3）：Runtime 订阅 + 流监听器 → 有界队列 → 事件流。

        受理校验（operation 路由、凭据、连接模式、标的映射）在调用时急切
        执行——错误在响应头发出前统一抛出（JSON 错误结构）；Runtime 启动
        与订阅在首次迭代时进行。

        事件桥接：Runtime 线程回调经 ``loop.call_soon_threadsafe`` 投递进
        本流的有界 ``asyncio.Queue``（队列满丢旧保新——行情「最新」比
        「完整」有价值）；迭代器关闭时释放订阅引用并注销监听器。
        """
        self.resolve_quote_operation(query)
        auth = self._ensure_ready()
        if self._conn_mode() != "shared":
            raise UnsupportedCapabilityError(
                "Streaming quotes require the shared runtime connection mode.",
                details={"provider": self.name},
            )
        symbol = quote_symbol(query)
        return self._stream_events(auth, symbol)

    async def _stream_events(
        self, auth: tuple[str, str], symbol: str
    ) -> AsyncIterator[QuoteStreamEvent]:
        runtime = await self._ensure_runtime(auth)
        runtime.subscribe(symbol)
        loop = asyncio.get_running_loop()
        buffer = StreamBuffer(self._stream_queue_size())

        def _publish(kind: str, _symbol: str, snapshot: dict[str, Any] | None) -> None:
            """Runtime 线程回调：调度到事件循环线程入缓冲（不阻塞 tick）。"""
            loop.call_soon_threadsafe(buffer.put, kind, snapshot)

        remove_listener = runtime.add_stream_listener(symbol, _publish)
        try:
            while True:
                kind, snapshot = await buffer.get()
                if kind == "quote":
                    if snapshot is not None:
                        yield QuoteStreamEvent(quote=self._stream_quote_event(snapshot))
                else:
                    yield QuoteStreamEvent(status=StreamStatusKind(kind))
                    if kind == "subscribed" and snapshot is not None:
                        # 订阅确认先于首帧快照（首事件 status，随后 quote）
                        yield QuoteStreamEvent(quote=self._stream_quote_event(snapshot))
        finally:
            remove_listener()
            runtime.unsubscribe(symbol)

    def _stream_quote_event(self, snapshot: dict[str, Any]) -> Quote:
        """流帧快照标准化（与 M2 快照同 mapper、同北京时间口径）。"""
        return to_standard_quote(
            snapshot, provider=self.name, received_at=datetime.now(CN_TIMEZONE)
        )

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

    def _stream_queue_size(self) -> int:
        return int(getattr(self.settings, "stream_queue_size", 64) or 64)

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
                try:
                    await call_in_thread(runtime.start)
                except BaseException:
                    # 取消（客户端断开/请求超时）或构造失败：anyio shield 下
                    # 线程已完成 start——TqApi 连接与 runtime 线程可能已带起
                    # 但引用永不落账成孤儿。非阻塞收编：停止标志入队（join(0)
                    # 立即返回），tick 线程随后自行干净退出
                    runtime.shutdown(timeout=0)
                    raise
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
