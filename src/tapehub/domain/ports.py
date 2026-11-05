"""Domain Port：Provider 适配器实现的接口。

接口保持最小化：历史 K 线（``HistoricalBarProvider``）、实时快照
（``QuoteProvider``）与实时快照流（``QuoteStreamProvider``）各自独立，
互不扩展。
"""

from collections.abc import AsyncIterator
from typing import Protocol

from tapehub.domain.models import BarBatch, BarQuery, Quote, QuoteQuery, QuoteStreamEvent


class HistoricalBarProvider(Protocol):
    """历史 K 线 Provider 端口。

    约定：

    - 输入是标准 ``BarQuery``，输出是标准 ``BarBatch``；
    - 不返回 SDK 原始类型，不依赖 HTTP 请求对象；
    - 适配器在返回前完成统一排序（timestamp 升序）与 offset/limit 切片，
      Application 层不得再次切片；
    - 同步阻塞型 SDK 调用必须隔离到线程（见 providers.base.call_in_thread）。
    """

    async def fetch_bars(self, query: BarQuery) -> BarBatch:
        """查询并返回一页标准化 K 线。"""
        ...


class QuoteProvider(Protocol):
    """实时快照 Provider 端口。

    约定：

    - 输入是标准 ``QuoteQuery``（无周期/时间范围/分页），输出单条 ``Quote``；
    - 快照实现可基于长连接订阅（如共享 Runtime 的最新快照缓存），
      也可基于每次独立请求；订阅建立后首包未在超时内到达时，
      适配器抛 ``QuoteNotReadyError``（QUOTE_NOT_READY）；
    - 不返回 SDK 原始类型，不依赖 HTTP 请求对象。
    """

    async def fetch_quote(self, query: QuoteQuery) -> Quote:
        """查询并返回单标的实时快照。"""
        ...


class QuoteStreamProvider(Protocol):
    """实时快照流 Provider 端口（SSE 流式订阅）。

    约定：

    - 返回 ``QuoteStreamEvent`` 异步迭代器：``quote``（标准快照，与快照
      缓存同源的拷贝）、``status``（subscribed / upstream_disconnected /
      recycling / idle_unsubscribed——断线与回收必须有明确事件，绝不静默
      断流）；心跳与空闲退订由 Application 层的流服务负责；
    - 迭代器被关闭（``aclose``）时适配器必须释放订阅（引用计数 -1）；
    - 跨线程只传拷贝、不阻塞运行时驱动循环；不依赖 HTTP 请求对象。
    """

    def stream_quote(self, query: QuoteQuery) -> AsyncIterator[QuoteStreamEvent]:
        """订阅单标的实时快照流。"""
        ...
