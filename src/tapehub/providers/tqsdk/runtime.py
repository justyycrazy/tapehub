"""TqSdkRuntime：进程内唯一的 TqApi 长连接运行时（``conn_mode=shared``）。

线程模型、代际生命周期与订阅协议的设计定案见 docs/providers/tqsdk.md
（唯一出处，本文不复述）。

要点：Runtime 独占一个线程按 tick 驱动 tqsdk 私有事件循环；快照只在线程内
``dict(quote)`` 拷贝后发布（外部拿到的是纯 dict）；tqsdk 协议不支持取消
订阅——退订只停止本地快照维护，休眠订阅累计到 ``subscription_limit`` 触发
新一代重建清理，仍被引用的订阅在新一代自动重订。

实时快照流监听器（M3，SSE 数据出口）：``add_stream_listener(symbol,
callback)`` 注册流监听器，callback 在 Runtime 线程被调用，收到
``(kind, symbol, snapshot)``：``subscribed``（首包到达/休眠唤醒/代际重订
完成，携带快照拷贝）、``quote``（is_changing 增量，与快照缓存同源的拷贝）、
``upstream_disconnected``（wait_update 异常/停机）、``recycling``（代际
重建，重订后自动恢复 subscribed）——通知只传线程内已拷贝的纯 dict，回调
异常只记录、绝不阻塞 tick；SSE 客户端经 ``recycling`` → ``subscribed``
事件感知回收窗口，绝不静默断流。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import queue
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from tapehub.domain.errors import ConcurrencyLimitError, UpstreamError
from tapehub.domain.models import CN_TIMEZONE

# watchdog：连续 wait_update 异常达到该次数即触发新一代重建
_WATCHDOG_STREAK_LIMIT = 3
# 新一代重建退避（秒）：上游不可用时避免密集重试
_REBUILD_BACKOFF_SECONDS = 2.0


logger = logging.getLogger("tapehub.tqsdk.runtime")


class _GenerationReset(Exception):
    """内部信号：当前 TqApi 代际作废（跨交易日回收 / watchdog），重建新一代。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RuntimeStatus(StrEnum):
    """Runtime 生命周期状态（供 /v1/capabilities runtime_status 回填）。"""

    IDLE = "idle"  # 未启动（懒启动前）
    STARTING = "starting"  # 线程已启动、TqApi 构造中
    READY = "ready"  # 连接就绪，可接请求
    RECYCLING = "recycling"  # 新一代重建中（在飞已失败，新请求排队等待）
    FATAL = "fatal"  # 构造致命失败，拒绝后续请求


class _StopSignal:
    """停止哨兵：入队后 Runtime 线程在当前 tick 结束时退出。"""


# 流监听器回调签名：(kind, symbol, snapshot)；snapshot 为线程内拷贝的纯 dict
# （subscribed / quote 携带；upstream_disconnected / recycling 为 None）
StreamListener = Callable[[str, str, "dict[str, Any] | None"], None]


class _Subscribe:
    """订阅命令：Runtime 线程在本代 TqApi 上发起 get_quote 订阅。"""

    __slots__ = ("symbol",)

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol


class TqSdkRuntime:
    """进程内唯一的 TqApi 长连接运行时；由 tqsdk 适配器持有与启停。"""

    def __init__(
        self,
        *,
        auth_user: str,
        auth_password: str,
        max_inflight: int = 16,
        tick_seconds: float = 0.2,
        construct_timeout: float = 30.0,
        subscription_limit: int = 64,
    ) -> None:
        self._auth_user = auth_user
        self._auth_password = auth_password
        self._max_inflight = max_inflight
        self._tick_seconds = tick_seconds
        self._construct_timeout = construct_timeout
        self._subscription_limit = subscription_limit

        self._jobs: queue.Queue[
            tuple[dict[str, Any], concurrent.futures.Future] | _StopSignal | _Subscribe
        ] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._stop_requested = False
        self._status = RuntimeStatus.IDLE
        self._last_error: Exception | None = None
        self._inflight = 0
        # 已派发、尚未回填的请求 Future：停机/回收时必须显式失败，避免调用方挂起
        self._pending: set[concurrent.futures.Future] = set()
        self._generation = 0
        # —— 实时快照订阅（M2）——
        # symbol → 订阅引用计数（外部线程增减）
        self._sub_refs: dict[str, int] = {}
        # 本代活跃订阅的 Quote 对象引用（仅 Runtime 线程读写，is_changing 判定用）
        self._live_quotes: dict[str, Any] = {}
        # 本代休眠订阅（引用计数归零但上游推送仍在，等代际回收清理）
        self._dormant_quotes: dict[str, Any] = {}
        # symbol → 最新快照（Runtime 线程内 dict(quote) 拷贝后发布，外部只读）
        self._snapshots: dict[str, dict[str, Any]] = {}
        # —— 实时快照流监听器（M3）——
        # symbol → 监听器集合（外部线程增删；通知发生在 Runtime 线程）
        self._stream_listeners: dict[str, set[StreamListener]] = {}

    # ------------------------------------------------------------------
    # 对外接口（线程安全）
    # ------------------------------------------------------------------

    @property
    def status(self) -> RuntimeStatus:
        with self._lock:
            return self._status

    @property
    def generation(self) -> int:
        """已构造的 TqApi 代数（跨交易日回收与重建会递增）。"""
        with self._lock:
            return self._generation

    @property
    def last_error(self) -> Exception | None:
        with self._lock:
            return self._last_error

    def start(self) -> None:
        """懒启动：创建驱动线程并等待 TqApi 构造完成（就绪或致命失败）。"""
        with self._lock:
            if self._thread is not None or self._stop_requested:
                return
            self._status = RuntimeStatus.STARTING
            self._thread = threading.Thread(
                target=self._thread_main, name="tqsdk-runtime", daemon=True
            )
            self._thread.start()
        if not self._ready.wait(self._construct_timeout):
            self._last_error = UpstreamError(
                "tqsdk runtime construction timed out.",
                details={"timeout_s": self._construct_timeout},
            )
            self._request_stop()

    def fetch(self, kwargs: dict[str, Any]) -> "concurrent.futures.Future[list[dict[str, Any]]]":
        """提交一次取数；返回 concurrent Future（不阻塞调用方线程）。"""
        with self._lock:
            thread = self._thread
            if (
                self._status in (RuntimeStatus.READY, RuntimeStatus.RECYCLING)
                and thread is not None
                and not thread.is_alive()
            ):
                # 驱动线程已死亡（防御：close 异常等不应让调用方无限等待）
                self._status = RuntimeStatus.FATAL
            if self._status in (RuntimeStatus.IDLE, RuntimeStatus.STARTING):
                raise UpstreamError("tqsdk runtime is not ready.")
            if self._status == RuntimeStatus.FATAL:
                raise UpstreamError(
                    "tqsdk runtime is in fatal state.",
                    details={"error": str(self._last_error) if self._last_error else None},
                )
            if self._inflight >= self._max_inflight:
                raise ConcurrencyLimitError(
                    "tqsdk runtime inflight limit reached.",
                    details={"limit": self._max_inflight},
                )
            self._inflight += 1
        cf: concurrent.futures.Future[list[dict[str, Any]]] = concurrent.futures.Future()
        with self._lock:
            self._pending.add(cf)
        self._jobs.put((kwargs, cf))
        return cf

    async def fetch_async(self, kwargs: dict[str, Any]) -> list[dict[str, Any]]:
        """协程封装：在调用方事件循环中等待 Runtime 线程的取数结果。"""
        cf = self.fetch(kwargs)
        return await asyncio.wrap_future(cf)

    # ----------------------------------------------------------------------
    # 实时快照订阅（M2，线程安全）
    # ----------------------------------------------------------------------

    def subscribe(self, symbol: str) -> None:
        """注册一个订阅者（引用计数 +1）；必要时发起上游订阅。

        首个订阅者向 Runtime 线程入队订阅命令；若本代此前订阅过该标的
        （休眠中），唤醒即可复用既有 Quote 引用，不再重复 get_quote。
        tqsdk 协议不支持取消订阅：退订只停止本地快照维护（见模块 docstring）。
        """
        with self._lock:
            if self._status in (RuntimeStatus.IDLE, RuntimeStatus.STARTING):
                raise UpstreamError("tqsdk runtime is not ready.")
            if self._status == RuntimeStatus.FATAL:
                raise UpstreamError(
                    "tqsdk runtime is in fatal state.",
                    details={"error": str(self._last_error) if self._last_error else None},
                )
            refs = self._sub_refs.get(symbol, 0)
            self._sub_refs[symbol] = refs + 1
            # 引用计数 0→1 即需要订阅；已在活跃集合（重复首个订阅命令）由
            # Runtime 线程去重，命令本身幂等
            if refs == 0 and symbol not in self._live_quotes:
                self._jobs.put(_Subscribe(symbol))

    def unsubscribe(self, symbol: str) -> None:
        """注销一个订阅者（引用计数 -1）；最后一个订阅者退订时停止维护。

        停止维护 = 移出活跃集合并丢弃快照（``latest`` 此后返回 None）；
        Quote 引用转入休眠集合，等待休眠订阅回收阈值触发新一代重建清理。
        """
        with self._lock:
            refs = self._sub_refs.get(symbol, 0)
            if refs <= 1:
                self._sub_refs.pop(symbol, None)
                quote = self._live_quotes.pop(symbol, None)
                self._snapshots.pop(symbol, None)
                if quote is not None:
                    self._dormant_quotes[symbol] = quote
            else:
                self._sub_refs[symbol] = refs - 1

    def latest(self, symbol: str) -> dict[str, Any] | None:
        """读取最新快照；未收到首包或已退订返回 ``None``。

        返回的是发布时在线程内拷贝好的纯 dict，调用方可安全持有。
        """
        with self._lock:
            return self._snapshots.get(symbol)

    def add_stream_listener(self, symbol: str, callback: StreamListener) -> Callable[[], None]:
        """注册实时快照流监听器（SSE 数据出口），返回注销句柄。

        callback 通常在 Runtime 线程被调用（见模块 docstring 的事件契约）；
        适配器侧负责经 ``loop.call_soon_threadsafe`` 桥接回自己的事件循环。
        监听器不持有订阅引用：与 ``subscribe``/``unsubscribe`` 配对使用，
        注册表跨代际保留（回收后自动重订恢复推送）。

        注册时若该标的已有可用快照（订阅早已建立、首包已过——如先经
        快照接口查询再开流），立即在调用方线程回调 ``subscribed`` + 当前
        快照拷贝，保证流的首事件总是订阅确认，不静默等待。
        """
        with self._lock:
            self._stream_listeners.setdefault(symbol, set()).add(callback)
            snapshot = self._snapshots.get(symbol)
            if snapshot is not None:
                snapshot = dict(snapshot)

        def _remove() -> None:
            with self._lock:
                listeners = self._stream_listeners.get(symbol)
                if listeners is not None:
                    listeners.discard(callback)
                    if not listeners:
                        self._stream_listeners.pop(symbol, None)

        if snapshot is not None:
            try:
                callback("subscribed", symbol, snapshot)
            except Exception as exc:  # noqa: BLE001 - 监听器故障不伤实例
                logger.warning("tqsdk stream listener failed for %s: %r", symbol, exc)
        return _remove

    def shutdown(self, timeout: float = 10.0) -> None:
        """停机：置停止标志并唤醒 tick，等待线程完成 close 与退出。"""
        self._request_stop()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    # ------------------------------------------------------------------
    # 内部实现（Runtime 线程）
    # ------------------------------------------------------------------

    def _request_stop(self) -> None:
        with self._lock:
            self._stop_requested = True
        self._jobs.put(_StopSignal())

    def _notify_stream(self, symbol: str, kind: str, snapshot: dict[str, Any] | None) -> None:
        """通知单标的的流监听器（Runtime 线程内；回调异常只记录不阻塞 tick）。"""
        with self._lock:
            callbacks = list(self._stream_listeners.get(symbol, ()))
        for callback in callbacks:
            try:
                callback(kind, symbol, snapshot)
            except Exception as exc:  # noqa: BLE001 - 监听器故障不伤实例
                logger.warning("tqsdk stream listener failed for %s: %r", symbol, exc)

    def _notify_all_streams(self, kind: str) -> None:
        """通知全部流监听器（代际重建/上游断连/停机等实例级事件）。"""
        with self._lock:
            items = [
                (symbol, callback)
                for symbol, listeners in self._stream_listeners.items()
                for callback in listeners
            ]
        for symbol, callback in items:
            try:
                callback(kind, symbol, None)
            except Exception as exc:  # noqa: BLE001 - 监听器故障不伤实例
                logger.warning("tqsdk stream listener failed for %s: %r", symbol, exc)

    def _thread_main(self) -> None:
        try:
            self._thread_main_inner()
        except Exception as exc:  # noqa: BLE001 - 驱动线程绝不无声死亡
            with self._lock:
                self._last_error = exc
                self._status = RuntimeStatus.FATAL
            logger.error("tqsdk runtime thread died: %r", exc)
            self._ready.set()  # 唤醒 start() 的等待（否则空等 construct_timeout）
            self._fail_all_jobs(f"tqsdk runtime thread died: {exc!r}")

    def _thread_main_inner(self) -> None:
        from tqsdk import TqApi, TqAuth  # 延迟导入：SDK 未安装时 fatal

        backoff = 0.0
        while not self._stop_requested:
            if backoff:
                time.sleep(backoff)
            self._status = RuntimeStatus.RECYCLING
            _construct_t0 = time.perf_counter()
            # 每代一个显式循环并设为线程 current loop：
            # SDK/close 内部的 asyncio.gather、get_event_loop 都依赖它
            gen_loop = asyncio.SelectorEventLoop()
            asyncio.set_event_loop(gen_loop)
            try:
                # 构造期循环未运行：TqApi 构造函数自行以 wait_update 驱动收初始截面
                api = TqApi(
                    auth=TqAuth(self._auth_user, self._auth_password),
                    disable_print=True,
                    loop=gen_loop,
                )
            except Exception as exc:  # noqa: BLE001 - 构造失败属致命（凭据/网络初始失败）
                gen_loop.close()
                asyncio.set_event_loop(None)
                with self._lock:
                    self._last_error = exc
                    self._status = RuntimeStatus.FATAL
                logger.error("tqsdk runtime construction failed: %r", exc)
                self._ready.set()
                self._fail_all_jobs("tqsdk runtime construction failed")
                return
            with self._lock:
                self._generation += 1
                self._status = RuntimeStatus.READY
                self._last_error = None
            logger.info(
                "tqsdk runtime generation %d ready (construct %.2fs)",
                self._generation,
                time.perf_counter() - _construct_t0,
            )
            self._ready.set()
            # 新一代就绪：清空上一代订阅态，并为仍被引用的标的重建订阅
            self._on_generation_ready()
            next_recycle = self._next_recycle_ts()
            try:
                self._tick_loop(api, next_recycle)
                # 正常停机路径：流监听器以明确事件终结（绝不静默断流）
                self._notify_all_streams("upstream_disconnected")
                self._fail_inflight("tqsdk runtime stopped")
                self._close_api(api)
                asyncio.set_event_loop(None)
                if not gen_loop.is_closed():
                    # 旧代 loop 显式关闭：残留任务/asyncgen 不依赖 GC 兜底
                    # （tqsdk close 只收自己创建的 loop，显式传入的 loop 不关）
                    gen_loop.close()
                return
            except _GenerationReset as exc:
                reset_reason = exc.reason
            except Exception as exc:  # noqa: BLE001 - tick 循环未知异常也按代际重建处理
                reset_reason = f"unexpected tick-loop error: {exc!r}"
                with self._lock:
                    self._last_error = exc
            # 新一代重建：通知流监听器（仍被引用的订阅将自动重订并恢复推送）
            self._notify_all_streams("recycling")
            # 新一代重建：失败在飞 → close（失败不阻断重建）→ 退避后重建
            self._fail_inflight(f"tqsdk runtime recycling: {reset_reason}")
            self._close_api(api)
            if not gen_loop.is_closed():
                gen_loop.close()  # 旧代 loop 显式关闭（新代自建新 loop）
            asyncio.set_event_loop(None)
            backoff = _REBUILD_BACKOFF_SECONDS

    def _close_api(self, api: Any) -> None:
        """关闭 TqApi；失败只记录（last_error），绝不阻断重建/停机路径。"""
        try:
            api.close()
        except Exception as exc:  # noqa: BLE001 - close 失败不阻断重建
            with self._lock:
                self._last_error = exc

    def _tick_loop(self, api: Any, next_recycle: float) -> None:
        """tick 驱动循环：收割完成请求 → 接新请求 → wait_update 驱动一切。

        **peek 门闩复位（关键）**：tqsdk 推送协议要求客户端发出
        ``peek_message`` 后才推送数据，静默 tick 会把 ``_pending_peek``
        门闩留在 True 导致连接变聋——因此每个 tick 主动复位门闩
        （机制定案见 docs/providers/tqsdk.md）；``_fetch_msg`` 残留任务
        由每日 04:00 回收清理。
        """
        wait_exc_streak = 0
        silent_ticks = 0
        while not self._stop_requested:
            self._drain_jobs(api)
            if time.time() >= next_recycle:
                logger.info("tqsdk runtime cross-trading-day recycle triggered (CN 04:00)")
                raise _GenerationReset("cross-trading-day recycle (CN 04:00)")
            if self._dormant_subscription_recycle_due():
                logger.info(
                    "tqsdk runtime dormant-subscription recycle triggered (dormant=%d, limit=%d)",
                    self._dormant_subscription_count(),
                    self._subscription_limit,
                )
                raise _GenerationReset("dormant subscription recycle")
            # peek 门闩复位（见 docstring）
            api._pending_peek = False
            # 自适应 tick：有在飞请求或活跃订阅 50ms（低延迟收数/快照保鲜）；
            # 数据静默期逐步放宽至 2s（限制静默期 _fetch_msg 残留任务累积，
            # 由每日回收清理）
            with self._lock:
                busy = (
                    self._inflight > 0
                    or not self._jobs.empty()
                    or bool(self._live_quotes)
                )
            tick = 0.05 if busy else (0.5 if silent_ticks < 3 else 2.0)
            try:
                got_data = api.wait_update(deadline=time.time() + tick)
                with self._lock:
                    wait_exc_streak = 0
                silent_ticks = 0 if got_data else min(silent_ticks + 1, 4)
                if got_data:
                    # 收到数据才判定 is_changing（_sync_diffs 反映最近一次更新）
                    self._publish_changed_snapshots(api)
            except Exception as exc:  # noqa: BLE001 - 连续异常触发重建，单次只记录
                with self._lock:
                    wait_exc_streak += 1
                    self._last_error = exc
                # 单次 wait_update 异常即通知流客户端（可能自愈；连续异常由
                # watchdog 重建并发 recycling 事件）
                self._notify_all_streams("upstream_disconnected")
                if wait_exc_streak >= _WATCHDOG_STREAK_LIMIT:
                    logger.warning(
                        "tqsdk runtime watchdog: %d consecutive wait_update failures, rebuilding",
                        wait_exc_streak,
                    )
                    raise _GenerationReset("watchdog: consecutive wait_update failures") from exc

    def _drain_jobs(self, api: Any) -> None:
        """非阻塞收割新请求；为每个请求在 tqsdk 循环上创建并发协程。"""
        while True:
            item = self._jobs.get_nowait() if not self._jobs.empty() else None
            if item is None:
                return
            if isinstance(item, _StopSignal):
                return  # 停止由 tick 循环的 while 条件接管，走干净停机路径
            if isinstance(item, _Subscribe):
                self._handle_subscribe(api, item.symbol)
                continue
            kwargs, cf = item
            api.create_task(self._fetch_coro(api, kwargs, cf))

    def _handle_subscribe(self, api: Any, symbol: str) -> None:
        """处理订阅命令（Runtime 线程）：唤醒休眠订阅或发起首次订阅。"""
        with self._lock:
            if symbol in self._live_quotes:
                return  # 重复命令去重
            quote = self._dormant_quotes.pop(symbol, None)
            refs = self._sub_refs.get(symbol, 0)
            wakeup: dict[str, Any] | None = None
            if quote is not None:
                if refs > 0:
                    # 休眠唤醒：复用本代既有 Quote 引用，线程内拷贝发布当前值
                    # （dict 拷贝在 Runtime 线程完成，此时 quote 不会被并发修改）
                    self._live_quotes[symbol] = quote
                    self._snapshots[symbol] = dict(quote)
                    wakeup = dict(self._snapshots[symbol])
                else:
                    # 命令到达前引用已归零：放回休眠，等待回收
                    self._dormant_quotes[symbol] = quote
        if wakeup is not None:
            self._notify_stream(symbol, "subscribed", wakeup)
        if quote is not None:
            return  # 唤醒（或放回休眠）路径结束，不再重复发起订阅
        if refs <= 0:
            return  # 首包未到即已退订（竞态）：丢弃命令
        api.create_task(self._subscribe_coro(api, symbol))

    async def _subscribe_coro(self, api: Any, symbol: str) -> None:
        """订阅协程：``await api.get_quote(symbol)`` 至首包到达。

        官方文档的协程用法（api.py Example2）：循环未在协程内运行时
        get_quote 立即返回 Quote 引用，await 由 tick 循环的 wait_update
        驱动、首包到达才完成——不阻塞 tick。首包到达即在线程内拷贝发布
        首个快照；此后增量由 tick 循环 is_changing 判定刷新。订阅失败
        （无效合约/权限不足等）只记录日志，等待方按首包超时收场。
        """
        try:
            quote = await api.get_quote(symbol)
        except Exception as exc:  # noqa: BLE001 - 单标的订阅失败不伤实例
            logger.warning("tqsdk quote subscribe failed for %s: %r", symbol, exc)
            with self._lock:
                self._last_error = exc
            return
        with self._lock:
            if self._sub_refs.get(symbol, 0) > 0:
                snapshot = dict(quote)
                self._live_quotes[symbol] = quote
                self._snapshots[symbol] = snapshot
            else:
                # 首包到达前引用已归零：转休眠（供后续快速唤醒）
                self._dormant_quotes[symbol] = quote
                snapshot = None
        if snapshot is not None:
            self._notify_stream(symbol, "subscribed", snapshot)

    def _publish_changed_snapshots(self, api: Any) -> None:
        """tick 内刷新发生变化的订阅快照（is_changing 判定，线程内拷贝发布）。"""
        with self._lock:
            live = list(self._live_quotes.items())
        changed: dict[str, dict[str, Any]] = {}
        for symbol, quote in live:
            try:
                if api.is_changing(quote):
                    changed[symbol] = dict(quote)
            except Exception as exc:  # noqa: BLE001 - 单标的判定异常不伤实例
                logger.warning("tqsdk is_changing failed for %s: %r", symbol, exc)
        if changed:
            with self._lock:
                # 只刷新仍处于活跃集合的订阅（发布窗口内退订的标的不复活）
                for symbol, snapshot in changed.items():
                    if symbol in self._live_quotes:
                        self._snapshots[symbol] = snapshot
            # 流监听器发布（与快照缓存同源的拷贝；回调不持锁执行）
            for symbol, snapshot in changed.items():
                self._notify_stream(symbol, "quote", snapshot)

    def _on_generation_ready(self) -> None:
        """新一代就绪：清空上一代订阅态，为仍被引用的标的重建订阅。"""
        with self._lock:
            active = [symbol for symbol, refs in self._sub_refs.items() if refs > 0]
            self._live_quotes.clear()
            self._dormant_quotes.clear()
            self._snapshots.clear()
        for symbol in active:
            self._jobs.put(_Subscribe(symbol))

    def _dormant_subscription_count(self) -> int:
        with self._lock:
            return len(self._dormant_quotes)

    def _dormant_subscription_recycle_due(self) -> bool:
        """休眠订阅累计达到回收阈值：重建新一代清理（活跃订阅自动重订）。"""
        if self._subscription_limit <= 0:
            return False
        with self._lock:
            return len(self._dormant_quotes) >= self._subscription_limit

    async def _fetch_coro(
        self,
        api: Any,
        kwargs: dict[str, Any],
        cf: "concurrent.futures.Future[list[dict[str, Any]]]",
    ) -> None:
        """取数协程：在 tqsdk 私有循环上运行，结果回填 concurrent Future。

        计数回收在 ``finally``（协程内同步执行，无论成功/异常/取消都回收，
        防止并发闸门泄漏）：闸门因此覆盖「在飞取数」全程而非仅入队窗口。
        ``set_result`` 的调用方唤醒经 ``wrap_future``/call_soon 调度，必然
        晚于 finally 释放——不存在「闸门未释放窗口内误拒下一请求」的问题
        （原针对的是 task done callback 方案，回调在循环下一拍才触发）。
        """
        try:
            if cf.cancelled():
                return  # 调用方已取消（wrap_future 反向传播）：不再消耗 SDK 协程
            try:
                frame = await api.get_kline_serial(**kwargs)
                cf.set_result(list(frame.to_dict("records")))
            except Exception as exc:  # noqa: BLE001 - 单请求失败不伤实例
                if not cf.done():
                    cf.set_exception(exc)
        finally:
            with self._lock:
                self._inflight = max(0, self._inflight - 1)
                self._pending.discard(cf)

    def _fail_inflight(self, reason: str) -> None:
        """新一代重建/停机前，明确失败全部在飞与排队请求（调用方可快速重试）。"""
        with self._lock:
            self._inflight = 0
            pending = list(self._pending)
            self._pending.clear()
        for cf in pending:
            if not cf.done():
                cf.set_exception(UpstreamError(reason))
        # 队列中未派发的请求也一并失败（重建窗口内不再派发旧代请求）；
        # 订阅命令直接丢弃——新一代就绪时会按引用计数重发（见 _on_generation_ready）
        while True:
            try:
                item = self._jobs.get_nowait()
            except queue.Empty:
                return
            if isinstance(item, _StopSignal):
                self._jobs.put(item)  # 停止哨兵放回，交给 tick 循环处理
                return
            if isinstance(item, _Subscribe):
                continue
            kwargs, cf = item
            del kwargs
            if not cf.done():
                cf.set_exception(UpstreamError(reason))

    def _fail_all_jobs(self, reason: str) -> None:
        """致命路径：失败队列中全部未派发请求；订阅命令一并丢弃。"""
        while True:
            try:
                item = self._jobs.get_nowait()
            except queue.Empty:
                return
            if isinstance(item, (_StopSignal, _Subscribe)):
                continue
            kwargs, cf = item
            del kwargs
            if not cf.done():
                cf.set_exception(UpstreamError(reason))

    def _next_recycle_ts(self) -> float:
        """下一次跨交易日回收时刻：北京时间每日 04:00（全市场闭市且跨日）。"""
        now_cn = datetime.now(CN_TIMEZONE)
        target = now_cn.replace(hour=4, minute=0, second=0, microsecond=0)
        if target <= now_cn:
            target += timedelta(days=1)
        return target.timestamp()
