"""Domain Port：Provider 适配器实现的接口。

接口保持最小化，只覆盖第一版历史 K 线链路；
未来实时订阅应新增独立的 StreamingProvider，不扩展现有接口。
"""

from typing import Protocol

from tapehub.domain.models import BarBatch, BarQuery


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
