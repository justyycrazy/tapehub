"""缺省路由顺序配置的加载与格式校验。

``provider_routing.yaml`` 集中声明跨 Provider 的缺省裁决：同一
(data_type, market, asset_type) 组合被多个 Provider 覆盖时的完整回退链。

- 格式校验（三维 match 显式、order 非空且名称不重复、组合不重复）在本
  模块完成——providers 层可 import yaml；
- 与注册能力的交叉校验（覆盖完整性、order 引用合法性与能力支持）在
  ``ProviderRegistry`` 构建期完成（需要聚合后的能力目录）。

配置属启动期配置：加载或校验失败抛出 ``RoutingConfigError``，启动失败。
"""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tapehub.domain.errors import RoutingConfigError
from tapehub.domain.models import AssetType, DataType

# 路由表键：data_type × market × asset_type（order 只决定 Provider 级
# 优先顺序，interval/adjust/variant 由六维能力匹配在逐个候选上解析）。
RoutingKey = tuple[DataType, str, AssetType]
RoutingTable = dict[RoutingKey, tuple[str, ...]]

SUPPORTED_CONFIG_VERSION = 1


class RoutingMatch(BaseModel):
    """路由条目匹配键：三维全显式，不做隐式通配。"""

    model_config = ConfigDict(extra="forbid")

    data_type: DataType
    market: str
    asset_type: AssetType


class RoutingEntry(BaseModel):
    """单条路由声明：组合 → 完整回退链（首个可用者为缺省）。"""

    model_config = ConfigDict(extra="forbid")

    match: RoutingMatch
    order: list[str] = Field(min_length=1)

    @field_validator("order")
    @classmethod
    def _no_duplicate_names(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("order contains duplicate provider names")
        return value


class RoutingConfigFile(BaseModel):
    """provider_routing.yaml 顶层结构。"""

    model_config = ConfigDict(extra="forbid")

    version: int
    defaults: list[RoutingEntry]

    @model_validator(mode="after")
    def _version_supported(self) -> "RoutingConfigFile":
        if self.version != SUPPORTED_CONFIG_VERSION:
            raise ValueError(
                f"unsupported routing config version: {self.version!r} "
                f"(supported: {SUPPORTED_CONFIG_VERSION})"
            )
        return self

    @model_validator(mode="after")
    def _no_duplicate_matches(self) -> "RoutingConfigFile":
        seen: set[tuple[DataType, str, AssetType]] = set()
        for entry in self.defaults:
            m = entry.match
            key = (m.data_type, m.market, m.asset_type)
            if key in seen:
                raise ValueError(f"duplicate routing entry for {key}")
            seen.add(key)
        return self


def builtin_routing_path() -> Path:
    """包内内置路由配置的资源路径。"""
    from importlib import resources

    traversable = resources.files("tapehub.config") / "provider_routing.yaml"
    # importlib.resources 返回 Traversable；常规安装下为真实文件系统路径
    return Path(str(traversable))


def load_routing_config(path: Path | str | None = None) -> RoutingTable:
    """加载路由配置为注册表可用的路由表。

    - ``path`` 为 ``None`` 时加载包内内置版本；
    - 显式路径加载外部文件（整体替换内置版本，不做合并）；
    - 文件缺失、YAML 非法或结构校验失败抛 ``RoutingConfigError``。
    """
    target = builtin_routing_path() if path is None else Path(path)
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RoutingConfigError(f"cannot read routing config: {target.name}") from exc
    except yaml.YAMLError as exc:
        raise RoutingConfigError(f"cannot parse routing config YAML: {target.name}") from exc
    if not isinstance(raw, dict):
        raise RoutingConfigError(f"routing config must be a mapping: {target.name}")
    try:
        config = RoutingConfigFile.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - 统一转换为 RoutingConfigError
        raise RoutingConfigError(f"invalid routing config ({target.name}): {exc}") from exc

    table: RoutingTable = {}
    for entry in config.defaults:
        m = entry.match
        table[(m.data_type, m.market, m.asset_type)] = tuple(entry.order)
    return table
