"""Twelve Data WebSocket 共享运行时（M2 快照缓存 + M3 流式同源）。

asyncio 原生实现（与 tqsdk 的线程模型不同）：``single`` 部署模式下
Runtime 连接任务运行在主事件循环上，所有状态仅事件循环内访问，无锁、
无线程桥。WS 协议行为定案见 docs/providers/twelvedata.md（唯一出处，
本文不复述）。探针运行记录见 probes/results/PROBE_LOG.md（本地文件，
不入库）。

要点：credit = 当前活跃订阅 symbol 数——退订即时释放、重订免费，上游支持
逐 symbol 真退订，无休眠订阅累积、无代际回收（不产生 ``recycling`` 事件）。

事件通知（listener 回调 ``callback(kind, symbol, snapshot)``）：

- ``subscribed``：symbol 处于等首包态时收到首条 price（订阅确认语义，
  与 tqsdk「首包到达」口径一致；重连重订后同样触发）；
- ``quote``：其后的每条 price 事件；
- ``upstream_disconnected``：连接断开/静默超时（对全部 symbol 广播）、
  上游拒订（``subscribe-status.fails``，仅该 symbol）、Runtime 停机——
  绝不静默断流。

引用计数与退订：``subscribe``/``unsubscribe`` 按 symbol 计数；引用归零
不立即退订，延迟 ``_SNAPSHOT_LINGER_SECONDS``（默认 60s）后向上游发
unsubscribe 释放 credit 并清缓存——M2 轮询与快速重连的流在窗口内复活
免重订；linger 窗口内快照仍可读（新鲜度由 ``received_at`` 表达）。
"""

import asyncio
import json
import logging
import weakref
from collections.abc import Callable
from typing import Any

from tapehub.domain.errors import ProviderNotImplementedError
from tapehub.providers.twelvedata.request_mapper import WS_ENDPOINT

logger = logging.getLogger(__name__)

# 流监听器回调：`(kind, symbol, snapshot)`；snapshot 为已拷贝的纯 dict
# （subscribed/quote）或 None（upstream_disconnected）
StreamListener = Callable[[str, str, "dict[str, Any] | None"], None]

# 上游建议 ~10s 发一次应用层心跳（有应答，应答也计入静默看门狗口径）
_HEARTBEAT_SECONDS = 10.0
# 断线重连退避（秒）
_RECONNECT_BACKOFF_SECONDS = 2.0
# 引用归零后的真退订延迟（秒）：M2 轮询 / 流快速重连免重订
_SNAPSHOT_LINGER_SECONDS = 60.0
# 静默看门狗（秒）：该窗口内无任何报文（含心跳应答）即判定连接僵死重连
_SILENCE_WATCHDOG_SECONDS = 45.0
# WS 建连超时（秒）
_OPEN_TIMEOUT_SECONDS = 20.0

# 进程内存活 Runtime 检测（tripwire，弱引用不阻止回收）：上游免费版限 1 连接
# 且超额关旧，同进程第二个存活实例会互踢连接（重连死循环）——单例由部署
# 构造保证（single 模式 → 一 app → 一 registry → 一 adapter → 一 Runtime），
# 此处只做启动期告警，不强制、不共享状态
_LIVE_RUNTIMES: "weakref.WeakSet[TwelvedataWsRuntime]" = weakref.WeakSet()


def _scrub(text: str, api_key: str) -> str:
    """日志脱敏：异常文本中可能含连接 URL（内嵌 API key）。"""
    return text.replace(api_key, "***") if api_key else text


class TwelvedataWsRuntime:
    """进程内唯一 WS 连接 Runtime（懒启动，跑在调用方事件循环上）。"""

    def __init__(self, *, api_key: str, subscription_limit: int) -> None:
        self._api_key = api_key
        self._subscription_limit = subscription_limit
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        # 仅事件循环内访问（asyncio 原生，无锁）
        self._ws: Any = None
        self._refs: dict[str, int] = {}
        self._pending: set[str] = set()  # 已发订阅、首包未到（等首包态）
        self._snapshots: dict[str, dict[str, Any]] = {}  # symbol → 最新 price 事件拷贝
        self._waiters: dict[str, list[asyncio.Future[dict[str, Any]]]] = {}
        self._listeners: dict[str, set[StreamListener]] = {}
        self._linger_tasks: dict[str, asyncio.Task[None]] = {}
        # 短生命周期内部任务（发送/心跳）持有引用：CPython 对挂起中任务仅
        # 弱引用，裸 create_task 会被 GC 概率性销毁（Task was destroyed 噪音）
        self._tasks: set[asyncio.Task[None]] = set()
        self._fatal_error: BaseException | None = None  # 致命错误（如库缺失）

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """懒启动连接任务（幂等）；不阻塞等待建连——M2 的首包超时由
        调用方（适配器）控制，建连失败按固定退避无限重试。

        ``websockets`` 未安装在此急切失败（PROVIDER_NOT_IMPLEMENTED），
        调用方（适配器）无需等首包超时。检测到同进程另有存活 Runtime 时
        告警（上游 1 连接限制下多实例互踢，见 ``_LIVE_RUNTIMES`` 注释）。
        """
        self._import_websockets()
        others = [
            rt
            for rt in _LIVE_RUNTIMES
            if rt is not self and rt._task is not None and not rt._task.done()
        ]
        if others:
            logger.warning(
                "twelvedata ws runtime: %d other live runtime(s) in this process; "
                "upstream free plan allows a single WS connection — concurrent "
                "runtimes will keep closing each other's connections",
                len(others),
            )
        _LIVE_RUNTIMES.add(self)
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._run(), name="twelvedata-ws-runtime")

    async def shutdown(self, timeout: float = 10.0) -> None:
        """停机：取消连接任务、通知全部监听器 ``upstream_disconnected``。

        防御：连接/linger 任务可能绑定在已关闭的事件循环上（如测试环境
        每请求一个 loop）——``cancel`` 抛 ``RuntimeError`` 时跳过等待，
        不让停机路径失败。
        """
        self._stopping = True
        for linger in list(self._linger_tasks.values()):
            try:
                linger.cancel()
            except RuntimeError:
                pass
        self._linger_tasks.clear()
        task = self._task
        if task is not None and not task.done():
            try:
                task.cancel()
                await asyncio.wait_for(task, timeout)
            except (asyncio.TimeoutError, TimeoutError, asyncio.CancelledError, RuntimeError):
                pass
        self._task = None
        self._notify_all("upstream_disconnected")

    # ------------------------------------------------------------------
    # 订阅 / 引用计数（仅事件循环内调用）
    # ------------------------------------------------------------------

    def subscribe(self, symbol: str) -> None:
        """引用 +1；首次引用且无缓存快照时向上游发订阅（幂等，重复
        订阅同 symbol 上游不重复计费）。"""
        task = self._linger_tasks.pop(symbol, None)
        if task is not None:
            task.cancel()
        self._refs[symbol] = self._refs.get(symbol, 0) + 1
        if symbol not in self._snapshots and symbol not in self._pending:
            self._pending.add(symbol)
            self._send({"action": "subscribe", "params": {"symbols": symbol}})
            # 连接未建立时发送被忽略：建连后 _resubscribe_all 统一重订

    def unsubscribe(self, symbol: str) -> None:
        """引用 -1；归零后延迟 ``_SNAPSHOT_LINGER_SECONDS`` 真退订
        （上游退订即时释放 credit 并清缓存）。"""
        refs = max(0, self._refs.get(symbol, 0) - 1)
        if refs > 0:
            self._refs[symbol] = refs
            return
        self._refs.pop(symbol, None)
        if symbol not in self._snapshots and symbol not in self._pending:
            return  # 从未订上（如上游拒订），无需退订
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._release_symbol(symbol, send=False)
            return
        if symbol not in self._linger_tasks:
            self._linger_tasks[symbol] = asyncio.create_task(
                self._linger_unsubscribe(symbol), name=f"twelvedata-ws-linger-{symbol}"
            )

    async def _linger_unsubscribe(self, symbol: str) -> None:
        """引用归零后的延迟退订；窗口内复活（refs>0）则放弃。"""
        await asyncio.sleep(_SNAPSHOT_LINGER_SECONDS)
        self._linger_tasks.pop(symbol, None)
        if self._refs.get(symbol, 0) > 0:
            return  # linger 窗口内复活，订阅保留
        self._release_symbol(symbol, send=True)

    def _release_symbol(self, symbol: str, *, send: bool) -> None:
        """本地清理订阅状态；``send`` 时向上游发退订（释放 credit）。"""
        self._snapshots.pop(symbol, None)
        self._pending.discard(symbol)
        if send:
            self._send({"action": "unsubscribe", "params": {"symbols": symbol}})

    def active_symbols(self) -> list[str]:
        """上游视角的占用口径：有缓存快照（活跃或 linger 中）∪ 等首包。"""
        return sorted(set(self._snapshots) | self._pending)

    def latest(self, symbol: str) -> dict[str, Any] | None:
        """最新 price 事件拷贝（无订阅/已退订返回 None）。"""
        snapshot = self._snapshots.get(symbol)
        return dict(snapshot) if snapshot is not None else None

    async def wait_snapshot(self, symbol: str, timeout: float) -> dict[str, Any]:
        """等待首包快照（已缓存则立即返回）；超时抛 ``TimeoutError``。

        Runtime 已致命失败（如 ``websockets`` 缺失）时立即抛出该错误。
        """
        if self._fatal_error is not None:
            raise self._fatal_error
        snapshot = self._snapshots.get(symbol)
        if snapshot is not None:
            return dict(snapshot)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._waiters.setdefault(symbol, []).append(fut)
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            waiters = self._waiters.get(symbol)
            if waiters and fut in waiters:
                waiters.remove(fut)

    def add_stream_listener(self, symbol: str, callback: StreamListener) -> Callable[[], None]:
        """注册流监听器；快照已存在（订阅已建立）则立即补发 ``subscribed``
        （先查快照再开流不静默，镜像 tqsdk Runtime 语义）。"""
        listeners = self._listeners.setdefault(symbol, set())
        listeners.add(callback)
        snapshot = self._snapshots.get(symbol)
        if snapshot is not None:
            self._safe_call(callback, "subscribed", symbol, dict(snapshot))

        def _remove() -> None:
            listeners.discard(callback)
            if not listeners:
                self._listeners.pop(symbol, None)

        return _remove

    # ------------------------------------------------------------------
    # 连接任务
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        """连接状态机：建连 → 重订活跃订阅 → 收包 + 心跳 → 断开（广播
        upstream_disconnected）→ 退避重连。建连失败同样退避重试（不致命）；
        唯一例外是 ``websockets`` 缺失（致命，唤醒全部等待者后终止）。"""
        while not self._stopping:
            try:
                ws = await self._connect()
            except asyncio.CancelledError:
                raise
            except ProviderNotImplementedError as exc:
                self._fatal_error = exc
                self._fail_waiters(exc)
                self._notify_all("upstream_disconnected")
                return
            except Exception as exc:  # noqa: BLE001 - 任何建连失败都退避重试
                logger.warning(
                    "twelvedata ws connect failed: %s: %s",
                    type(exc).__name__,
                    _scrub(str(exc), self._api_key),
                )
                self._ws = None
                await asyncio.sleep(_RECONNECT_BACKOFF_SECONDS)
                continue
            self._ws = ws
            heartbeat = asyncio.create_task(self._heartbeat_loop(ws))
            self._tasks.add(heartbeat)
            heartbeat.add_done_callback(self._tasks.discard)
            try:
                await self._resubscribe_all()
                await self._recv_loop(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 收包异常=断连，走重连
                logger.warning(
                    "twelvedata ws connection error: %s: %s",
                    type(exc).__name__,
                    _scrub(str(exc), self._api_key),
                )
            finally:
                heartbeat.cancel()
                self._ws = None
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001 - 关闭失败只记录
                    logger.debug("twelvedata ws close failed", exc_info=True)
            if self._stopping:
                break
            self._notify_all("upstream_disconnected")
            await asyncio.sleep(_RECONNECT_BACKOFF_SECONDS)

    @staticmethod
    def _import_websockets() -> Any:
        """延迟导入 ``websockets``（未安装 → PROVIDER_NOT_IMPLEMENTED）。"""
        try:
            import websockets
        except ImportError as exc:
            raise ProviderNotImplementedError(
                "WebSocket library 'websockets' is not installed.",
                details={"provider": "twelvedata", "sdk": "websockets"},
            ) from exc
        return websockets

    async def _connect(self) -> Any:
        websockets = self._import_websockets()
        uri = f"{WS_ENDPOINT}?apikey={self._api_key}"
        return await websockets.connect(uri, open_timeout=_OPEN_TIMEOUT_SECONDS)

    async def _resubscribe_all(self) -> None:
        """建连后对全部活跃引用重发订阅（首连接=冷启动集合为空），
        并置等首包态——首包到达即广播 ``subscribed``（重订恢复语义）。"""
        self._pending = {symbol for symbol, refs in self._refs.items() if refs > 0}
        for symbol in sorted(self._pending):
            await self._send_await({"action": "subscribe", "params": {"symbols": symbol}})

    async def _heartbeat_loop(self, ws: Any) -> None:
        while not self._stopping:
            await asyncio.sleep(_HEARTBEAT_SECONDS)
            try:
                await ws.send(json.dumps({"action": "heartbeat"}))
            except Exception:  # noqa: BLE001 - 发送失败：静默看门狗兜底重连
                return

    async def _recv_loop(self, ws: Any) -> None:
        """收包循环；静默看门狗超时退出（触发重连）。"""
        while not self._stopping:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=_SILENCE_WATCHDOG_SECONDS)
            except (asyncio.TimeoutError, TimeoutError):
                logger.warning(
                    "twelvedata ws silent for %.0fs, reconnecting", _SILENCE_WATCHDOG_SECONDS
                )
                return
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                logger.debug("twelvedata ws non-JSON message dropped")
                continue
            if isinstance(message, dict):
                self._handle_message(message)

    def _handle_message(self, message: dict[str, Any]) -> None:
        event = message.get("event")
        if event == "price":
            symbol = message.get("symbol")
            if not isinstance(symbol, str) or not symbol:
                return
            snapshot = dict(message)
            self._snapshots[symbol] = snapshot
            self._resolve_waiters(symbol, snapshot)
            if symbol in self._pending:
                self._pending.discard(symbol)
                self._notify(symbol, "subscribed", snapshot)
            else:
                self._notify(symbol, "quote", snapshot)
        elif event == "subscribe-status":
            fails = message.get("fails")
            if isinstance(fails, list):
                for entry in fails:
                    symbol = entry.get("symbol") if isinstance(entry, dict) else None
                    if not isinstance(symbol, str) or not symbol:
                        continue
                    logger.warning("twelvedata ws rejected subscription: %s", symbol)
                    self._pending.discard(symbol)
                    # 上游拒订：以显式断流事件收尾（绝不静默挂死），
                    # 引用由适配器 finally 的 unsubscribe 常规释放
                    self._notify(symbol, "upstream_disconnected", None)
        elif event == "unsubscribe-status":
            fails = message.get("fails")
            if isinstance(fails, list) and fails:
                logger.warning("twelvedata ws unsubscribe reported fails: %r", fails)
        # heartbeat 应答及其它事件：无需处理（静默看门狗口径覆盖活性）

    # ------------------------------------------------------------------
    # 发送 / 通知（仅事件循环内）
    # ------------------------------------------------------------------

    def _send(self, payload: dict[str, Any]) -> None:
        """尽力发送（连接未建立时忽略——建连后 _resubscribe_all 兜底）。"""
        ws = self._ws
        if ws is None:
            return

        async def _do() -> None:
            try:
                await ws.send(json.dumps(payload))
            except Exception:  # noqa: BLE001 - 发送失败由重连重订兜底
                logger.debug("twelvedata ws send failed: %r", payload.get("action"))

        try:
            task = asyncio.get_running_loop().create_task(_do())
        except RuntimeError:
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send_await(self, payload: dict[str, Any]) -> None:
        """建连路径上的顺序发送（失败抛出，由外层走重连）。"""
        ws = self._ws
        if ws is None:
            return
        await ws.send(json.dumps(payload))

    def _resolve_waiters(self, symbol: str, snapshot: dict[str, Any]) -> None:
        waiters = self._waiters.pop(symbol, None)
        if not waiters:
            return
        for fut in waiters:
            if not fut.done():
                fut.set_result(dict(snapshot))

    def _fail_waiters(self, exc: BaseException) -> None:
        """致命失败：唤醒全部首包等待者（抛出该错误而非挂到超时）。"""
        waiters = list(self._waiters.values())
        self._waiters.clear()
        for group in waiters:
            for fut in group:
                if not fut.done():
                    fut.set_exception(exc)

    def _notify(self, symbol: str, kind: str, snapshot: dict[str, Any] | None) -> None:
        for callback in list(self._listeners.get(symbol, ())):
            self._safe_call(callback, kind, symbol, snapshot)

    def _notify_all(self, kind: str) -> None:
        for symbol in list(self._listeners):
            for callback in list(self._listeners[symbol]):
                self._safe_call(callback, kind, symbol, None)

    @staticmethod
    def _safe_call(
        callback: StreamListener, kind: str, symbol: str, snapshot: dict[str, Any] | None
    ) -> None:
        try:
            callback(kind, symbol, snapshot)
        except Exception:  # noqa: BLE001 - 回调异常只记录，绝不传播
            logger.exception("twelvedata ws listener error (%s/%s)", kind, symbol)
