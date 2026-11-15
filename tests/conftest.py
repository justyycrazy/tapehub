"""pytest 共享 fixture。

测试与本地环境完全解耦：

- ``make_settings`` 构造的 Settings 不读 ``.env``（``_env_file=None``），
  本地凭据文件不会影响任何测试；
- autouse fixture 清空进程环境中的 ``TAPEHUB_*`` 变量，
  外壳里配置的真实凭据同样不会泄漏进测试；
- 需要凭据的测试一律通过 ``make_settings`` 显式传参。
"""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tapehub.api.app import create_app
from tapehub.domain.models import AssetType, DataType
from tapehub.providers.manifest import load_manifest_file
from tapehub.providers.registry import BUILTIN_PROVIDERS, ProviderRegistry, build_provider_record
from tapehub.providers.routing import RoutingTable
from tapehub.settings import Settings
from tests.fixtures.providers.fake_provider import FakeBarProvider

FIXTURES_DIR = Path(__file__).parent / "fixtures"

BUILTIN_PROVIDER_NAMES = list(BUILTIN_PROVIDERS)


def make_settings(**overrides: object) -> Settings:
    """测试专用 Settings 构造：不读 ``.env``，显式传参优先。

    进程环境变量仍然生效（pydantic-settings 语义），由 autouse 的
    ``isolated_tapehub_env`` 保证测试运行环境中没有 ``TAPEHUB_*`` 变量。
    """
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def isolated_tapehub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清空进程环境中的 ``TAPEHUB_*`` 变量，隔离外壳里配置的真实凭据。"""
    for key in [name for name in os.environ if name.startswith("TAPEHUB_")]:
        monkeypatch.delenv(key, raising=False)


class CountingProvider:
    """包装适配器并统计调用次数，用于验证某些接口不访问 Provider。"""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.calls = 0

    async def fetch_bars(self, query: object) -> object:
        self.calls += 1
        return await self._inner.fetch_bars(query)  # type: ignore[attr-defined]


@pytest.fixture()
def test_settings() -> Settings:
    return make_settings(max_limit=1000, catalog_version="test")


def build_fake_record() -> object:
    """加载 Fake Provider manifest 并构建注册记录。"""
    manifest = load_manifest_file(FIXTURES_DIR / "manifests" / "fake_provider.yaml")
    return build_provider_record(manifest.provider, manifest, FakeBarProvider)


def fake_routing() -> RoutingTable:
    """Fake Provider 的缺省路由表：与 fake_provider.yaml 声明的能力一一对应
    （bars/CN × stock/etf/index/contract + quotes/CN × stock/index，
    单 Provider 独占故各为单元素链）。"""
    keys = [
        (DataType.BARS, "CN", at)
        for at in (AssetType.STOCK, AssetType.ETF, AssetType.INDEX, AssetType.CONTRACT)
    ]
    keys += [
        (DataType.QUOTE, "CN", at) for at in (AssetType.STOCK, AssetType.INDEX)
    ]
    return {key: ("fake-provider",) for key in keys}


def single_provider_routing(record: object) -> RoutingTable:
    """测试构造辅助：单 Provider 独占其声明全部组合的显式路由表。

    仅用于测试 fixture 构造（生产路径的路由表必须来自 provider_routing.yaml）；
    多 Provider 注册表不适用，须显式写出回退链。
    """
    keys = {
        (cap.data_type, market, asset_type)
        for cap in record.capabilities  # type: ignore[attr-defined]
        for market in cap.markets
        for asset_type in cap.asset_types
    }
    return {key: (record.name, ) for key in keys}  # type: ignore[attr-defined]


@pytest.fixture()
def fake_registry() -> ProviderRegistry:
    record = build_fake_record()
    return ProviderRegistry([record], fake_routing())  # type: ignore[list-item]


@pytest.fixture()
def fake_client(fake_registry: ProviderRegistry, test_settings: Settings) -> TestClient:
    with TestClient(create_app(settings=test_settings, registry=fake_registry)) as client:
        # with 形态确保 lifespan 执行（停机钩子/资源收尾在测试中真实生效）
        yield client


@pytest.fixture()
def default_client() -> TestClient:
    """不含 Fake Provider 的默认应用。

    凭据视为未配置：需要凭据的内置 Provider 不启用（能力目录可见但
    enabled=False，auto 路由跳过），akshare（无凭据概念）仍注册。
    """
    with TestClient(create_app(settings=make_settings())) as client:
        yield client


@pytest.fixture()
def counting_registry() -> tuple[ProviderRegistry, CountingProvider]:
    record = build_fake_record()
    counting = CountingProvider(record.adapter)  # type: ignore[arg-type]
    record.adapter = counting  # type: ignore[assignment]
    return ProviderRegistry([record], fake_routing()), counting  # type: ignore[list-item]


@pytest.fixture()
def counting_client(
    counting_registry: tuple[ProviderRegistry, CountingProvider], test_settings: Settings
) -> TestClient:
    registry, _ = counting_registry
    with TestClient(create_app(settings=test_settings, registry=registry)) as client:
        yield client


