"""实时快照流服务（StreamHub，SSE 行情流的应用层编排）。

流程：校验后的统一请求 → 客户端容量闸门 → ProviderRouter.select_quote
选择数据源并具体化变体 → 适配器（``QuoteStreamProvider`` 端口，鸭子类型
探测）产出领域事件流 → 本服务叠加流式通用语义后交给 API 层。

流式通用语义（与 Provider 无关）：

- **客户端容量闸门**：并发流数达 ``max_clients`` 即 ``STREAM_LIMIT_REACHED``；
- **心跳帧**：``heartbeat_seconds`` 内无任何事件则发一帧保活（默认 15s，
  代理/负载均衡的空闲连接超时不至于掐断流）；
- **空闲退订**：``idle_timeout_seconds`` 内无行情事件（心跳不算）时，
  服务端发 ``idle_unsubscribed`` 并结束流——释放订阅引用，客户端可重连；
- **优雅停机**：``shutdown()`` 拒绝新流并唤醒全部活跃流，以
  ``upstream_disconnected`` 事件显式终结（绝不静默断流）；
- **错误事件化**：流中（响应头已发出后）的适配器异常不再向上抛
  （无法转换为统一 JSON 错误），统一转换为 ``upstream_disconnected``
  事件后正常收尾。

订阅引用计数由适配器侧维护（端口约定：迭代器关闭即释放订阅）；
本服务只做流级编排，不感知 Provider 与标的专用代码。
"""

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tapehub.application.provider_router import ProviderRouter
from tapehub.domain.errors import StreamLimitReachedError, UnsupportedCapabilityError
from tapehub.domain.models import QuoteQuery, QuoteStreamEvent, StreamStatusKind

if TYPE_CHECKING:
    from tapehub.providers.registry import ProviderRecord

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QuoteStream:
    """已受理的流：事件迭代器、实际执行的查询（variant 已具体化）与数据源。"""

    events: AsyncIterator[QuoteStreamEvent]
    query: QuoteQuery
    provider: str


@dataclass(eq=False)
class _ClientSlot:
    """单个活跃流的登记项：停机唤醒句柄 + 幂等配额释放。

    ``eq=False`` 保持恒等哈希：以实例身份登记在服务端集合中。
    """

    event: asyncio.Event = field(default_factory=asyncio.Event)
    released: bool = False


class _StreamIterator:
    """暴露给调用方的事件迭代器：关闭即释放客户端配额。

    直接返回 ``_drive`` 生成器有一个配额泄漏死角：生成器**从未启动**时
    ``aclose()`` 是 no-op（``finally`` 不执行）。本包装在 ``aclose`` 中
    显式释放（幂等），生成器自身的 ``finally`` 同样调用同一释放函数，
    两条路径互为兜底；``__del__`` 再兜「受理后从未被消费即被丢弃」的
    最后一层（断连抢在首次 ``__anext__`` 之前的窗口）。
    """

    def __init__(self, gen: AsyncIterator[QuoteStreamEvent], release: Callable[[], None]):
        self._gen = gen
        self._release = release
        self._inflight: asyncio.Task[QuoteStreamEvent] | None = None

    def __aiter__(self) -> "_StreamIterator":
        return self

    async def __anext__(self) -> QuoteStreamEvent:
        self._inflight = asyncio.current_task()
        try:
            return await self._gen.__anext__()
        finally:
            self._inflight = None

    async def aclose(self) -> None:
        try:
            # 在飞 __anext__（另一任务挂在生成器上）先取消并等传播完成再
            # aclose——否则「generator is already executing」（与 _drive
            # finally 的 cancel-await-aclose 先例同款）
            task = self._inflight
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, RuntimeError):
                    await task
            await self._gen.aclose()  # 生成器未启动时为 no-op，不抛错
        finally:
            self._release()

    def __del__(self) -> None:
        # GC 兜底（幂等）：正常路径由 aclose/finally 覆盖，本路径只收
        # 「从未消费即被丢弃」的窗口
        try:
            self._release()
        except Exception:  # noqa: BLE001 - 析构路径绝不抛错
            pass


class QuoteStreamService:
    """实时快照流应用服务。"""

    def __init__(
        self,
        router: ProviderRouter,
        *,
        max_clients: int = 8,
        heartbeat_seconds: float = 15.0,
        idle_timeout_seconds: float = 300.0,
    ) -> None:
        self._router = router
        self._max_clients = max_clients
        self._heartbeat_seconds = heartbeat_seconds
        self._idle_timeout_seconds = idle_timeout_seconds
        self._lock = threading.Lock()
        self._clients = 0
        self._slots: set[_ClientSlot] = set()
        self._closed = False

    @property
    def client_count(self) -> int:
        """当前活跃流数（容量观测，供测试与运维）。"""
        with self._lock:
            return self._clients

    def open(self, query: QuoteQuery) -> QuoteStream:
        """受理一条流：急切执行路由与容量闸门（错误统一 JSON 响应）。

        返回的迭代器由调用方消费/关闭；关闭（``aclose``）即释放客户端
        配额与订阅引用。
        """
        selection = self._router.select_quote(query)
        record: ProviderRecord = selection.record
        stream_fn = getattr(record.adapter, "stream_quote", None)
        if stream_fn is None:
            raise UnsupportedCapabilityError(
                "Provider does not implement quote streaming.",
                details={"provider": record.name},
            )
        final_query = (
            query
            if query.variant == selection.variant
            else query.model_copy(update={"variant": selection.variant})
        )
        slot = _ClientSlot()
        # 容量检查与计数在同一临界区：并发 open 不会越过上限
        with self._lock:
            if self._closed:
                raise UnsupportedCapabilityError(
                    "Streaming is shutting down.",
                    details={"reason": "shutdown"},
                )
            if self._clients >= self._max_clients:
                raise StreamLimitReachedError(details={"limit": self._max_clients})
            self._clients += 1
            self._slots.add(slot)
        return QuoteStream(
            events=_StreamIterator(
                self._drive(stream_fn(final_query), slot=slot),
                release=lambda: self._release(slot),
            ),
            query=final_query,
            provider=record.name,
        )

    async def shutdown(self) -> None:
        """优雅停机：拒绝新流并唤醒全部活跃流（随后以事件显式终结）。"""
        with self._lock:
            self._closed = True
            slots = list(self._slots)
        for slot in slots:
            slot.event.set()

    async def _drive(
        self, upstream: AsyncIterator[QuoteStreamEvent], *, slot: _ClientSlot
    ) -> AsyncIterator[QuoteStreamEvent]:
        """叠加心跳、空闲退订与停机终结的流驱动循环。"""
        next_task: asyncio.Task[QuoteStreamEvent] | None = None
        last_data_at = time.monotonic()
        got_first_event = False
        try:
            while True:
                if next_task is None:
                    next_task = asyncio.ensure_future(upstream.__anext__())
                wake_task = asyncio.ensure_future(slot.event.wait())
                try:
                    done, _pending = await asyncio.wait(
                        (next_task, wake_task),
                        timeout=self._heartbeat_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    wake_task.cancel()
                if slot.event.is_set():
                    # 停机：显式事件终结，绝不静默断流
                    yield QuoteStreamEvent(status=StreamStatusKind.UPSTREAM_DISCONNECTED)
                    return
                if next_task.done():
                    try:
                        event = next_task.result()
                    except StopAsyncIteration:
                        return
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - 流中错误事件化收尾
                        logger.warning(
                            "quote stream upstream failed: %s: %s", type(exc).__name__, exc
                        )
                        yield QuoteStreamEvent(status=StreamStatusKind.UPSTREAM_DISCONNECTED)
                        return
                    next_task = None
                    got_first_event = True
                    if not event.heartbeat:
                        last_data_at = time.monotonic()
                    yield event
                    continue
                # 心跳窗口耗尽：空闲退订判定 → 心跳帧
                if time.monotonic() - last_data_at >= self._idle_timeout_seconds:
                    yield QuoteStreamEvent(status=StreamStatusKind.IDLE_UNSUBSCRIBED)
                    return
                if got_first_event:
                    # 心跳只出现在首事件之后：保证流的第一帧是 status
                    # （订阅确认/错误），不被保活帧抢先
                    yield QuoteStreamEvent(heartbeat=True)
        finally:
            if next_task is not None and not next_task.done():
                next_task.cancel()
                # 等待取消传播完成（上游生成器在挂起点收尾并释放订阅），
                # 否则紧接着的 aclose 会撞上「generator is already running」；
                # 已结束的任务（正常/异常）无需等待，await 会重抛其异常
                with contextlib.suppress(asyncio.CancelledError):
                    await next_task
            self._release(slot)
            try:
                await upstream.aclose()
            except Exception as exc:  # noqa: BLE001 - 清理异常只记录
                logger.warning("quote stream close failed: %s: %s", type(exc).__name__, exc)

    def _release(self, slot: _ClientSlot) -> None:
        """幂等释放客户端配额（生成器 finally 与迭代器 aclose 双路径兜底）。"""
        with self._lock:
            if slot.released:
                return
            slot.released = True
            self._clients -= 1
            self._slots.discard(slot)
