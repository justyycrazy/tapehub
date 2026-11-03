"""ProviderRegistry：Provider 注册、manifest 加载、启用管理与能力目录聚合。

职责：

- 注册内置 Provider（akshare、tushare、tickflow、alphafeed、tqsdk）；
- 加载并校验各 Provider 的 manifest.yaml；
- 管理启用状态（manifest + 环境覆盖）；
- 可选依赖与延迟导入：适配器模块不在启动期导入任何 SDK；
- 隔离单个 Provider 的导入或初始化错误，不影响服务启动和其他 Provider；
- 聚合能力目录（与能力查询接口、公共路由共用同一份目录）。
"""

import logging
from dataclasses import dataclass, field
from importlib import import_module
from typing import TYPE_CHECKING, Protocol, TypeGuard

from tapehub.domain.capabilities import Capability
from tapehub.domain.intervals import Interval
from tapehub.domain.models import Adjust, AssetType, DataType, DataVariant
from tapehub.providers.manifest import ProviderManifest, load_builtin_manifest

if TYPE_CHECKING:
    from tapehub.domain.ports import HistoricalBarProvider

logger = logging.getLogger(__name__)

BUILTIN_PROVIDERS: tuple[str, ...] = (
    "akshare",
    "tushare",
    "tickflow",
    "alphafeed",
    "tqsdk",
    "twelvedata",
)


class _AdapterClass(Protocol):
    def __init__(self, name: str, manifest: ProviderManifest) -> None: ...


@dataclass(frozen=True)
class ResolvedCandidate:
    """路由候选：Provider 记录与其解析后的具体变体。"""

    record: "ProviderRecord"
    variant: DataVariant


def _is_adapter_class(obj: object) -> TypeGuard[_AdapterClass]:
    return callable(obj)


@dataclass
class ProviderRecord:
    """单个 Provider 的注册态。"""

    name: str
    manifest: ProviderManifest
    capabilities: list[Capability] = field(default_factory=list)
    default_variants: dict[AssetType, DataVariant] = field(default_factory=dict)
    adapter: "HistoricalBarProvider | None" = None
    implemented: bool = False
    enabled: bool = False
    load_error: str | None = None

    def resolve_variant(
        self,
        *,
        data_type: DataType,
        market: str,
        asset_type: AssetType,
        interval: Interval,
        adjust: Adjust,
        variant: DataVariant | None,
    ) -> DataVariant | None:
        """解析可执行的具体变体；不支持该能力时返回 ``None``。

        - ``variant`` 显式传递：严格匹配，不匹配即不支持，不做任何降级；
        - ``variant`` 为 ``None``（未表达偏好）：在其余维度匹配的能力中，
          优先取声明 ``default_variant`` 所在的能力；default 未覆盖其余维度时
          回退到声明顺序第一个匹配能力的第一个变体，可观测、确定性强。
        """
        if variant is not None:
            for cap in self.capabilities:
                if cap.supports(
                    data_type=data_type,
                    market=market,
                    asset_type=asset_type,
                    interval=interval,
                    adjust=adjust,
                    variant=variant,
                ):
                    return variant
            return None
        matched = [
            cap
            for cap in self.capabilities
            if cap.supports(
                data_type=data_type,
                market=market,
                asset_type=asset_type,
                interval=interval,
                adjust=adjust,
                variant=None,
            )
        ]
        if not matched:
            return None
        default = self.default_variants.get(asset_type)
        if default is not None:
            for cap in matched:
                if default in cap.variants:
                    return default
        return matched[0].variants[0]


class ProviderRegistry:
    """Provider 注册表。"""

    def __init__(self, records: list[ProviderRecord]) -> None:
        self._records: list[ProviderRecord] = []
        self._by_name: dict[str, ProviderRecord] = {}
        for record in records:
            if record.name in self._by_name:
                raise ValueError(f"duplicate provider registration: {record.name!r}")
            self._records.append(record)
            self._by_name[record.name] = record

    def get(self, name: str) -> ProviderRecord | None:
        return self._by_name.get(name)

    def list_records(self) -> list[ProviderRecord]:
        """按注册顺序返回全部记录（固定顺序，保证路由确定性）。"""
        return list(self._records)

    def resolve_candidates(
        self,
        *,
        data_type: DataType,
        market: str,
        asset_type: AssetType,
        interval: Interval,
        adjust: Adjust,
        variant: DataVariant | None,
    ) -> list["ResolvedCandidate"]:
        """启用、已实现且能力可解析的候选，按注册顺序返回并携带具体变体。"""
        resolved: list[ResolvedCandidate] = []
        for record in self._records:
            if not (record.enabled and record.implemented and record.adapter is not None):
                continue
            resolved_variant = record.resolve_variant(
                data_type=data_type,
                market=market,
                asset_type=asset_type,
                interval=interval,
                adjust=adjust,
                variant=variant,
            )
            if resolved_variant is not None:
                resolved.append(ResolvedCandidate(record=record, variant=resolved_variant))
        return resolved


def build_provider_record(
    name: str,
    manifest: ProviderManifest,
    adapter_cls: _AdapterClass,
    *,
    enabled_override: bool | None = None,
    settings: object = None,
) -> ProviderRecord:
    """由 manifest 与适配器类构建注册记录。"""
    enabled = manifest.enabled if enabled_override is None else enabled_override
    adapter = adapter_cls(name=name, manifest=manifest, settings=settings)
    return ProviderRecord(
        name=name,
        manifest=manifest,
        capabilities=manifest.to_domain_capabilities(),
        default_variants=manifest.resolved_default_variants(),
        adapter=adapter,
        implemented=bool(getattr(adapter_cls, "implemented", False)),
        enabled=enabled,
    )


def build_default_registry(settings: object) -> ProviderRegistry:
    """构建内置 Provider 注册表。

    任一 Provider 的 manifest 缺失、格式错误或适配器导入失败，
    都只隔离为该 Provider 的 ``load_error``，不影响服务启动。
    """
    overrides = getattr(settings, "provider_enabled_overrides", {}) or {}
    records: list[ProviderRecord] = []
    for name in BUILTIN_PROVIDERS:
        override = overrides.get(name)
        try:
            manifest = load_builtin_manifest(name)
            module = import_module(f"tapehub.providers.{name}.adapter")
            adapter_cls = getattr(module, "ProviderAdapter")
            if not _is_adapter_class(adapter_cls):
                raise TypeError("ProviderAdapter is not a class")
            record = build_provider_record(
                name, manifest, adapter_cls, enabled_override=override, settings=settings
            )
        except Exception as exc:  # noqa: BLE001 - 单个 Provider 错误必须被隔离
            logger.warning("provider %s failed to load: %s: %s", name, type(exc).__name__, exc)
            fallback = ProviderManifest(provider=name, enabled=False)
            record = ProviderRecord(
                name=name,
                manifest=fallback,
                capabilities=[],
                adapter=None,
                implemented=False,
                enabled=False,
                load_error=f"{type(exc).__name__}: {exc}",
            )
        records.append(record)
    return ProviderRegistry(records)
