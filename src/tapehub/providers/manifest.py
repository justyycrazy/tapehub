"""Provider manifest 加载与校验。

manifest 将“公共能力声明”与“内部操作映射”分开：
capabilities 面向能力目录与公共路由，operations 面向适配器内部二级路由。
加载时校验周期格式，非法或不支持的周期不得进入能力目录。
"""

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tapehub.domain.capabilities import Capability, IntervalEntry, IntervalHistory
from tapehub.domain.errors import ManifestError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import (
    CN_TIMEZONE,
    Adjust,
    AssetType,
    DataType,
    DataVariant,
    Delivery,
    ensure_aware,
)


class ManifestHistory(BaseModel):
    """历史覆盖声明：``start``（固定起点）与 ``retention_days``（滚动保留天数）互斥。

    边界必须携带时区偏移（如 ``2016-01-05T00:00:00+08:00``），不做隐式解释；
    到市场规范时区的归一化在 ``ManifestCapability.to_domain`` 完成。
    """

    model_config = ConfigDict(extra="forbid")

    start: datetime | None = None
    end: datetime | None = None
    retention_days: int | None = Field(default=None, ge=1)

    @field_validator("start", "end")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return ensure_aware(value, "history boundary")

    @model_validator(mode="after")
    def _start_and_retention_mutually_exclusive(self) -> "ManifestHistory":
        if self.start is not None and self.retention_days is not None:
            raise ValueError("history start and retention_days are mutually exclusive")
        return self


def _to_zone(value: datetime | None, zone: ZoneInfo | None) -> datetime | None:
    """边界归一化到目标时区；值为空或未指定市场时区时原样返回。"""
    if value is None or zone is None:
        return value
    return value.astimezone(zone)


class ManifestInterval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Interval
    history: ManifestHistory = Field(default_factory=ManifestHistory)


class ManifestCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_type: DataType
    markets: list[str]
    asset_types: list[AssetType]
    delivery: list[Delivery]
    variants: list[DataVariant]
    intervals: list[ManifestInterval]
    adjustments: list[Adjust]
    fields: list[str]

    def to_domain(self) -> Capability:
        # 历史边界统一归一化为北京时间（全市场统一口径）
        zone = CN_TIMEZONE
        intervals = [
            IntervalEntry(
                code=entry.code,
                history=IntervalHistory(
                    start=_to_zone(entry.history.start, zone),
                    end=_to_zone(entry.history.end, zone),
                    retention_days=entry.history.retention_days,
                ),
            )
            for entry in self.intervals
        ]
        return Capability(
            data_type=self.data_type,
            markets=list(self.markets),
            asset_types=list(self.asset_types),
            delivery=list(self.delivery),
            variants=list(self.variants),
            intervals=intervals,
            adjustments=list(self.adjustments),
            fields=list(self.fields),
        )


class OperationMatch(BaseModel):
    """内部操作匹配键：data_type × market × asset_type × interval × adjust × variant。

    所有维度必须显式声明，不依赖默认值，避免“未传递参数的默认值不一致”。
    对请求无意义的维度显式声明 ``null``（如 quote 快照无周期/复权概念），
    ``null`` 只与「未携带该维度的请求」（quote 查询）匹配。
    """

    model_config = ConfigDict(extra="forbid")

    data_type: DataType
    market: str
    asset_type: AssetType
    interval: Interval | None = None
    adjust: Adjust | None = None
    variant: DataVariant


class ManifestOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    match: OperationMatch
    operation: str = Field(min_length=1)


class ProviderManifest(BaseModel):
    """Provider manifest。

    ``default_variant`` 声明「Provider × 资产类型」级缺省变体：请求未表达
    ``variant`` 偏好时使用。声明为标量时作用于全部资产类型；声明为映射时
    必须覆盖 capabilities 中出现的每个 asset_type，且每个值必须属于该
    资产类型下实际声明的 variants。有 capabilities 就必须显式声明，
    不做任何隐式推断；无 capabilities 的空壳 manifest（占位/测试桩）允许省略。

    ``default_provider`` 声明本 Provider 是哪些资产类型的缺省数据源
    （跨 Provider 的 auto 路由优先级）：列表值逐项声明，空列表 ``[]``
    表示对任何资产类型都不是缺省。有 capabilities 就必须显式声明；
    只能声明本 Provider 实际声明了能力的资产类型，不得编造。跨 Provider 的
    「每个资产类型恰好一个缺省」约束在 ProviderRegistry 构建期统一校验。
    """

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    enabled: bool = False
    default_variant: DataVariant | dict[AssetType, DataVariant] | None = None
    default_provider: list[AssetType] | None = None
    capabilities: list[ManifestCapability] = Field(default_factory=list)
    operations: list[ManifestOperation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_duplicate_operations(self) -> "ProviderManifest":
        seen: set[tuple[Any, ...]] = set()
        for op in self.operations:
            m = op.match
            key = (m.data_type, m.market, m.asset_type, m.interval, m.adjust, m.variant)
            if key in seen:
                raise ValueError(f"duplicate operation match for {key}")
            seen.add(key)
        return self

    @model_validator(mode="after")
    def _default_variant_covers_capabilities(self) -> "ProviderManifest":
        declared: dict[AssetType, set[DataVariant]] = {}
        for cap in self.capabilities:
            for asset_type in cap.asset_types:
                declared.setdefault(asset_type, set()).update(cap.variants)
        if not declared:
            if self.default_variant is not None:
                raise ValueError("default_variant declared but no capabilities exist")
            return self
        if self.default_variant is None:
            raise ValueError(
                "default_variant is required when capabilities are declared; "
                "declare a scalar covering all asset types or a per-asset-type mapping"
            )
        if isinstance(self.default_variant, DataVariant):
            resolved = dict.fromkeys(declared, self.default_variant)
        else:
            resolved = dict(self.default_variant)
        missing = sorted(set(declared) - set(resolved), key=lambda item: item.value)
        if missing:
            names = ", ".join(item.value for item in missing)
            raise ValueError(f"default_variant mapping misses asset types: {names}")
        for asset_type, variant in resolved.items():
            if variant not in declared[asset_type]:
                raise ValueError(
                    f"default_variant {variant.value!r} for asset type "
                    f"{asset_type.value!r} is not among declared variants "
                    f"{sorted(v.value for v in declared[asset_type])}"
                )
        return self

    @model_validator(mode="after")
    def _default_provider_declares_owned_asset_types(self) -> "ProviderManifest":
        covered: set[AssetType] = set()
        for cap in self.capabilities:
            covered.update(cap.asset_types)
        if not covered:
            if self.default_provider is not None:
                raise ValueError("default_provider declared but no capabilities exist")
            return self
        if self.default_provider is None:
            raise ValueError(
                "default_provider is required when capabilities are declared; "
                "declare the asset types this provider is the default source for, "
                "or an empty list to opt out"
            )
        unknown = sorted(set(self.default_provider) - covered, key=lambda item: item.value)
        if unknown:
            names = ", ".join(item.value for item in unknown)
            raise ValueError(
                f"default_provider declares asset types without capabilities: {names}"
            )
        return self

    def resolved_default_variants(self) -> dict[AssetType, DataVariant]:
        """归一化为 ``{asset_type: variant}`` 映射；仅可在校验通过的实例上调用。"""
        if self.default_variant is None:
            return {}
        if isinstance(self.default_variant, DataVariant):
            covered: set[AssetType] = set()
            for cap in self.capabilities:
                covered.update(cap.asset_types)
            return dict.fromkeys(covered, self.default_variant)
        return dict(self.default_variant)

    def to_domain_capabilities(self) -> list[Capability]:
        return [cap.to_domain() for cap in self.capabilities]


def manifest_from_dict(data: dict[str, Any], *, source: str = "manifest") -> ProviderManifest:
    """从字典构建 manifest，校验失败抛出 ``ManifestError``。"""
    try:
        return ProviderManifest.model_validate(data)
    except Exception as exc:  # noqa: BLE001 - 统一转换为 ManifestError
        raise ManifestError(f"invalid provider manifest ({source}): {exc}") from exc


def load_manifest_file(path: Path) -> ProviderManifest:
    """从 YAML 文件加载并校验 manifest。"""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError(f"cannot read manifest file: {path.name}") from exc
    except yaml.YAMLError as exc:
        raise ManifestError(f"cannot parse manifest YAML: {path.name}") from exc
    if not isinstance(raw, dict):
        raise ManifestError(f"manifest must be a mapping: {path.name}")
    return manifest_from_dict(raw, source=path.name)


def load_builtin_manifest(provider: str) -> ProviderManifest:
    """加载 tapehub 内置 Provider 的 manifest.yaml。"""
    from importlib import resources

    try:
        text = (resources.files(f"tapehub.providers.{provider}") / "manifest.yaml").read_text(
            encoding="utf-8"
        )
    except (OSError, ModuleNotFoundError) as exc:
        raise ManifestError(f"builtin manifest not found for provider {provider!r}") from exc
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise ManifestError(f"builtin manifest must be a mapping: {provider!r}")
    return manifest_from_dict(raw, source=f"builtin:{provider}")
