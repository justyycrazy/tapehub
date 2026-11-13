"""测试用 websockets 假模块（Tiingo WS 协议语义，零网络零凭据）。

模拟 ``websockets.connect`` 与 WS 收发（官方文档 websockets/crypto|forex
页的协议行为；消息形态细节未经探针实证，本假件按文档字段表造帧）：

- ``connect(endpoint, open_timeout=...)``：返回单例式 ``FakeWebSocket``
  （前 ``connect_failures`` 次抛连接异常，测重连退避）；
- ``send``：``eventName=subscribe`` → 对 ``eventData.tickers`` 中未被
  ``reject_symbols`` 拒绝的 ticker 立即推首条 data 元素（可配
  ``first_price_delay``；被拒 ticker 不推数据——等 first-packet 超时
  兜底，上游拒收帧形态未实证）；``eventName=unsubscribe`` 仅记录出站；
- ``recv``：从入站队列取下一条报文（JSON 字符串，同真实库）；连接
  被丢弃/关闭后抛 ``ConnectionError``；
- 测试手柄：``push_quote(ticker, element)`` 推 data 帧（``data`` 数组
  可含多元素）、``push_heartbeat()`` 推服务端 H 帧、
  ``drop_connection()`` 注入断连、``sent`` 全量出站记录。

模块级旋钮（``install`` 重置）：

- ``FakeWebSocket.connect_failures``：建连连续失败次数；
- ``FakeWebSocket.reject_symbols``：不推送数据的 ticker 集合；
- ``FakeWebSocket.auto_price_seconds``：订阅成功后按该周期自动推 data
  （None = 仅首条 + 手动推送）；
- ``FakeWebSocket.first_price_delay``：首条 data 延迟秒数（0 = 立即）；
- ``FakeWebSocket.initial_elements``：按 ticker 定制首条 data 元素
  （缺省用 top_element 工厂）。
"""

import asyncio
import json
import sys
import types
from typing import Any

# 快照样本（top-of-book 形态）：date 为 ISO UTC（北京 +8）
INITIAL_DATE = "2026-01-05T00:00:00.000Z"


def top_element(
    ticker: str,
    *,
    bid: float = 1.085,
    ask: float = 1.086,
    mid: float | None = None,
    date: str = INITIAL_DATE,
    exchange: str | None = None,
) -> dict[str, Any]:
    """构造 top-of-book data 元素（文档字段表：bidSize/bidPrice/askSize/
    askPrice/midPrice）。"""
    element: dict[str, Any] = {
        "ticker": ticker,
        "date": date,
        "bidSize": 1.0,
        "bidPrice": bid,
        "askSize": 1.0,
        "askPrice": ask,
        # 文档公式 mid=(bid+ask)/2；取整消除浮点求和噪声（真实上游为显式字段）
        "midPrice": round((bid + ask) / 2, 10) if mid is None else mid,
    }
    if exchange is not None:
        element["exchange"] = exchange
    return element


def trade_element(
    ticker: str,
    price: float,
    *,
    size: float = 0.5,
    date: str = INITIAL_DATE,
    exchange: str | None = None,
) -> dict[str, Any]:
    """构造 Trade data 元素（文档字段表：lastSize/lastPrice）。"""
    element: dict[str, Any] = {
        "ticker": ticker,
        "date": date,
        "lastSize": size,
        "lastPrice": price,
    }
    if exchange is not None:
        element["exchange"] = exchange
    return element


class FakeWebSocket:
    instances: list["FakeWebSocket"] = []
    connect_failures = 0
    reject_symbols: set[str] = set()
    auto_price_seconds: float | None = None
    first_price_delay = 0.0
    initial_elements: dict[str, dict[str, Any]] | None = None
    # 送达 N 条报文后注入一次断连（单次触发；需配合 auto_price 保证后续流量）
    drop_after_messages: int | None = None

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
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
        event = message.get("eventName")
        event_data = message.get("eventData") or {}
        tickers = [t for t in event_data.get("tickers") or [] if isinstance(t, str)]
        if event == "subscribe":
            for ticker in tickers:
                if ticker in self._reject():
                    continue  # 拒收：不推数据（首包超时兜底）
                self._subscribed.add(ticker)
                self._schedule_first_element(ticker)
        elif event == "unsubscribe":
            for ticker in tickers:
                self._subscribed.discard(ticker)

    def _reject(self) -> set[str]:
        return FakeWebSocket.reject_symbols

    def _schedule_first_element(self, ticker: str) -> None:
        """首条 data 元素：默认立即入队；配置 first_price_delay 时延迟。"""

        def _emit() -> None:
            payload = (FakeWebSocket.initial_elements or {}).get(ticker)
            self._inbound.put_nowait(
                {"service": "fx_data", "messageType": "A", "data": [payload or top_element(ticker)]}
            )

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
                while not self.closed and ticker in self._subscribed:
                    await asyncio.sleep(interval)
                    if self.closed or ticker not in self._subscribed:
                        return
                    element = self._next_auto_element(ticker)
                    self.push_quote(ticker, element)

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            self._auto_tasks.append(loop.create_task(_auto()))

    def _next_auto_element(self, ticker: str) -> dict[str, Any]:
        """自动流的递增 top 元素（bid/ask 各 +1，便于断言新帧到达）。"""
        price = self._price_seq.get(ticker, 0.0) + 1.0
        self._price_seq[ticker] = price
        return top_element(ticker, bid=price, ask=price + 1.0)

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

    def push_quote(self, ticker: str, *elements: dict[str, Any]) -> None:
        """推一条 data 帧（数组元素任意；可含非订阅 ticker 测客户端过滤）。"""
        if not elements:
            elements = (top_element(ticker),)
        self._inbound.put_nowait(
            {"service": "fx_data", "messageType": "A", "data": list(elements)}
        )

    def push_heartbeat(self) -> None:
        """推一条服务端 H 心跳帧（~30s 周期，静默看门狗活性口径）。"""
        self._inbound.put_nowait({"service": "fx_data", "messageType": "H"})

    def drop_connection(self) -> None:
        """注入断连：唤醒阻塞中的 recv 并让其抛 ConnectionError。"""
        self._drop_next = True
        self._inbound.put_nowait(None)

    def subscribe_frames(self, ticker: str) -> list[dict[str, Any]]:
        """指定 ticker 的 subscribe 出站记录。"""
        return [
            m
            for m in self.sent
            if m.get("eventName") == "subscribe"
            and ticker in (m.get("eventData") or {}).get("tickers", [])
        ]

    def unsubscribe_frames(self, ticker: str) -> list[dict[str, Any]]:
        """指定 ticker 的 unsubscribe 出站记录。"""
        return [
            m
            for m in self.sent
            if m.get("eventName") == "unsubscribe"
            and ticker in (m.get("eventData") or {}).get("tickers", [])
        ]


def install(monkeypatch: Any) -> types.ModuleType:
    """把假 websockets 模块注入 sys.modules 并重置全部旋钮。"""
    module = types.ModuleType("websockets")

    async def connect(endpoint: str, **kwargs: Any) -> FakeWebSocket:
        if FakeWebSocket.connect_failures > 0:
            FakeWebSocket.connect_failures -= 1
            raise ConnectionError("connect failed")
        ws = FakeWebSocket(endpoint)
        FakeWebSocket.instances.append(ws)
        return ws

    module.connect = connect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "websockets", module)
    FakeWebSocket.instances = []
    FakeWebSocket.connect_failures = 0
    FakeWebSocket.reject_symbols = set()
    FakeWebSocket.auto_price_seconds = None
    FakeWebSocket.first_price_delay = 0.0
    FakeWebSocket.initial_elements = None
    FakeWebSocket.drop_after_messages = None
    return module
