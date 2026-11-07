"""公共 ProviderRouter：只负责选择 Provider，不包含任何上游 SDK 函数名。

路由完整能力维度：provider × data_type × market × asset_type × interval × adjust × variant。
其中 data_type=bars 已由 /v1/market/bars 路径确定；data_type=quotes 由
/v1/market/quotes 路径确定（quote 无 interval/adjust 维度，能力匹配跳过）；
market/exchange 从标准 instrument 解析，不需要调用方重复传递。

``variant`` 未传递（None）表示请求未表达数据源偏好：
该维度不参与能力过滤，由各 Provider 声明的 ``default_variant`` 具体化，
解析结果随 ``ProviderSelection.variant`` 返回并回显到响应 meta。

不做没有依据的自动降级，不伪造 Provider 能力：
显式 ``variant`` 严格匹配；``provider=auto`` 时只从“启用 + 已实现 + 能力可解析”
的候选中选择；无可用候选时返回 ``NO_PROVIDER_AVAILABLE``。
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
from tapehub.domain.models import BarQuery, DataType, DataVariant, QuoteQuery

if TYPE_CHECKING:
    from tapehub.providers.registry import ProviderRecord, ProviderRegistry

AUTO_PROVIDER = "auto"


@dataclass(frozen=True)
class ProviderSelection:
    """路由结果：Provider 记录与具体化的数据源变体。"""

    record: "ProviderRecord"
    variant: DataVariant


def _capability_details(query: BarQuery) -> dict[str, str | None]:
    return {
        "market": query.instrument.market,
        "asset_type": query.asset_type.value,
        "interval": query.interval.code,
        "adjust": query.adjust.value,
        "variant": query.variant.value if query.variant is not None else None,
    }


def _quote_capability_details(query: QuoteQuery) -> dict[str, str | None]:
    return {
        "market": query.instrument.market,
        "asset_type": query.asset_type.value,
        "variant": query.variant.value if query.variant is not None else None,
    }


class ProviderRouter:
    """基于能力目录的公共路由器。"""

    def __init__(self, registry: "ProviderRegistry") -> None:
        self._registry = registry

    def select(self, query: BarQuery) -> "ProviderSelection":
        """按统一请求选择实际执行的 Provider 与具体变体。"""
        if query.provider != AUTO_PROVIDER:
            return self._select_explicit(query)
        return self._select_auto(query)

    def _select_explicit(self, query: BarQuery) -> "ProviderSelection":
        record = self._registry.get(query.provider)
        if record is None:
            raise ProviderNotFoundError(
                f"Provider {query.provider!r} not found.",
                details={"provider": query.provider},
            )
        if not record.enabled:
            raise ProviderDisabledError(
                f"Provider {query.provider!r} is disabled.",
                details={
                    "provider": record.name,
                    **(
                        {"disabled_reason": record.disabled_reason}
                        if record.disabled_reason
                        else {}
                    ),
                },
            )
        if not record.implemented or record.adapter is None:
            raise ProviderNotImplementedError(
                f"Provider {query.provider!r} is not implemented.",
                details={"provider": record.name},
            )
        resolved_variant = record.resolve_variant(
            data_type=DataType.BARS,
            market=query.instrument.market,
            asset_type=query.asset_type,
            interval=query.interval,
            adjust=query.adjust,
            variant=query.variant,
        )
        if resolved_variant is None:
            raise UnsupportedCapabilityError(
                f"Provider {query.provider!r} does not support the requested capability.",
                details={"provider": record.name, **_capability_details(query)},
            )
        return ProviderSelection(record=record, variant=resolved_variant)

    def _select_auto(self, query: BarQuery) -> "ProviderSelection":
        candidates = self._registry.resolve_candidates(
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
        return ProviderSelection(record=candidates[0].record, variant=candidates[0].variant)

    # ------------------------------------------------------------------
    # 实时快照路由（data_type=quotes，无 interval/adjust 维度）
    # ------------------------------------------------------------------

    def select_quote(self, query: QuoteQuery) -> "ProviderSelection":
        """按快照请求选择实际执行的 Provider 与具体变体（与 bars 并列）。"""
        if query.provider != AUTO_PROVIDER:
            return self._select_quote_explicit(query)
        return self._select_quote_auto(query)

    def _select_quote_explicit(self, query: QuoteQuery) -> "ProviderSelection":
        record = self._registry.get(query.provider)
        if record is None:
            raise ProviderNotFoundError(
                f"Provider {query.provider!r} not found.",
                details={"provider": query.provider},
            )
        if not record.enabled:
            raise ProviderDisabledError(
                f"Provider {query.provider!r} is disabled.",
                details={
                    "provider": record.name,
                    **(
                        {"disabled_reason": record.disabled_reason}
                        if record.disabled_reason
                        else {}
                    ),
                },
            )
        if not record.implemented or record.adapter is None:
            raise ProviderNotImplementedError(
                f"Provider {query.provider!r} is not implemented.",
                details={"provider": record.name},
            )
        resolved_variant = record.resolve_variant(
            data_type=DataType.QUOTE,
            market=query.instrument.market,
            asset_type=query.asset_type,
            variant=query.variant,
        )
        if resolved_variant is None:
            raise UnsupportedCapabilityError(
                f"Provider {query.provider!r} does not support the requested capability.",
                details={"provider": record.name, **_quote_capability_details(query)},
            )
        return ProviderSelection(record=record, variant=resolved_variant)

    def _select_quote_auto(self, query: QuoteQuery) -> "ProviderSelection":
        candidates = self._registry.resolve_candidates(
            data_type=DataType.QUOTE,
            market=query.instrument.market,
            asset_type=query.asset_type,
            variant=query.variant,
        )
        if not candidates:
            raise NoProviderAvailableError(
                "No enabled provider supports the requested capability.",
                details=_quote_capability_details(query),
            )
        return ProviderSelection(record=candidates[0].record, variant=candidates[0].variant)
