"""历史 K 线查询服务。

流程：校验后的统一请求 → ProviderRouter 选择数据源 → 适配器
（内部 operation 路由 + 参数转换 + 上游调用 + 标准化 + 排序分页）
→ 标准化 ``BarBatch``。

适配器已经完成统一分页，本层不得再次切片；
任何未识别的适配器异常都转换为统一 ``UpstreamError``，
不向客户端泄露 Token、堆栈或内部地址。
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from tapehub.application.provider_router import ProviderRouter
from tapehub.domain.errors import TapehubError, UpstreamError, UpstreamTimeoutError
from tapehub.domain.models import BarBatch, BarQuery

if TYPE_CHECKING:
    from tapehub.providers.registry import ProviderRecord

logger = logging.getLogger(__name__)


class BarQueryService:
    """历史 K 线查询应用服务。"""

    def __init__(self, router: ProviderRouter) -> None:
        self._router = router

    async def query(self, query: BarQuery) -> BarBatch:
        record: ProviderRecord = self._router.select(query)
        adapter = record.adapter
        if adapter is None:  # 理论上不可达：路由已确保 implemented
            raise UpstreamError(details={"provider": record.name})
        try:
            return await adapter.fetch_bars(query)
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
