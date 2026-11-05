"""TqSdkRuntime：进程内唯一的 TqApi 长连接运行时（``conn_mode=shared``）。

线程模型（tqsdk 官方并发模式，2026-11-04 经真实连接实验验证）：

- Runtime 独占一个线程，按 tick 驱动 tqsdk 私有事件循环：
  收割已完成请求 → 非阻塞接收新请求（``api.create_task`` 并发协程）
  → ``api.wait_update(deadline=tick)``（维持 peek_message 推送与连接保活，
  并推进全部并发协程）；
- 取数协程结果经 ``concurrent.futures.Future`` 回填，适配器在 FastAPI
  事件循环中 ``await wrap_future(f)``，互不阻塞对方线程；
- SDK 延迟导入仅发生在 Runtime 线程内；不依赖 FastAPI，后台进程可复用。

生命周期：

- ``start()`` 懒启动：创建线程，线程内构造 TqApi（构造期 ``wait_update``
  自行驱动收初始截面，实测 ~1s；此时循环未运行，构造函数的守卫不触发）；
- 新一代重建：失败在飞请求 → ``api.close()``（tick 返回后循环未运行，
  close 才被允许）→ 同线程构造新一代。触发：北京时间 04:00 跨交易日窗口
  （采纳官方「跨交易日重启」建议）、watchdog 连续异常、休眠订阅累计到
  ``subscription_limit``（tqsdk 协议不支持取消订阅，退订只停止本地快照
  维护，上游推送仍在消耗带宽——累计到阈值即重建一代清理，仍被引用的
  订阅在新一代自动重订）；
- ``shutdown()``：置停止标志并唤醒 tick → 线程内 close → join。

实时快照订阅（M2）：

- ``subscribe(symbol)`` 引用计数 +1；首个订阅者经订阅协程
  ``await api.get_quote(symbol)`` 订阅并等到首包（官方文档的协程用法，
  由 tick 循环的 wait_update 驱动，不阻塞 tick）；
- 首包到达即在 Runtime 线程内 ``dict(quote)`` 拷贝发布首个快照；此后
  tick 循环以 ``api.is_changing`` 判定增量并刷新 ``{symbol: dict}``
  快照缓存——快照只在线程内拷贝后发布，外部拿到的是纯 dict；
- ``latest(symbol)`` 读取最新快照（未收到首包/已退订返回 None）；
- ``unsubscribe(symbol)`` 引用计数 -1；最后一个订阅者退订时停止本地
  维护并丢弃快照，Quote 引用转入「休眠」（上游订阅仍在，等待代际回收）。

健壮性：并发闸门（在飞 ≤ 上限，超限立即 ``ConcurrencyLimitError``）；
watchdog（连续 wait_update 异常触发新一代重建）；构造失败为致命
（凭据类错误重建无意义），Runtime 进入 fatal 并拒绝后续请求。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import queue
import threading
import time
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
                # 正常停机路径
                self._fail_inflight("tqsdk runtime stopped")
                self._close_api(api)
                asyncio.set_event_loop(None)
                return
            except _GenerationReset as exc:
                reset_reason = exc.reason
            except Exception as exc:  # noqa: BLE001 - tick 循环未知异常也按代际重建处理
                reset_reason = f"unexpected tick-loop error: {exc!r}"
                with self._lock:
                    self._last_error = exc
            # 新一代重建：失败在飞 → close（失败不阻断重建）→ 退避后重建
            self._fail_inflight(f"tqsdk runtime recycling: {reset_reason}")
            self._close_api(api)
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

        **peek 门闩复位（关键）**：tqsdk 的推送协议要求客户端发出
        ``peek_message`` 后才推送数据；``wait_update`` 仅在**收到数据**时
        复位 ``_pending_peek`` 门闩（api.py _fetch_msg）。deadline 到期且
        无数据的静默 tick 会把门闩留在 True——后续 tick 不再发 peek，
        服务器停止推送，整个连接变聋（实测：夜盘数据断流、新订阅挂起）。
        因此每个 tick 主动复位门闩，等价于活跃策略主循环的持续
        wait_update 行为；``_fetch_msg`` 残留任务由每日 04:00 回收清理。
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
            if quote is not None:
                if refs > 0:
                    # 休眠唤醒：复用本代既有 Quote 引用，线程内拷贝发布当前值
                    # （dict 拷贝在 Runtime 线程完成，此时 quote 不会被并发修改）
                    self._live_quotes[symbol] = quote
                    self._snapshots[symbol] = dict(quote)
                else:
                    # 命令到达前引用已归零：放回休眠，等待回收
                    self._dormant_quotes[symbol] = quote
                return
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
                self._live_quotes[symbol] = quote
                self._snapshots[symbol] = dict(quote)
            else:
                # 首包到达前引用已归零：转休眠（供后续快速唤醒）
                self._dormant_quotes[symbol] = quote

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

        计数回收必须在 ``set_result`` **之前**（同协程内同步完成）：若依赖
        task done callback（循环下一拍才触发），调用方可能在闸门计数未释放
        的窗口内提交下一请求，造成偶发误拒（实测并发闸门 flake 的根因）。
        """
        try:
            with self._lock:
                self._inflight = max(0, self._inflight - 1)
                self._pending.discard(cf)
            frame = await api.get_kline_serial(**kwargs)
            cf.set_result(list(frame.to_dict("records")))
        except Exception as exc:  # noqa: BLE001 - 单请求失败不伤实例
            if not cf.done():
                cf.set_exception(exc)

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
