"""测试用 websockets 假模块（Twelve Data WS 协议语义，零网络零凭据）。

模拟 ``websockets.connect`` 与 WS 收发（探针实证的协议行为）：

- ``connect(uri, open_timeout=...)``：返回单例式 ``FakeWebSocket``
  （前 ``connect_failures`` 次抛连接异常，测重连退避）；
- ``send``：subscribe → ``subscribe-status``（``reject_symbols`` 中的
  symbol 进 ``fails``）+ 立即推首条 price（可配 ``first_price_delay``）；
  unsubscribe → ``unsubscribe-status``；heartbeat → 应答 ack；
- ``recv``：从入站队列取下一条报文（JSON 字符串，同真实库）；连接
  被丢弃/关闭后抛 ``ConnectionError``；
- 测试手柄：``push_price(symbol, price)`` 手动推 price、
  ``drop_connection()`` 注入断连、``sent`` 全量出站记录。

模块级旋钮（``install`` 重置）：

- ``FakeWebSocket.connect_failures``：建连连续失败次数；
- ``FakeWebSocket.reject_symbols``：上游拒订集合（进 fails）；
- ``FakeWebSocket.auto_price_seconds``：订阅成功后按该周期自动推 price
  （None = 仅首条 + 手动推送）；
- ``FakeWebSocket.initial_price`` / ``initial_timestamp``：首条 price 值
  （默认取探针实测样本 EUR/USD）。
"""

import asyncio
import json
import sys
import types
from typing import Any

# 探针实测样本（EUR/USD）：timestamp 为分钟桶 Unix 秒
INITIAL_PRICE = 1.12183
INITIAL_TIMESTAMP = 1793887260


def price_event(symbol: str, price: float, timestamp: int | None = None) -> dict[str, Any]:
    """构造 price 事件（EUR/USD 口径：无 exchange，探针实证）。"""
    return {
        "event": "price",
        "symbol": symbol,
        "currency_base": "Euro",
        "currency_quote": "US Dollar",
        "type": "Physical Currency",
        "timestamp": INITIAL_TIMESTAMP if timestamp is None else timestamp,
        "price": price,
    }


class FakeWebSocket:
    instances: list["FakeWebSocket"] = []
    connect_failures = 0
    reject_symbols: set[str] = set()
    auto_price_seconds: float | None = None
    first_price_delay = 0.0
    initial_price = INITIAL_PRICE
    # 按 symbol 定制首条 price 事件（缺省用 price_event 工厂）；测 crypto 字段差异用
    initial_payloads: dict[str, dict[str, Any]] | None = None
    # 送达 N 条报文后注入一次断连（单次触发；需配合 auto_price 保证后续流量）
    drop_after_messages: int | None = None

    def __init__(self, uri: str) -> None:
        self.uri = uri
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self._drop_next = False
        self._inbound: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._subscribed: set[str] = set()
        self._price_seq: dict[str, float] = {}
        self._auto_tasks: list[asyncio.Task[None]] = []

    # ------------------------------------------------------------------
    # 出站（runtime 调用）
    # ------------------------------------------------------------------

    async def send(self, text: str) -> None:
        if self.closed:
            raise ConnectionError("connection closed")
        message = json.loads(text)
        self.sent.append(message)
        action = message.get("action")
        params = message.get("params") or {}
        symbols = [s for s in str(params.get("symbols", "")).split(",") if s]
        if action == "subscribe":
            success, fails = [], []
            for symbol in symbols:
                if symbol in self._reject():
                    fails.append({"symbol": symbol, "exchange": "FOREX"})
                else:
                    self._subscribed.add(symbol)
                    success.append(
                        {"symbol": symbol, "exchange": "FOREX", "type": "PHYSICAL_CURRENCY"}
                    )
            status = {
                "event": "subscribe-status",
                "status": "ok",
                "success": success,
                "fails": fails or None,
            }
            self._inbound.put_nowait(status)
            for entry in success:
                self._schedule_first_price(entry["symbol"])
        elif action == "unsubscribe":
            for symbol in symbols:
                self._subscribed.discard(symbol)
            self._inbound.put_nowait(
                {
                    "event": "unsubscribe-status",
                    "status": "ok",
                    "success": [
                        {"symbol": s, "exchange": "FOREX", "type": "PHYSICAL_CURRENCY"}
                        for s in symbols
                    ],
                    "fails": None,
                }
            )
        elif action == "heartbeat":
            self._inbound.put_nowait({"event": "heartbeat", "status": "ok"})

    def _reject(self) -> set[str]:
        return FakeWebSocket.reject_symbols

    def _schedule_first_price(self, symbol: str) -> None:
        """首条 price：默认立即入队；配置 first_price_delay 时延迟。"""

        def _emit() -> None:
            price = self._price_seq.get(symbol, FakeWebSocket.initial_price)
            self._price_seq[symbol] = price
            payload = (FakeWebSocket.initial_payloads or {}).get(symbol)
            self._inbound.put_nowait(payload or price_event(symbol, price))

        delay = FakeWebSocket.first_price_delay
        if delay > 0:

            async def _later() -> None:
                await asyncio.sleep(delay)
                _emit()

            try:
                asyncio.get_running_loop().create_task(_later())
            except RuntimeError:
                _emit()
        else:
            _emit()
        interval = FakeWebSocket.auto_price_seconds
        if interval is not None:

            async def _auto() -> None:
                while not self.closed and symbol in self._subscribed:
                    await asyncio.sleep(interval)
                    if self.closed or symbol not in self._subscribed:
                        return
                    self.push_price(symbol, self._price_seq.get(symbol, 0.0) + 1.0)

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            self._auto_tasks.append(loop.create_task(_auto()))

    # ------------------------------------------------------------------
    # 入站（runtime 调用）
    # ------------------------------------------------------------------

    async def recv(self) -> str:
        if self._drop_next:
            self._drop_next = False
            raise ConnectionError("injected connection drop")
        message = await self._inbound.get()
        if message is None or self._drop_next:
            self._drop_next = False
            raise ConnectionError("connection closed")
        count = FakeWebSocket.drop_after_messages
        if count is not None and count > 0:
            FakeWebSocket.drop_after_messages = count - 1
            if count - 1 == 0:
                FakeWebSocket.drop_after_messages = None  # 单次注入
                self._drop_next = True
        return json.dumps(message)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._subscribed.clear()
        for task in self._auto_tasks:
            task.cancel()
        self._inbound.put_nowait(None)  # 唤醒阻塞中的 recv

    # ------------------------------------------------------------------
    # 测试手柄
    # ------------------------------------------------------------------

    def push_price(self, symbol: str, price: float, timestamp: int | None = None) -> None:
        """手动推一条 price 事件（自动流之外的定向推送）。"""
        self._price_seq[symbol] = price
        self._inbound.put_nowait(price_event(symbol, price, timestamp))

    def drop_connection(self) -> None:
        """注入断连：唤醒阻塞中的 recv 并让其抛 ConnectionError。"""
        self._drop_next = True
        self._inbound.put_nowait(None)

    def sent_actions(self, symbol: str) -> list[dict[str, Any]]:
        """指定 symbol 的 subscribe/unsubscribe 出站记录。"""
        return [
            m
            for m in self.sent
            if m.get("action") in ("subscribe", "unsubscribe")
            and symbol in str((m.get("params") or {}).get("symbols", "")).split(",")
        ]


def install(monkeypatch: Any) -> types.ModuleType:
    """把假 websockets 模块注入 sys.modules 并重置全部旋钮。"""
    module = types.ModuleType("websockets")

    async def connect(uri: str, **kwargs: Any) -> FakeWebSocket:
        if FakeWebSocket.connect_failures > 0:
            FakeWebSocket.connect_failures -= 1
            raise ConnectionError("connect failed")
        ws = FakeWebSocket(uri)
        FakeWebSocket.instances.append(ws)
        return ws

    module.connect = connect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "websockets", module)
    FakeWebSocket.instances = []
    FakeWebSocket.connect_failures = 0
    FakeWebSocket.reject_symbols = set()
    FakeWebSocket.auto_price_seconds = None
    FakeWebSocket.first_price_delay = 0.0
    FakeWebSocket.initial_price = INITIAL_PRICE
    FakeWebSocket.initial_payloads = None
    FakeWebSocket.drop_after_messages = None
    return module
