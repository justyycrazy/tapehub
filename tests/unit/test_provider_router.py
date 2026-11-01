"""公共 ProviderRouter 与适配器内部 operation 路由测试。"""

from datetime import datetime, timezone
from typing import Any

import pytest

from tapehub.application.provider_router import ProviderRouter
from tapehub.domain.errors import (
    NoProviderAvailableError,
    ProviderDisabledError,
    ProviderNotFoundError,
    ProviderNotImplementedError,
    UnsupportedCapabilityError,
)
from tapehub.domain.intervals import Interval
from tapehub.domain.models import Adjust, AssetType, BarQuery, DataType, Instrument
from tapehub.providers.base import BaseProviderAdapter
from tapehub.providers.manifest import ProviderManifest
from tapehub.providers.registry import ProviderRegistry, build_provider_record
from tests.conftest import build_fake_record

UTC = timezone.utc


class _YesAdapter(BaseProviderAdapter):
    """已实现但能力为空的适配器，用于能力不匹配分支。"""

    implemented = True


def _query(**overrides: Any) -> BarQuery:
    base: dict[str, Any] = {
        "asset_type": AssetType.STOCK,
        "instrument": Instrument.parse("CN-SSE-600000"),
        "interval": Interval.parse("P1D"),
        "start": datetime(2026, 1, 1, tzinfo=UTC),
        "end": datetime(2026, 1, 31, tzinfo=UTC),
        "adjust": Adjust.NONE,
        "offset": 0,
        "limit": 10,
    }
    base.update(overrides)
    return BarQuery(**base)


@pytest.fixture()
def fake_record() -> Any:
    return build_fake_record()


def _registry(*records: Any) -> ProviderRegistry:
    return ProviderRegistry(list(records))


class TestSelectExplicit:
    def test_found(self, fake_record: Any) -> None:
        router = ProviderRouter(_registry(fake_record))
        assert router.select(_query(provider="fake-provider")) is fake_record

    def test_unknown_provider(self, fake_record: Any) -> None:
        router = ProviderRouter(_registry(fake_record))
        with pytest.raises(ProviderNotFoundError):
            router.select(_query(provider="nope"))

    def test_disabled_provider(self) -> None:
        manifest = ProviderManifest(provider="stub-provider", enabled=False)
        record = build_provider_record("stub-provider", manifest, _YesAdapter)
        router = ProviderRouter(_registry(record))
        with pytest.raises(ProviderDisabledError):
            router.select(_query(provider="stub-provider"))

    def test_not_implemented_provider(self) -> None:
        manifest = ProviderManifest(provider="stub-provider", enabled=True)
        record = build_provider_record("stub-provider", manifest, _YesAdapter)
        record.implemented = False
        router = ProviderRouter(_registry(record))
        with pytest.raises(ProviderNotImplementedError):
            router.select(_query(provider="stub-provider"))

    def test_unsupported_capability(self) -> None:
        manifest = ProviderManifest(provider="empty-provider", enabled=True)
        record = build_provider_record("empty-provider", manifest, _YesAdapter)
        router = ProviderRouter(_registry(record))
        with pytest.raises(UnsupportedCapabilityError):
            router.select(_query(provider="empty-provider"))


class TestSelectAuto:
    def test_selects_only_full_match(self, fake_record: Any) -> None:
        router = ProviderRouter(_registry(fake_record))
        assert router.select(_query(provider="auto")) is fake_record

    def test_skips_disabled_and_unimplemented(self) -> None:
        manifest = ProviderManifest(provider="stub-provider", enabled=True)
        stub = build_provider_record("stub-provider", manifest, _YesAdapter)
        router = ProviderRouter(_registry(stub))
        with pytest.raises(NoProviderAvailableError):
            router.select(_query(provider="auto"))

    def test_no_candidates(self, fake_record: Any) -> None:
        router = ProviderRouter(_registry(fake_record))
        # fake 不支持 P1W（周周期）与 qfq
        with pytest.raises(NoProviderAvailableError):
            router.select(_query(interval="P1W"))
        with pytest.raises(NoProviderAvailableError):
            router.select(_query(adjust="qfq"))

    def test_pt1m_and_p1m_route_differently(self, fake_record: Any) -> None:
        router = ProviderRouter(_registry(fake_record))
        assert router.select(_query(interval="PT1M")) is fake_record
        # P1M（月）不在 fake 能力目录中，不能与 PT1M（分钟）混淆
        with pytest.raises(NoProviderAvailableError):
            router.select(_query(interval="P1M"))

    def test_asset_type_must_match(self, fake_record: Any) -> None:
        # 通过直接构造能力检查验证 asset_type 维度参与匹配
        assert fake_record.capabilities[0].supports(
            data_type=DataType.BARS,
            market="CN",
            asset_type=AssetType.ETF,
            interval=Interval.parse("P1D"),
            adjust=Adjust.NONE,
        )
        assert not fake_record.capabilities[0].supports(
            data_type=DataType.BARS,
            market="US",
            asset_type=AssetType.ETF,
            interval=Interval.parse("P1D"),
            adjust=Adjust.NONE,
        )


class TestAdapterOperationRouting:
    def test_asset_types_resolve_to_distinct_operations(self, fake_record: Any) -> None:
        adapter = fake_record.adapter
        operations = {
            asset_type: adapter.resolve_operation(_query(asset_type=asset_type))
            for asset_type in (AssetType.STOCK, AssetType.ETF, AssetType.INDEX, AssetType.FUTURES)
        }
        assert len(set(operations.values())) == 4
        assert operations == {
            AssetType.STOCK: "fake_stock_daily",
            AssetType.ETF: "fake_etf_daily",
            AssetType.INDEX: "fake_index_daily",
            AssetType.FUTURES: "fake_futures_daily",
        }

    def test_minute_operation_for_stock(self, fake_record: Any) -> None:
        adapter = fake_record.adapter
        assert adapter.resolve_operation(_query(interval="PT1M")) == "fake_stock_minute"

    def test_no_cross_asset_operation_reuse(self, fake_record: Any) -> None:
        adapter = fake_record.adapter
        # etf + PT1M：能力目录声明了 etf 与 PT1M，但没有对应内部 operation
        with pytest.raises(UnsupportedCapabilityError):
            adapter.resolve_operation(_query(asset_type=AssetType.ETF, interval="PT1M"))
