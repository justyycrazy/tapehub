"""公共 ProviderRouter：只负责选择 Provider，不包含任何上游 SDK 函数名。

路由完整能力维度：provider × data_type × market × asset_type × interval × adjust。
其中 data_type=bars 已由 /v1/market/bars 路径确定；
market/exchange 从标准 instrument 解析，不需要调用方重复传递。

不做没有依据的自动降级，不伪造 Provider 能力：
``provider=auto`` 时只从“启用 + 已实现 + 完整能力匹配”的候选中选择；
无可用候选时返回 ``NO_PROVIDER_AVAILABLE``。
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from tapehub.domain.errors import (
    NoProviderAvailableError,
    ProviderDisabledError,
    ProviderNotFoundError,
    ProviderNotImplementedError,
    UnsupportedCapabilityError,
)
from tapehub.domain.models import BarQuery, DataType

if TYPE_CHECKING:
    from tapehub.providers.registry import ProviderRecord, ProviderRegistry

AUTO_PROVIDER = "auto"


@dataclass(frozen=True)
class ProviderSelection:
    """路由结果。"""

    record: "ProviderRecord"


def _capability_details(query: BarQuery) -> dict[str, str]:
    return {
        "market": query.instrument.market,
        "asset_type": query.asset_type.value,
        "interval": query.interval.code,
        "adjust": query.adjust.value,
        "variant": query.variant.value,
    }


class ProviderRouter:
    """基于能力目录的公共路由器。"""

    def __init__(self, registry: "ProviderRegistry") -> None:
        self._registry = registry

    def select(self, query: BarQuery) -> "ProviderRecord":
        """按统一请求选择实际执行的 Provider。"""
        if query.provider != AUTO_PROVIDER:
            return self._select_explicit(query)
        return self._select_auto(query)

    def _select_explicit(self, query: BarQuery) -> "ProviderRecord":
        record = self._registry.get(query.provider)
        if record is None:
            raise ProviderNotFoundError(
                f"Provider {query.provider!r} not found.",
                details={"provider": query.provider},
            )
        if not record.enabled:
            raise ProviderDisabledError(
                f"Provider {query.provider!r} is disabled.",
                details={"provider": record.name},
            )
        if not record.implemented or record.adapter is None:
            raise ProviderNotImplementedError(
                f"Provider {query.provider!r} is not implemented.",
                details={"provider": record.name},
            )
        if not record.supports(
            data_type=DataType.BARS,
            market=query.instrument.market,
            asset_type=query.asset_type,
            interval=query.interval,
            adjust=query.adjust,
            variant=query.variant,
        ):
            raise UnsupportedCapabilityError(
                f"Provider {query.provider!r} does not support the requested capability.",
                details={"provider": record.name, **_capability_details(query)},
            )
        return record

    def _select_auto(self, query: BarQuery) -> "ProviderRecord":
        candidates = self._registry.candidates(
            data_type=DataType.BARS,
            market=query.instrument.market,
            asset_type=query.asset_type,
            interval=query.interval,
            adjust=query.adjust,
            variant=query.variant,
        )
        if not candidates:
            raise NoProviderAvailableError(
                "No enabled provider supports the requested capability.",
                details=_capability_details(query),
            )
        # 注册顺序固定，取第一个完整匹配的候选，保证路由确定性。
        return candidates[0]
