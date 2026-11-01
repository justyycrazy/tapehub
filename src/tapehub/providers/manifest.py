"""Provider manifest 加载与校验。

manifest 将“公共能力声明”与“内部操作映射”分开：
capabilities 面向能力目录与公共路由，operations 面向适配器内部二级路由。
加载时校验周期格式，非法或不支持的周期不得进入能力目录。
"""

from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tapehub.domain.capabilities import Capability, IntervalEntry, IntervalHistory
from tapehub.domain.errors import ManifestError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import Adjust, AssetType, DataType, Delivery, ensure_utc


class ManifestHistory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: datetime | None = None
    end: datetime | None = None

    @field_validator("start", "end")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return ensure_utc(value, "history boundary")


class ManifestInterval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Interval
    history: ManifestHistory = Field(default_factory=ManifestHistory)

    def to_domain(self) -> IntervalEntry:
        return IntervalEntry(
            code=self.code,
            history=IntervalHistory(start=self.history.start, end=self.history.end),
        )


class ManifestCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_type: DataType
    markets: list[str]
    asset_types: list[AssetType]
    delivery: list[Delivery]
    intervals: list[ManifestInterval]
    adjustments: list[Adjust]
    fields: list[str]

    def to_domain(self) -> Capability:
        return Capability(
            data_type=self.data_type,
            markets=list(self.markets),
            asset_types=list(self.asset_types),
            delivery=list(self.delivery),
            intervals=[entry.to_domain() for entry in self.intervals],
            adjustments=list(self.adjustments),
            fields=list(self.fields),
        )


class OperationMatch(BaseModel):
    """内部操作匹配键：data_type × market × asset_type × interval × adjust。"""

    model_config = ConfigDict(extra="forbid")

    data_type: DataType
    market: str
    asset_type: AssetType
    interval: Interval
    adjust: Adjust


class ManifestOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    match: OperationMatch
    operation: str = Field(min_length=1)


class ProviderManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    enabled: bool = False
    capabilities: list[ManifestCapability] = Field(default_factory=list)
    operations: list[ManifestOperation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_duplicate_operations(self) -> "ProviderManifest":
        seen: set[tuple[Any, ...]] = set()
        for op in self.operations:
            m = op.match
            key = (m.data_type, m.market, m.asset_type, m.interval, m.adjust)
            if key in seen:
                raise ValueError(f"duplicate operation match for {key}")
            seen.add(key)
        return self

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
