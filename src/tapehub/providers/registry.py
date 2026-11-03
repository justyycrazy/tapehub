"""ProviderRegistry：Provider 注册、manifest 加载、启用管理与能力目录聚合。

职责：

- 注册内置 Provider（akshare、tushare、tickflow、alphafeed、tqsdk）；
- 加载并校验各 Provider 的 manifest.yaml（manifest 属于配置：缺失或校验失败
  直接启动失败，不做单 Provider 隔离）；
- 管理启用状态（manifest + 环境覆盖）；
- 可选依赖与延迟导入：适配器模块不在启动期导入任何 SDK；
- 隔离单个 Provider 的适配器导入错误（可选 SDK 未安装等），不影响服务启动；
- 校验跨 Provider 的缺省声明：每个被覆盖的资产类型恰好一个 default_provider；
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
    # 本 Provider 声明为缺省数据源的资产类型（manifest default_provider）
    default_for: frozenset[AssetType] = frozenset()
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
        self._validate_default_provider_coverage()

    def _validate_default_provider_coverage(self) -> None:
        """校验缺省 Provider 声明的覆盖性：每个被覆盖的资产类型恰好一个缺省。

        - ``0`` 个：该资产类型在 auto 路由中将退回注册顺序的隐式裁决，禁止；
        - ``>=2`` 个：多个 Provider 声明同一缺省，配置冲突，禁止。
        """
        covered: set[AssetType] = set()
        declared: dict[AssetType, list[str]] = {}
        for record in self._records:
            for cap in record.capabilities:
                covered.update(cap.asset_types)
            for asset_type in record.default_for:
                declared.setdefault(asset_type, []).append(record.name)
        missing = sorted(covered - set(declared), key=lambda item: item.value)
        if missing:
            names = ", ".join(item.value for item in missing)
            raise ValueError(
                "asset types without a default provider: "
                f"{names}; every capability-covered asset type must declare "
                "exactly one default_provider across manifests"
            )
        conflicts = sorted(
            (asset_type, names)
            for asset_type, names in declared.items()
            if len(names) > 1
        )
        if conflicts:
            details = "; ".join(
                f"{asset_type.value} -> [{', '.join(names)}]" for asset_type, names in conflicts
            )
            raise ValueError(
                f"multiple default providers declared for the same asset type: {details}"
            )

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
        """启用、已实现且能力可解析的候选，按优先级返回并携带具体变体。

        优先级：声明为该资产类型缺省（``default_provider``）的 Provider 在前，
        其余按注册顺序；显式 ``provider=X`` 不经过本方法，不受影响。
        """
        defaults: list[ResolvedCandidate] = []
        others: list[ResolvedCandidate] = []
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
            if resolved_variant is None:
                continue
            candidate = ResolvedCandidate(record=record, variant=resolved_variant)
            if asset_type in record.default_for:
                defaults.append(candidate)
            else:
                others.append(candidate)
        return defaults + others


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
        default_for=frozenset(manifest.default_provider or ()),
        adapter=adapter,
        implemented=bool(getattr(adapter_cls, "implemented", False)),
        enabled=enabled,
    )


def build_default_registry(settings: object) -> ProviderRegistry:
    """构建内置 Provider 注册表。

    manifest 缺失、格式错误或校验失败属于配置错误：直接抛出，启动失败；
    适配器导入失败（可选 SDK 未安装等）仍隔离为该 Provider 的
    ``load_error``，不影响服务启动（数据请求返回 ``PROVIDER_NOT_IMPLEMENTED``）。
    """
    overrides = getattr(settings, "provider_enabled_overrides", {}) or {}
    records: list[ProviderRecord] = []
    for name in BUILTIN_PROVIDERS:
        override = overrides.get(name)
        # manifest 是配置：加载/校验失败直接失败，不做单 Provider 隔离
        manifest = load_builtin_manifest(name)
        try:
            module = import_module(f"tapehub.providers.{name}.adapter")
            adapter_cls = getattr(module, "ProviderAdapter")
            if not _is_adapter_class(adapter_cls):
                raise TypeError("ProviderAdapter is not a class")
            record = build_provider_record(
                name, manifest, adapter_cls, enabled_override=override, settings=settings
            )
        except Exception as exc:  # noqa: BLE001 - 适配器导入错误保持隔离（SDK 可选）
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
