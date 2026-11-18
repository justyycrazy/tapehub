"""历史 K 线查询服务。

流程：校验后的统一请求 → ProviderRouter 选择数据源并具体化变体 → 适配器
（内部 operation 路由 + 参数转换 + 上游调用 + 标准化 + 排序分页）
→ 标准化 ``BarBatch``。

请求未传递 ``variant`` 时由路由结果具体化为 Provider 声明的默认变体，
最终查询随 ``BarQueryResult.query`` 返回（响应 meta 回显实际使用的变体）。
适配器已经完成统一分页，本层不得再次切片；
任何未识别的适配器异常都转换为统一 ``UpstreamError``，
不向客户端泄露 Token、堆栈或内部地址。
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tapehub.application.provider_router import ProviderRouter
from tapehub.application.scrub import scrub
from tapehub.domain.errors import TapehubError, UpstreamError, UpstreamTimeoutError
from tapehub.domain.models import BarBatch, BarQuery

if TYPE_CHECKING:
    from tapehub.providers.registry import ProviderRecord

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BarQueryResult:
    """查询结果：标准化批次与实际执行的查询（variant 已具体化）。"""

    batch: BarBatch
    query: BarQuery


class BarQueryService:
    """历史 K 线查询应用服务。"""

    def __init__(
        self,
        router: ProviderRouter,
        upstream_timeout_seconds: float = 0.0,
        secret_values: tuple[str, ...] = (),
    ) -> None:
        """``upstream_timeout_seconds``：上游调用整体超时（秒）；0 = 不限时。

        超时经 ``asyncio.wait_for`` 实现，``TimeoutError`` 由下方既有映射
        转为 ``UPSTREAM_TIMEOUT``（HTTP 504）。线程型适配器调用需配合
        ``call_in_thread(abandon_on_cancel=True)`` 才能即时取消（否则 shield
        会把超时推迟到线程自然结束，等于失效）。

        ``secret_values``：Provider 凭据值（``Settings.credential_values()``），
        供 catch-all 日志脱敏——SDK 异常文本可能回显含凭据的请求 URL。
        """
        self._router = router
        self._upstream_timeout_seconds = upstream_timeout_seconds
        self._secret_values = secret_values

    async def query(self, query: BarQuery) -> BarQueryResult:
        selection = self._router.select(query)
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
            batch = await self._fetch_with_timeout(adapter, final_query)
        except TapehubError:
            raise
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            logger.warning("provider %s timed out: %s", record.name, type(exc).__name__)
            raise UpstreamTimeoutError(details={"provider": record.name}) from exc
        except Exception as exc:
            logger.warning(
                "provider %s failed: %s: %s",
                record.name,
                type(exc).__name__,
                scrub(str(exc), self._secret_values),
            )
            raise UpstreamError(details={"provider": record.name}) from exc
        return BarQueryResult(batch=batch, query=final_query)

    async def _fetch_with_timeout(self, adapter: object, query: BarQuery) -> BarBatch:
        """带整体超时的取数；``upstream_timeout_seconds`` 为 0 时不包裹。"""
        if self._upstream_timeout_seconds <= 0:
            return await adapter.fetch_bars(query)  # type: ignore[attr-defined]
        return await asyncio.wait_for(
            adapter.fetch_bars(query),  # type: ignore[attr-defined]
            self._upstream_timeout_seconds,
        )
