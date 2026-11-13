"""实时快照查询服务（与历史 K 线查询服务并列，互不侵入）。

流程：校验后的统一请求 → ProviderRouter.select_quote 选择数据源并具体化
变体 → 适配器（内部 operation 路由 + 订阅/首包等待 + 标准化）→ 标准 ``Quote``。

请求未传递 ``variant`` 时由路由结果具体化为 Provider 声明的默认变体，
最终查询随 ``QuoteQueryResult.query`` 返回（响应 meta 回显实际使用的变体）。
快照无分页语义，本层不做切片；任何未识别的适配器异常都转换为统一
``UpstreamError``，不向客户端泄露 Token、堆栈或内部地址。
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tapehub.application.provider_router import ProviderRouter
from tapehub.domain.errors import TapehubError, UpstreamError, UpstreamTimeoutError
from tapehub.domain.models import Quote, QuoteQuery

if TYPE_CHECKING:
    from tapehub.providers.registry import ProviderRecord

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QuoteQueryResult:
    """查询结果：标准化快照与实际执行的查询（variant 已具体化）。"""

    quote: Quote
    query: QuoteQuery


class QuoteQueryService:
    """实时快照查询应用服务。"""

    def __init__(self, router: ProviderRouter, upstream_timeout_seconds: float = 0.0) -> None:
        """``upstream_timeout_seconds``：上游调用整体超时（秒）；0 = 不限时。

        与 BarQueryService 同语义：超时经 ``asyncio.wait_for`` 实现，
        ``TimeoutError`` 转为 ``UPSTREAM_TIMEOUT``（HTTP 504）；线程型适配器
        需配合 ``call_in_thread(abandon_on_cancel=True)`` 才能即时取消。
        """
        self._router = router
        self._upstream_timeout_seconds = upstream_timeout_seconds

    async def query(self, query: QuoteQuery) -> QuoteQueryResult:
        selection = self._router.select_quote(query)
        record: ProviderRecord = selection.record
        adapter = record.adapter
        if adapter is None:  # 理论上不可达：路由已确保 implemented
            raise UpstreamError(details={"provider": record.name})
        # 适配器内部路由与响应 meta 都需要具体变体：缺省请求按路由解析结果具体化
        final_query = (
            query
            if query.variant == selection.variant
            else query.model_copy(update={"variant": selection.variant})
        )
        try:
            quote = await self._fetch_with_timeout(adapter, final_query)
        except TapehubError:
            raise
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            logger.warning("provider %s timed out: %s", record.name, type(exc).__name__)
            raise UpstreamTimeoutError(details={"provider": record.name}) from exc
        except Exception as exc:
            logger.warning(
                "provider %s failed: %s: %s", record.name, type(exc).__name__, exc
            )
            raise UpstreamError(details={"provider": record.name}) from exc
        return QuoteQueryResult(quote=quote, query=final_query)

    async def _fetch_with_timeout(self, adapter: object, query: QuoteQuery) -> Quote:
        """带整体超时的取数；``upstream_timeout_seconds`` 为 0 时不包裹。"""
        if self._upstream_timeout_seconds <= 0:
            return await adapter.fetch_quote(query)  # type: ignore[attr-defined]
        return await asyncio.wait_for(
            adapter.fetch_quote(query),  # type: ignore[attr-defined]
            self._upstream_timeout_seconds,
        )
