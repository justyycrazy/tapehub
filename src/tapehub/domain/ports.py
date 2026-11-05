"""Domain Port：Provider 适配器实现的接口。

接口保持最小化：历史 K 线（``HistoricalBarProvider``）与实时快照
（``QuoteProvider``）各自独立；未来 SSE 流式订阅应新增独立的
StreamingProvider，不扩展现有接口。
"""

from typing import Protocol

from tapehub.domain.models import BarBatch, BarQuery, Quote, QuoteQuery


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
