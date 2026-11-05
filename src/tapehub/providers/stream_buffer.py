"""Provider 共享的单客户端流事件缓冲。

供流式适配器（tqsdk / twelvedata）复用：监听回调投递事件、适配器生成器
消费。仅在单个事件循环内读写（回调经 ``call_soon_threadsafe`` 调度或
直接同循环调用），无锁。
"""

import asyncio
from collections import deque
from typing import Any

# 流缓冲中 status 帧的防御性堆积上限（超出才丢最旧；正常运行每代际至多 2 个）
_STATUS_BACKLOG_LIMIT = 64


class StreamBuffer:
    """单客户端有界事件缓冲（仅事件循环线程读写，无锁）。

    coalesce 丢旧保新只约束 ``quote`` 数据帧——行情「最新」比「完整」
    有价值；``status`` 生命周期事件（订阅确认/回收/断连）稀有且关键，
    **不触发丢弃也绝不被丢弃**：缓冲内 quote 数达到上限时，新 quote
    挤掉最旧的 quote。status 仅在极端堆积（超过防御上限）时丢最旧。
    """

    def __init__(self, maxsize: int) -> None:
        self._maxsize = maxsize
        self._items: deque[tuple[str, dict[str, Any] | None]] = deque()
        self._signal = asyncio.Event()

    def put(self, kind: str, snapshot: dict[str, Any] | None) -> None:
        if kind == "quote":
            quotes = sum(1 for item in self._items if item[0] == "quote")
            if quotes >= self._maxsize:
                for index, item in enumerate(self._items):
                    if item[0] == "quote":
                        del self._items[index]  # 挤掉最旧的 quote
                        break
        elif len(self._items) >= self._maxsize + _STATUS_BACKLOG_LIMIT:
            self._items.popleft()  # 防御：status 异常堆积时丢最旧
        self._items.append((kind, snapshot))
        self._signal.set()

    async def get(self) -> tuple[str, dict[str, Any] | None]:
        while not self._items:
            self._signal.clear()
            await self._signal.wait()
        return self._items.popleft()
