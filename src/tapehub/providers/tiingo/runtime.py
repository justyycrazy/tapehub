"""Tiingo WebSocket 共享运行时（M2 快照缓存 + M3 流式同源）。

asyncio 原生实现，与 twelvedata Runtime 同构；WS 协议行为定案见
docs/providers/tiingo.md（唯一出处，本文不复述）。

事件通知（listener 回调 ``callback(kind, symbol, snapshot)``）：

- ``subscribed``：等首包态收到首条匹配 data 元素（重连重订后同样触发）；
- ``quote``：其后的每条匹配 data 元素；
- ``upstream_disconnected``：连接断开/静默超时（对全部 symbol 广播）、
  Runtime 停机——绝不静默断流。

引用计数与退订：``subscribe``/``unsubscribe`` 按 symbol 计数；引用归零
不立即退订，延迟 ``_SNAPSHOT_LINGER_SECONDS``（默认 60s）后向上游发
unsubscribe 并清缓存——M2 轮询与快速重连的流在窗口内复活免重订；
linger 窗口内快照仍可读（新鲜度由 ``received_at`` 表达）。
"""

import asyncio
import json
import logging
import time
import weakref
from collections.abc import Callable
from typing import Any

from tapehub.domain.errors import ProviderNotImplementedError
from tapehub.providers.tiingo.request_mapper import ws_route
from tapehub.providers.tiingo.response_mapper import ws_element_to_snapshot

logger = logging.getLogger(__name__)

# 流监听器回调：`(kind, symbol, snapshot)`；snapshot 为已拷贝的纯 dict
# （subscribed/quote）或 None（upstream_disconnected）
StreamListener = Callable[[str, str, "dict[str, Any] | None"], None]

# 断线重连退避（秒）
_RECONNECT_BACKOFF_SECONDS = 2.0
# 引用归零后的真退订延迟（秒）：M2 轮询 / 流快速重连免重订
_SNAPSHOT_LINGER_SECONDS = 60.0
# 静默看门狗（秒）：服务端心跳实测 ~60-90s（客户端不发送应用层心跳），该窗口内无
# 任何报文即判定连接僵死重连（约两倍心跳周期留余量）
_SILENCE_WATCHDOG_SECONDS = 180.0
# WS 建连超时（秒）
_OPEN_TIMEOUT_SECONDS = 20.0

# 进程内存活 Runtime 检测（tripwire，弱引用不阻止回收）：同端点第二个存活
# 实例会互踢连接（重连死循环）——单例由部署构造保证（single 模式 → 一 app
# → 一 registry → 一 adapter → 每端点一 Runtime），只做启动期告警
_LIVE_RUNTIMES: "weakref.WeakSet[TiingoWsRuntime]" = weakref.WeakSet()


def _scrub(text: str, api_key: str) -> str:
    """日志脱敏：异常文本与订阅帧中含 API token。"""
    return text.replace(api_key, "***") if api_key else text


class TiingoWsRuntime:
    """单端点 WS 连接 Runtime（懒启动，跑在调用方事件循环上）。

    crypto 与 forex 端点各自实例化（协议同构、连接隔离）。
    """

    def __init__(
        self,
        *,
        api_key: str,
        asset_type: str,
        subscription_limit: int,
    ) -> None:
        endpoint, threshold_level = ws_route(asset_type)
        self._api_key = api_key
        self._asset_type = asset_type
        self._endpoint = endpoint
        self._threshold_level = threshold_level
        self._subscription_limit = subscription_limit
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        # 仅事件循环内访问（asyncio 原生，无锁）
        self._ws: Any = None
        self._refs: dict[str, int] = {}
        self._pending: set[str] = set()  # 已发订阅、首包未到（等首包态）
        self._snapshots: dict[str, dict[str, Any]] = {}  # symbol → 最新 data 元素拷贝
        self._waiters: dict[str, list[asyncio.Future[dict[str, Any]]]] = {}
        self._listeners: dict[str, set[StreamListener]] = {}
        self._linger_tasks: dict[str, asyncio.Task[None]] = {}
        # 短生命周期内部任务（发送）持有引用：CPython 对挂起中任务仅弱引用，
        # 裸 create_task 会被 GC 概率性销毁（Task was destroyed 噪音）
        self._tasks: set[asyncio.Task[None]] = set()
        self._fatal_error: BaseException | None = None  # 致命错误（如库缺失）

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """懒启动连接任务（幂等）；不阻塞等待建连——M2 的首包超时由
        调用方（适配器）控制，建连失败按固定退避无限重试。

        ``websockets`` 未安装在此急切失败（PROVIDER_NOT_IMPLEMENTED），
        调用方（适配器）无需等首包超时。检测到同端点另有存活 Runtime 时
        告警（多实例互踢连接，见 ``_LIVE_RUNTIMES`` 注释）。
        """
        self._import_websockets()
        others = [
            rt
            for rt in _LIVE_RUNTIMES
            if rt is not self
            and rt._endpoint == self._endpoint
            and rt._task is not None
            and not rt._task.done()
        ]
        if others:
            logger.warning(
                "tiingo ws runtime (%s): %d other live runtime(s) on the same "
                "endpoint in this process — concurrent runtimes will keep "
                "closing each other's connections",
                self._asset_type,
                len(others),
            )
        _LIVE_RUNTIMES.add(self)
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(
                self._run(), name=f"tiingo-ws-runtime-{self._asset_type}"
            )

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
        """引用 +1；首次引用且无缓存快照时向上游发订阅。"""
        task = self._linger_tasks.pop(symbol, None)
        if task is not None:
            task.cancel()
        self._refs[symbol] = self._refs.get(symbol, 0) + 1
        if symbol not in self._snapshots and symbol not in self._pending:
            self._pending.add(symbol)
            self._send(self._subscribe_frame(symbol))
            # 连接未建立时发送被忽略：建连后 _resubscribe_all 统一重订

    def unsubscribe(self, symbol: str) -> None:
        """引用 -1；归零后延迟 ``_SNAPSHOT_LINGER_SECONDS`` 真退订。

        若上游忽略 unsubscribe，linger 到期后仍完成本地释放（缓存清空、
        活跃口径回落），带宽残留由 docs/operations.md 限额说明与上游侧兜底。
        """
        refs = max(0, self._refs.get(symbol, 0) - 1)
        if refs > 0:
            self._refs[symbol] = refs
            return
        self._refs.pop(symbol, None)
        if symbol not in self._snapshots and symbol not in self._pending:
            return  # 从未订上（如上游拒收订阅帧），无需退订
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._release_symbol(symbol, send=False)
            return
        if symbol not in self._linger_tasks:
            self._linger_tasks[symbol] = asyncio.create_task(
                self._linger_unsubscribe(symbol), name=f"tiingo-ws-linger-{symbol}"
            )

    async def _linger_unsubscribe(self, symbol: str) -> None:
        """引用归零后的延迟退订；窗口内复活（refs>0）则放弃。"""
        await asyncio.sleep(_SNAPSHOT_LINGER_SECONDS)
        self._linger_tasks.pop(symbol, None)
        if self._refs.get(symbol, 0) > 0:
            return  # linger 窗口内复活，订阅保留
        self._release_symbol(symbol, send=True)

    def _release_symbol(self, symbol: str, *, send: bool) -> None:
        """本地清理订阅状态；``send`` 时向上游发退订。"""
        self._snapshots.pop(symbol, None)
        self._pending.discard(symbol)
        if send:
            self._send(self._unsubscribe_frame(symbol))

    def active_symbols(self) -> list[str]:
        """上游视角的占用口径：有缓存快照（活跃或 linger 中）∪ 等首包。"""
        return sorted(set(self._snapshots) | self._pending)

    def latest(self, symbol: str) -> dict[str, Any] | None:
        """最新快照数据元素拷贝（无订阅/已退订返回 None）。"""
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
        （先查快照再开流不静默）。"""
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
    # 协议帧（token 随帧传递，绝不写日志）
    # ------------------------------------------------------------------

    def _subscribe_frame(self, symbol: str) -> dict[str, Any]:
        return {
            "eventName": "subscribe",
            "authorization": self._api_key,
            "eventData": {
                "thresholdLevel": self._threshold_level,
                "tickers": [symbol],
            },
        }

    def _unsubscribe_frame(self, symbol: str) -> dict[str, Any]:
        return {
            "eventName": "unsubscribe",
            "authorization": self._api_key,
            "eventData": {"tickers": [symbol]},
        }

    # ------------------------------------------------------------------
    # 连接任务
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        """连接状态机：建连 → 重订活跃订阅 → 收包 → 断开（广播
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
                    "tiingo ws (%s) connect failed: %s: %s",
                    self._asset_type,
                    type(exc).__name__,
                    _scrub(str(exc), self._api_key),
                )
                self._ws = None
                await asyncio.sleep(_RECONNECT_BACKOFF_SECONDS)
                continue
            self._ws = ws
            try:
                await self._resubscribe_all()
                await self._recv_loop(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 收包异常=断连，走重连
                logger.warning(
                    "tiingo ws (%s) connection error: %s: %s",
                    self._asset_type,
                    type(exc).__name__,
                    _scrub(str(exc), self._api_key),
                )
            finally:
                self._ws = None
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001 - 关闭失败只记录
                    logger.debug("tiingo ws (%s) close failed", self._asset_type, exc_info=True)
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
                details={"provider": "tiingo", "sdk": "websockets"},
            ) from exc
        return websockets

    async def _connect(self) -> Any:
        websockets = self._import_websockets()
        # 端点不含凭据（token 随 subscribe 帧传递）
        return await websockets.connect(self._endpoint, open_timeout=_OPEN_TIMEOUT_SECONDS)

    async def _resubscribe_all(self) -> None:
        """建连后对全部活跃引用重发订阅（首连接=冷启动集合为空），
        并置等首包态——首条匹配 data 元素到达即广播 ``subscribed``。"""
        self._pending = {symbol for symbol, refs in self._refs.items() if refs > 0}
        for symbol in sorted(self._pending):
            await self._send_await(self._subscribe_frame(symbol))

    async def _recv_loop(self, ws: Any) -> None:
        """收包循环；静默看门狗超时退出（触发重连）。"""
        while not self._stopping:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=_SILENCE_WATCHDOG_SECONDS)
            except (asyncio.TimeoutError, TimeoutError):
                logger.warning(
                    "tiingo ws (%s) silent for %.0fs, reconnecting",
                    self._asset_type,
                    _SILENCE_WATCHDOG_SECONDS,
                )
                return
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                logger.debug("tiingo ws (%s) non-JSON message dropped", self._asset_type)
                continue
            if isinstance(message, dict):
                self._handle_message(message)

    def _handle_message(self, message: dict[str, Any]) -> None:
        """按 messageType 分发；data 元素按 ``ticker`` 客户端兜底过滤（防御，
        服务端 tickers 过滤为主）。E 帧提取 ``response.message`` 告警（错误后
        服务端会主动断连，由重连状态机接管）。
        """
        message_type = message.get("messageType")
        if message_type == "H":
            return  # 服务端心跳（实测 ~60-90s）：静默看门狗口径覆盖活性
        if message_type == "E":
            response = message.get("response")
            detail = response.get("message") if isinstance(response, dict) else None
            logger.warning(
                "tiingo ws (%s) upstream error frame: %s",
                self._asset_type,
                _scrub(str(detail or message), self._api_key),
            )
            return
        data = message.get("data")
        if not isinstance(data, list) or not data:
            return
        # 真实协议：data 即扁平位置数组；多元素嵌套形态仅为测试便利兼容
        elements = (
            [item for item in data if isinstance(item, list)]
            if isinstance(data[0], list)
            else [data]
        )
        for element in elements:
            snapshot = ws_element_to_snapshot(element)
            ticker = snapshot.get("ticker")
            # 客户端兜底过滤（防御保留）：非本 Runtime 订阅集的报文直接忽略
            # （订阅集=活跃引用）
            if not isinstance(ticker, str) or ticker not in self._refs:
                continue
            # 打到达时刻（墙钟秒）：映射层据此透出 snapshot_age_ms（断连/静默
            # 窗口内命中缓存可判陈旧，绝不冒充新鲜）
            snapshot["_arrival"] = time.time()
            self._snapshots[ticker] = snapshot
            self._resolve_waiters(ticker, snapshot)
            if ticker in self._pending:
                self._pending.discard(ticker)
                self._notify(ticker, "subscribed", snapshot)
            else:
                self._notify(ticker, "quote", snapshot)

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
                logger.debug(
                    "tiingo ws (%s) send failed: %r", self._asset_type, payload.get("eventName")
                )

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
            logger.exception("tiingo ws listener error (%s/%s)", kind, symbol)
