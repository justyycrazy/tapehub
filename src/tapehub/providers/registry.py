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


def _is_adapter_class(obj: object) -> TypeGuard[_AdapterClass]:
    return callable(obj)


@dataclass
class ProviderRecord:
    """单个 Provider 的注册态。"""

    name: str
    manifest: ProviderManifest
    capabilities: list[Capability] = field(default_factory=list)
    adapter: "HistoricalBarProvider | None" = None
    implemented: bool = False
    enabled: bool = False
    load_error: str | None = None

    def supports(
        self,
        *,
        data_type: DataType,
        market: str,
        asset_type: AssetType,
        interval: Interval,
        adjust: Adjust,
        variant: DataVariant,
    ) -> bool:
        return any(
            cap.supports(
                data_type=data_type,
                market=market,
                asset_type=asset_type,
                interval=interval,
                adjust=adjust,
                variant=variant,
            )
            for cap in self.capabilities
        )


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

    def candidates(
        self,
        *,
        data_type: DataType,
        market: str,
        asset_type: AssetType,
        interval: Interval,
        adjust: Adjust,
        variant: DataVariant,
    ) -> list[ProviderRecord]:
        """只返回启用、已实现且完整能力条件全部匹配的候选。"""
        return [
            record
            for record in self._records
            if record.enabled
            and record.implemented
            and record.adapter is not None
            and record.supports(
                data_type=data_type,
                market=market,
                asset_type=asset_type,
                interval=interval,
                adjust=adjust,
                variant=variant,
            )
        ]


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
