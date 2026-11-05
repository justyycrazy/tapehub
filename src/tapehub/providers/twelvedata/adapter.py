"""Twelve Data 适配器（已实现：历史 K 线 REST + 实时快照/快照流 WS）。

约定：

- 历史K线走官方 Python 库 ``twelvedata``（twelvedata-python，可选依赖）：
  只在 ``_fetch_rows`` 内部延迟导入，未安装时返回 PROVIDER_NOT_IMPLEMENTED，
  不影响服务启动；同步调用经 ``call_in_thread`` 隔离到线程池；
- 实时快照（M2）与快照流（M3）共用 WS 共享运行时
  （``wss://ws.twelvedata.com/v1/quotes/price``，asyncio 原生、单连接，
  ``websockets`` 为可选依赖延迟导入——未安装同样 PROVIDER_NOT_IMPLEMENTED）。
  上游行为按 2026-11-05 探针实证（报告 probes/results/twelvedata/
  ws-behavior-20261105T221000+0800.md，不入库）：WS credit = 当前活跃
  订阅 symbol 数（退订即时释放、重订重连免费）；上游支持逐 symbol 真退订，
  无 tqsdk 式代际回收（不产生 ``recycling`` 事件）；
- API Key 通过 ``TAPEHUB_TWELVEDATA_API_KEY`` 环境变量注入
  （pydantic-settings），只经内存传给 SDK/连接 URL，禁止写入代码、
  manifest、日志或异常响应；
- 容量闸门：并发订阅 symbol 数达 ``twelvedata_subscription_limit``
  （默认 8，免费版 WS credit 口径）时急切返回
  ``UPSTREAM_SUBSCRIPTION_LIMIT``（HTTP 429，SSE 响应头之前）；
- M2 快照：已活跃订阅直接读缓存即时返回；冷订阅等待首包，超过
  ``twelvedata_first_snapshot_timeout`` 返回 ``QUOTE_NOT_READY``（408）；
  取数后引用归零，延迟退订释放 credit（见 runtime 注释）；
- M3 流：受理校验急切执行（错误为 JSON 结构），订阅在首次迭代进行；
  事件经共享 ``StreamBuffer``（coalesce 丢旧保新只约束 quote 帧）；
  迭代器关闭释放引用并注销监听器；
- 上游 datetime 墙钟口径按资产类型区分（2026-11-04 实测 + 官方文档）：
  外汇 Australia/Sydney（AEDT/AEST），加密货币 UTC；解析见 response_mapper，
  统一北京时间归一化由领域层 BarBatch 完成；
- 上游 outputsize 上限 5000 且按“最新一根”往回返回：采用覆盖判定——
  返回数量达到上限且最早一根仍晚于请求 start 时标记 partial=true，
  API 层据此在 warnings 中提示可能截断；
- values 默认倒序，统一升序排序与分页由共享 ``paginate_bars`` 完成，
  闭区间 [start, end] 在标准化后复核；
- 上游 SDK/HTTP 异常类型未在文档中核实，统一由应用层包装为 UPSTREAM_ERROR；
  唯一实测核实的例外（2026-11-04 探针实测）：区间内无数据时上游返回 400
  BadRequestError（指纹见 request_mapper.UPSTREAM_NO_DATA_FINGERPRINT），
  属上游权威的无数据结论，适配器归一化为空列表而非报错。
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from typing import TYPE_CHECKING, Any

from tapehub.domain.errors import (
    ProviderNotImplementedError,
    QuoteNotReadyError,
    UpstreamSubscriptionLimitError,
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
from tapehub.providers.twelvedata.request_mapper import (
    UPSTREAM_MAX_BARS,
    UPSTREAM_NO_DATA_FINGERPRINT,
    build_request,
    td_symbol,
)
from tapehub.providers.twelvedata.response_mapper import to_standard_bars, ws_price_to_quote
from tapehub.providers.twelvedata.runtime import TwelvedataWsRuntime

if TYPE_CHECKING:
    from tapehub.settings import Settings


class ProviderAdapter(BaseProviderAdapter):
    """Twelve Data 适配器。"""

    implemented = True

    def __init__(self, name: str, manifest: Any, settings: "Settings | None" = None) -> None:
        super().__init__(name, manifest, settings)
        self._runtime: TwelvedataWsRuntime | None = None
        self._runtime_lock = asyncio.Lock()

    async def fetch_bars(self, query: BarQuery) -> BarBatch:
        operation = self.resolve_operation(query)
        api_key = self._ensure_ready()
        return await call_in_thread(self._fetch_sync, api_key, operation, query)

    async def fetch_quote(self, query: QuoteQuery) -> Quote:
        """实时快照：WS 订阅缓存（tqsdk 同构）。

        已活跃订阅直接读缓存即时返回；冷订阅等待首包，超时返回
        ``QUOTE_NOT_READY``；取数后引用归零（延迟退订释放 credit）。
        """
        self.resolve_quote_operation(query)
        api_key = self._ensure_ready()
        symbol = td_symbol(query)
        self._ensure_capacity(symbol)
        runtime = await self._ensure_runtime(api_key)
        runtime.subscribe(symbol)
        try:
            snapshot = await self._await_snapshot(runtime, symbol)
        finally:
            runtime.unsubscribe(symbol)
        return ws_price_to_quote(
            snapshot, provider=self.name, received_at=datetime.now(CN_TIMEZONE)
        )

    def stream_quote(self, query: QuoteQuery) -> AsyncIterator[QuoteStreamEvent]:
        """实时快照流（M3）：WS 订阅 + 监听器 → 有界队列 → 事件流。

        受理校验（operation 路由、凭据、标的映射、容量闸门）在调用时
        急切执行——错误在响应头发出前统一抛出（JSON 错误结构）；Runtime
        启动与订阅在首次迭代时进行。断线/重订恢复由 Runtime 以显式
        status 事件通知（无 ``recycling``——无代际回收）。
        """
        self.resolve_quote_operation(query)
        api_key = self._ensure_ready()
        symbol = td_symbol(query)
        self._ensure_capacity(symbol)
        return self._stream_events(api_key, symbol)

    async def _stream_events(
        self, api_key: str, symbol: str
    ) -> AsyncIterator[QuoteStreamEvent]:
        runtime = await self._ensure_runtime(api_key)
        runtime.subscribe(symbol)
        buffer = StreamBuffer(self._stream_queue_size())

        def _publish(kind: str, _symbol: str, snapshot: dict[str, Any] | None) -> None:
            """Runtime 回调（同事件循环）：直接投递进本流的有界缓冲。"""
            buffer.put(kind, snapshot)

        remove_listener = runtime.add_stream_listener(symbol, _publish)
        try:
            while True:
                kind, snapshot = await buffer.get()
                if kind == "quote":
                    if snapshot is not None:
                        yield QuoteStreamEvent(quote=self._quote_event(snapshot))
                else:
                    yield QuoteStreamEvent(status=StreamStatusKind(kind))
                    if kind == "subscribed" and snapshot is not None:
                        # 订阅确认先于首帧快照（首事件 status，随后 quote）
                        yield QuoteStreamEvent(quote=self._quote_event(snapshot))
        finally:
            remove_listener()
            runtime.unsubscribe(symbol)

    def _quote_event(self, snapshot: dict[str, Any]) -> Quote:
        """流帧快照标准化（与 M2 快照同 mapper、同北京时间口径）。"""
        return ws_price_to_quote(
            snapshot, provider=self.name, received_at=datetime.now(CN_TIMEZONE)
        )

    def shutdown_runtime(self) -> None:
        """停机钩子（由应用 lifespan 调用）：关闭 WS Runtime（若已启动）。

        lifespan 在事件循环内同步调用本方法——将异步停机调度为任务，
        由存活的事件循环完成收尾（取消连接任务、通知监听器断流）。
        """
        runtime = self._runtime
        if runtime is None:
            return
        self._runtime = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.create_task(runtime.shutdown(), name="twelvedata-ws-shutdown")
        else:  # 无运行循环的兜底（正常不触发）
            asyncio.run(runtime.shutdown())

    def _ensure_ready(self) -> str:
        """校验运行前提：API Key 配置。"""
        value = getattr(self.settings, "twelvedata_api_key", "") if self.settings else ""
        if not value:
            raise ProviderNotImplementedError(
                "Twelve Data API key is not configured.",
                details={"provider": self.name, "setting": "TAPEHUB_TWELVEDATA_API_KEY"},
            )
        return value

    # ------------------------------------------------------------------
    # WS Runtime 与容量
    # ------------------------------------------------------------------

    async def _ensure_runtime(self, api_key: str) -> TwelvedataWsRuntime:
        """懒启动进程内唯一 WS Runtime（asyncio 原生，不阻塞事件循环）。

        ``start`` 幂等：连接任务已消亡（异常退出/事件循环关闭）时重启，
        ``_run`` 建连后会对全部活跃引用自动重订（探针实证重订免费）。
        """
        async with self._runtime_lock:
            if self._runtime is None:
                self._runtime = TwelvedataWsRuntime(
                    api_key=api_key,
                    subscription_limit=self._subscription_limit(),
                )
            await self._runtime.start()
        return self._runtime

    def _ensure_capacity(self, symbol: str) -> None:
        """容量闸门（急切）：并发订阅 symbol 数达上限即拒绝。

        活跃口径为 Runtime 的 ``active_symbols``（含退订 linger 窗口）；
        Runtime 未启动时视为 0（首个订阅不受限）。存在并发受理的轻微
        竞态（软上限），越界订阅由上游 ``subscribe-status.fails`` 兜底。
        """
        runtime = self._runtime
        if runtime is None:
            return
        active = runtime.active_symbols()
        if symbol not in active and len(active) >= self._subscription_limit():
            raise UpstreamSubscriptionLimitError(
                "Twelve Data concurrent subscription limit reached, retry later.",
                details={
                    "provider": self.name,
                    "limit": self._subscription_limit(),
                    "active": active,
                },
            )

    async def _await_snapshot(
        self, runtime: TwelvedataWsRuntime, symbol: str
    ) -> dict[str, Any]:
        """等待首包快照；超时抛 ``QUOTE_NOT_READY``（不编造部分字段快照）。"""
        timeout = self._first_snapshot_timeout()
        try:
            return await runtime.wait_snapshot(symbol, timeout)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise QuoteNotReadyError(
                "Quote snapshot not ready within the first-packet timeout.",
                details={
                    "provider": self.name,
                    "symbol": symbol,
                    "timeout_s": timeout,
                },
            ) from exc

    def _subscription_limit(self) -> int:
        return int(getattr(self.settings, "twelvedata_subscription_limit", 8) or 8)

    def _first_snapshot_timeout(self) -> float:
        return float(getattr(self.settings, "twelvedata_first_snapshot_timeout", 5.0) or 5.0)

    def _stream_queue_size(self) -> int:
        return int(getattr(self.settings, "stream_queue_size", 64) or 64)

    # ------------------------------------------------------------------
    # 历史 K 线（REST，线程池）
    # ------------------------------------------------------------------

    def _fetch_sync(self, api_key: str, operation: str, query: BarQuery) -> BarBatch:
        rows = self._fetch_rows(api_key, query)
        bars = to_standard_bars(rows, query.asset_type)
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
            received_at=datetime.now(CN_TIMEZONE),
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
        try:
            payload = series.as_json()
        except Exception as exc:  # noqa: BLE001 - 仅按实测指纹归一化空窗，其余照常上抛
            if (
                type(exc).__name__ == "BadRequestError"
                and UPSTREAM_NO_DATA_FINGERPRINT in str(exc)
            ):
                return []
            raise
        if payload is None:
            return []
        return list(payload)
