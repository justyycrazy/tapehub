"""pytest 共享 fixture。

测试与本地环境完全解耦：

- ``make_settings`` 构造的 Settings 不读 ``.env``（``_env_file=None``），
  本地凭据文件不会影响任何测试；
- autouse fixture 清空进程环境中的 ``TAPEHUB_*`` 变量，
  外壳里配置的真实凭据同样不会泄漏进测试；
- 需要凭据的测试一律通过 ``make_settings`` 显式传参。
"""

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tapehub.api.app import create_app
from tapehub.providers.manifest import load_manifest_file
from tapehub.providers.registry import BUILTIN_PROVIDERS, ProviderRegistry, build_provider_record
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


@pytest.fixture()
def fake_registry() -> ProviderRegistry:
    record = build_fake_record()
    return ProviderRegistry([record])  # type: ignore[list-item]


@pytest.fixture()
def fake_client(fake_registry: ProviderRegistry, test_settings: Settings) -> TestClient:
    return TestClient(create_app(settings=test_settings, registry=fake_registry))


@pytest.fixture()
def default_client() -> TestClient:
    """不含 Fake Provider 的默认应用（仅内置 Provider，凭据视为未配置）。"""
    return TestClient(create_app(settings=make_settings()))


@pytest.fixture()
def counting_registry() -> tuple[ProviderRegistry, CountingProvider]:
    record = build_fake_record()
    counting = CountingProvider(record.adapter)  # type: ignore[arg-type]
    record.adapter = counting  # type: ignore[assignment]
    return ProviderRegistry([record]), counting  # type: ignore[list-item]


@pytest.fixture()
def counting_client(
    counting_registry: tuple[ProviderRegistry, CountingProvider], test_settings: Settings
) -> TestClient:
    registry, _ = counting_registry
    return TestClient(create_app(settings=test_settings, registry=registry))


def utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)
