"""ProviderRegistry：Provider 注册、manifest 加载、启用管理与能力目录聚合。

职责：

- 注册内置 Provider（顺序仅决定初始化顺序，不承载路由语义）；
- 加载并校验各 Provider 的 manifest.yaml（manifest 属于配置：缺失或校验失败
  直接启动失败，不做单 Provider 隔离）；
- 凭据驱动的注册：内置 Provider 需要凭据的（.env 相应变量全部非空）才启用；
  凭据缺失的 Provider 保留能力目录但标记停用（数据请求返回 PROVIDER_DISABLED）；
- 管理启用状态（显式环境覆盖 > 凭据推断；manifest 不承载启用声明）；
- 可选依赖与延迟导入：适配器模块不在启动期导入任何 SDK；
- 隔离单个 Provider 的适配器导入错误（可选 SDK 未安装等），不影响服务启动；
- 加载缺省路由配置（provider_routing.yaml，跨 Provider 能力冲突的集中裁决）
  并校验其与能力目录的交叉一致性；
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
from tapehub.providers.routing import RoutingKey, RoutingTable, load_routing_config

if TYPE_CHECKING:
    from tapehub.domain.ports import HistoricalBarProvider, QuoteProvider

logger = logging.getLogger(__name__)

# 内置 Provider 注册名单：顺序仅决定初始化顺序与能力目录展示顺序；
# auto 路由的缺省裁决由 provider_routing.yaml 的完整回退链声明。
BUILTIN_PROVIDERS: tuple[str, ...] = (
    "akshare",
    "alphafeed",
    "baostock",
    "tickflow",
    "ths",
    "tiingo",
    "tushare",
    "tqsdk",
    "twelvedata",
    "futunn",
)

# 凭据驱动的注册判定：Provider → 必须全部非空的 Settings 凭据字段。
# akshare 无凭据概念，恒可注册。判定只看非空（strip 后），不做格式校验——
# 格式错误的凭据保持「请求时报 UPSTREAM_ERROR」，启动不因凭据内容失败。
_REQUIRED_CREDENTIALS: dict[str, tuple[str, ...]] = {
    "alphafeed": ("alphafeed_api_key",),
    "tickflow": ("tickflow_api_key",),
    "ths": ("ths_api_key",),
    "tiingo": ("tiingo_api_key",),
    "tushare": ("tushare_token",),
    "tqsdk": ("tqsdk_auth_user", "tqsdk_auth_password"),
    "twelvedata": ("twelvedata_api_key",),
    "futunn": ("futunn_app_key", "futunn_private_key_path"),
}


def _missing_credential_fields(settings: object, name: str) -> list[str]:
    """返回该 Provider 缺失（空白）的凭据 Settings 字段名列表。"""
    required = _REQUIRED_CREDENTIALS.get(name, ())
    return [f for f in required if not str(getattr(settings, f, "") or "").strip()]


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
    # 适配器可同时实现 HistoricalBarProvider 与 QuoteProvider（鸭子类型）
    adapter: "HistoricalBarProvider | QuoteProvider | None" = None
    implemented: bool = False
    enabled: bool = False
    # 停用原因（凭据缺失/显式覆盖禁用），供日志与显式请求被拒时诊断
    disabled_reason: str | None = None
    load_error: str | None = None

    def resolve_variant(
        self,
        *,
        data_type: DataType,
        market: str,
        asset_type: AssetType,
        interval: Interval | None = None,
        adjust: Adjust | None = None,
        variant: DataVariant | None = None,
    ) -> DataVariant | None:
        """解析可执行的具体变体；不支持该能力时返回 ``None``。

        - ``variant`` 显式传递：严格匹配，不匹配即不支持，不做任何降级；
        - ``variant`` 为 ``None``（未表达偏好）：在其余维度匹配的能力中，
          优先取声明 ``default_variant`` 所在的能力；default 未覆盖其余维度时
          回退到声明顺序第一个匹配能力的第一个变体，可观测、确定性强；
        - ``interval``/``adjust`` 为 ``None`` 表示请求不携带该维度
          （quote 快照类查询），能力匹配跳过对应维度。
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
    """Provider 注册表。

    ``routing`` 是缺省路由表（provider_routing.yaml 加载结果）：
    ``(data_type, market, asset_type) → 完整回退链``。路由语义与注册期
    完整性校验规则见 README.md「总体设计」与 provider_routing.yaml 头注释。
    """

    def __init__(self, records: list[ProviderRecord], routing: RoutingTable) -> None:
        self._records: list[ProviderRecord] = []
        self._by_name: dict[str, ProviderRecord] = {}
        for record in records:
            if record.name in self._by_name:
                raise ValueError(f"duplicate provider registration: {record.name!r}")
            self._records.append(record)
            self._by_name[record.name] = record
        self._routing: RoutingTable = routing
        self._validate_routing_coverage()

    def _validate_routing_coverage(self) -> None:
        """路由表与能力目录的交叉校验：双向覆盖、order 引用合法。"""
        covered: set[RoutingKey] = set()
        for record in self._records:
            for cap in record.capabilities:
                for market in cap.markets:
                    for asset_type in cap.asset_types:
                        covered.add((cap.data_type, market, asset_type))
        declared = set(self._routing)
        missing = sorted(covered - declared)
        if missing:
            raise ValueError(
                "routing config missing entries for covered capabilities: "
                f"{missing}; every capability-covered (data_type, market, "
                "asset_type) combination must have an explicit routing entry"
            )
        extra = sorted(declared - covered)
        if extra:
            raise ValueError(
                f"routing config declares capabilities not covered by any provider: {extra}"
            )
        for key, order in self._routing.items():
            for name in order:
                record = self._by_name.get(name)
                if record is None:
                    raise ValueError(
                        f"routing order for {key} references unknown provider {name!r}"
                    )
                if not _covers_combination(record, *key):
                    raise ValueError(
                        f"routing order for {key} references provider {name!r} "
                        "which does not declare this capability"
                    )

    def get(self, name: str) -> ProviderRecord | None:
        return self._by_name.get(name)

    def list_records(self) -> list[ProviderRecord]:
        """按注册顺序返回全部记录（固定顺序，保证目录展示确定性）。"""
        return list(self._records)

    def resolve_candidates(
        self,
        *,
        data_type: DataType,
        market: str,
        asset_type: AssetType,
        interval: Interval | None = None,
        adjust: Adjust | None = None,
        variant: DataVariant | None = None,
    ) -> list["ResolvedCandidate"]:
        """按缺省路由链返回「启用、已实现且能力可解析」的候选。

        链序即优先级：第一个可完整解析该请求的 Provider 为缺省数据源；
        未启用（凭据缺失/显式禁用）或能力不匹配该请求具体维度的条目自动
        跳过。显式 ``provider=X`` 不经过本方法，不受影响。
        """
        order = self._routing.get((data_type, market, asset_type), ())
        candidates: list[ResolvedCandidate] = []
        for name in order:
            record = self._by_name[name]
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
            candidates.append(ResolvedCandidate(record=record, variant=resolved_variant))
        return candidates


def _covers_combination(
    record: ProviderRecord, data_type: DataType, market: str, asset_type: AssetType
) -> bool:
    """Provider 是否声明了该 (data_type, market, asset_type) 组合的能力。"""
    return any(
        cap.data_type == data_type
        and market in cap.markets
        and asset_type in cap.asset_types
        for cap in record.capabilities
    )


def build_provider_record(
    name: str,
    manifest: ProviderManifest,
    adapter_cls: _AdapterClass,
    *,
    enabled: bool = True,
    settings: object = None,
) -> ProviderRecord:
    """由 manifest 与适配器类构建注册记录。

    ``enabled`` 默认 True（测试构造便利）；生产路径由 ``build_default_registry``
    的启用裁决无条件覆盖（显式环境覆盖 > 凭据推断）。
    """
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

    失败分层：

    - manifest / 路由配置缺失、格式错误或校验失败属于配置错误：直接抛出，
      启动失败，不做单 Provider 隔离；
    - 适配器导入失败（可选 SDK 未安装等）仍隔离为该 Provider 的
      ``load_error``，不影响服务启动（数据请求返回 ``PROVIDER_NOT_IMPLEMENTED``）；
    - 凭据缺失的 Provider 保留能力目录但标记停用（``enabled=False`` +
      ``disabled_reason``）：auto 路由跳过，显式请求返回 ``PROVIDER_DISABLED``
      并携带缺失的凭据变量名。
    """
    overrides = getattr(settings, "provider_enabled_overrides", {}) or {}
    routing = load_routing_config(getattr(settings, "routing_config_path", None))
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
            record = build_provider_record(name, manifest, adapter_cls, settings=settings)
        except Exception as exc:  # noqa: BLE001 - 适配器导入错误保持隔离（SDK 可选）
            logger.warning("provider %s failed to load: %s: %s", name, type(exc).__name__, exc)
            fallback = ProviderManifest(provider=name)
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
            continue
        _apply_enable_decision(record, name, override, settings)
        records.append(record)
    return ProviderRegistry(records, routing)


def _apply_enable_decision(
    record: ProviderRecord, name: str, override: bool | None, settings: object
) -> None:
    """启用裁决两级：显式环境覆盖 > 凭据推断（manifest 不承载启用声明）。"""
    missing = _missing_credential_fields(settings, name)
    env_names = ", ".join(f"TAPEHUB_{f.upper()}" for f in missing)
    if override is False:
        record.enabled = False
        record.disabled_reason = "disabled via TAPEHUB_PROVIDER_ENABLED_OVERRIDES"
        return
    if override is True:
        record.enabled = True
        if missing:
            logger.warning(
                "provider %s explicitly enabled but credentials missing (%s); "
                "data requests may fail",
                name,
                env_names,
            )
        return
    if missing:
        record.enabled = False
        record.disabled_reason = f"missing credentials: {env_names}"
        logger.warning(
            "provider %s not registered: missing credentials (%s); "
            "set the environment variables to enable it",
            name,
            env_names,
        )
        return
    record.enabled = True
