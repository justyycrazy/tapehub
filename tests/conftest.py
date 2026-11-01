"""pytest 共享 fixture。"""

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
    return Settings(max_limit=1000, catalog_version="test")


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
    """不含 Fake Provider 的默认应用（仅五个内置 Provider，全部禁用）。"""
    return TestClient(create_app())


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
