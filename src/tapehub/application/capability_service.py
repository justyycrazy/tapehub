"""能力查询服务：读取本地能力目录，不实时访问上游。

与公共 ProviderRouter 复用同一份 ProviderRegistry 能力目录。
``enabled`` 与 ``runtime_status`` 是两个不同概念：前者是目录声明的启用
状态，后者是适配器长连接运行时的实时生命周期状态——经鸭子类型读取
适配器的 ``runtime_status`` 属性（与 lifespan 的 ``shutdown_runtime``
钩子同款模式，不感知具体 Provider）；未暴露该属性的适配器（无长连接
概念）与读取失败一律回退 ``unknown``，不伪造实时健康状态。
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tapehub.domain.capabilities import Capability
from tapehub.domain.errors import InvalidRequestError, ProviderNotFoundError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import AssetType, DataType, DataVariant

if TYPE_CHECKING:
    from tapehub.providers.registry import ProviderRecord, ProviderRegistry

RUNTIME_STATUS_UNKNOWN = "unknown"


def _runtime_status(record: "ProviderRecord") -> str:
    """鸭子类型读取适配器运行时状态；无该属性或非字符串回退 ``unknown``。"""
    adapter = record.adapter
    if adapter is None:
        return RUNTIME_STATUS_UNKNOWN
    try:
        value = getattr(adapter, "runtime_status", None)
    except Exception:  # noqa: BLE001 - 状态读取异常不影响能力目录可用性
        return RUNTIME_STATUS_UNKNOWN
    return value if isinstance(value, str) else RUNTIME_STATUS_UNKNOWN


@dataclass(frozen=True)
class ProviderCapabilitiesView:
    """Provider 能力视图。"""

    name: str
    enabled: bool
    runtime_status: str
    capabilities: list[Capability] = field(default_factory=list)
    # 「Provider × 资产类型」级缺省变体（manifest default_variant 归一化）
    default_variants: dict[AssetType, DataVariant] = field(default_factory=dict)
    # 声明为缺省数据源的资产类型（manifest default_provider，保持声明顺序）
    default_provider: list[AssetType] = field(default_factory=list)


class CapabilityService:
    """能力目录服务。"""

    def __init__(self, registry: "ProviderRegistry") -> None:
        self._registry = registry

    def list_providers(
        self,
        *,
        provider: str | None = None,
        data_type: DataType | None = None,
        market: str | None = None,
        asset_type: AssetType | None = None,
        interval: Interval | None = None,
        variant: DataVariant | None = None,
    ) -> list[ProviderCapabilitiesView]:
        """列出（可过滤的）Provider 能力视图。

        - ``provider`` 是精确名称过滤：名称不存在时结果为空；
        - 其余过滤作用于 capability 级别，无匹配能力的 Provider 不返回；
        - 仅按 ``provider`` 过滤时，存在但能力为空的 Provider 仍然返回。
        """
        views: list[ProviderCapabilitiesView] = []
        for record in self._registry.list_records():
            if provider is not None and record.name != provider:
                continue
            capabilities = [
                cap
                for cap in record.capabilities
                if _matches(
                    cap,
                    data_type=data_type,
                    market=market,
                    asset_type=asset_type,
                    interval=interval,
                    variant=variant,
                )
            ]
            capability_filters_applied = any((data_type, market, asset_type, interval, variant))
            if capabilities or not capability_filters_applied:
                views.append(
                    ProviderCapabilitiesView(
                        name=record.name,
                        enabled=record.enabled,
                        runtime_status=_runtime_status(record),
                        capabilities=capabilities,
                        default_variants=record.default_variants,
                        default_provider=list(record.manifest.default_provider or ()),
                    )
                )
        return views

    def get_provider(self, name: str) -> ProviderCapabilitiesView:
        """查询单个 Provider 能力；不存在时返回 ``PROVIDER_NOT_FOUND``。"""
        record = self._registry.get(name)
        if record is None:
            raise ProviderNotFoundError(
                f"Provider {name!r} not found.",
                details={"provider": name},
            )
        return ProviderCapabilitiesView(
            name=record.name,
            enabled=record.enabled,
            runtime_status=_runtime_status(record),
            capabilities=list(record.capabilities),
            default_variants=record.default_variants,
            default_provider=list(record.manifest.default_provider or ()),
        )


def _matches(
    cap: Capability,
    *,
    data_type: DataType | None,
    market: str | None,
    asset_type: AssetType | None,
    interval: Interval | None,
    variant: DataVariant | None,
) -> bool:
    if data_type is not None and cap.data_type != data_type:
        return False
    if market is not None and market not in cap.markets:
        return False
    if asset_type is not None and asset_type not in cap.asset_types:
        return False
    if variant is not None and variant not in cap.variants:
        return False
    if interval is not None and not any(entry.code == interval for entry in cap.intervals):
        return False
    return True


def parse_capability_filters(
    *,
    interval: str | None,
) -> Interval | None:
    """校验能力过滤参数中的周期字符串。"""
    if interval is None:
        return None
    try:
        return Interval.parse(interval)
    except ValueError as exc:
        raise InvalidRequestError(
            "Invalid capability filter.",
            details={"param": "interval", "reason": str(exc)},
        ) from exc


__all__ = ["CapabilityService", "ProviderCapabilitiesView", "parse_capability_filters"]
