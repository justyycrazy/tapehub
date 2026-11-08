"""公共 ProviderRouter 与适配器内部 operation 路由测试。"""

from datetime import datetime
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
from tapehub.domain.models import (
    CN_TIMEZONE,
    Adjust,
    AssetType,
    BarQuery,
    DataType,
    DataVariant,
    Instrument,
)
from tapehub.providers.base import BaseProviderAdapter
from tapehub.providers.manifest import ProviderManifest
from tapehub.providers.registry import ProviderRegistry, build_provider_record
from tapehub.providers.routing import RoutingTable
from tests.conftest import build_fake_record, single_provider_routing


class _YesAdapter(BaseProviderAdapter):
    """已实现但能力为空的适配器，用于能力不匹配分支。"""

    implemented = True


def _query(**overrides: Any) -> BarQuery:
    base: dict[str, Any] = {
        "asset_type": AssetType.STOCK,
        "instrument": Instrument.parse("CN-SSE-600000"),
        "interval": Interval.parse("P1D"),
        "start": datetime(2026, 1, 1, tzinfo=CN_TIMEZONE),
        "end": datetime(2026, 1, 31, tzinfo=CN_TIMEZONE),
        "adjust": Adjust.NONE,
        # 适配器内部 operation 路由需要具体变体；缺省（None）解析行为另行测试
        "variant": DataVariant.EASTMONEY,
        "offset": 0,
        "limit": 10,
    }
    base.update(overrides)
    return BarQuery(**base)


@pytest.fixture()
def fake_record() -> Any:
    return build_fake_record()


def _registry(*records: Any, routing: RoutingTable | None = None) -> ProviderRegistry:
    # 单记录缺省独占其声明组合；多记录必须显式给出回退链（与生产一致）
    if routing is None:
        assert len(records) == 1, "multi-record registries need an explicit routing table"
        routing = single_provider_routing(records[0])
    return ProviderRegistry(list(records), routing)


class TestSelectExplicit:
    def test_found(self, fake_record: Any) -> None:
        router = ProviderRouter(_registry(fake_record))
        selection = router.select(_query(provider="fake-provider"))
        assert selection.record is fake_record
        assert selection.variant is DataVariant.EASTMONEY

    def test_unknown_provider(self, fake_record: Any) -> None:
        router = ProviderRouter(_registry(fake_record))
        with pytest.raises(ProviderNotFoundError):
            router.select(_query(provider="nope"))

    def test_disabled_provider(self) -> None:
        manifest = ProviderManifest(provider="stub-provider")
        record = build_provider_record("stub-provider", manifest, _YesAdapter, enabled=False)
        router = ProviderRouter(_registry(record))
        with pytest.raises(ProviderDisabledError):
            router.select(_query(provider="stub-provider"))

    def test_not_implemented_provider(self) -> None:
        manifest = ProviderManifest(provider="stub-provider")
        record = build_provider_record("stub-provider", manifest, _YesAdapter)
        record.implemented = False
        router = ProviderRouter(_registry(record))
        with pytest.raises(ProviderNotImplementedError):
            router.select(_query(provider="stub-provider"))

    def test_unsupported_capability(self) -> None:
        manifest = ProviderManifest(provider="empty-provider")
        record = build_provider_record("empty-provider", manifest, _YesAdapter)
        router = ProviderRouter(_registry(record))
        with pytest.raises(UnsupportedCapabilityError):
            router.select(_query(provider="empty-provider"))


class TestSelectAuto:
    def test_selects_only_full_match(self, fake_record: Any) -> None:
        router = ProviderRouter(_registry(fake_record))
        selection = router.select(_query(provider="auto"))
        assert selection.record is fake_record
        assert selection.variant is DataVariant.EASTMONEY

    def test_variant_must_match(self, fake_record: Any) -> None:
        router = ProviderRouter(_registry(fake_record))
        # fake 仅声明 eastmoney 变体
        selection = router.select(_query(variant="eastmoney"))
        assert selection.record is fake_record
        with pytest.raises(NoProviderAvailableError):
            router.select(_query(variant="sina"))

    def test_missing_variant_resolves_declared_default(self, fake_record: Any) -> None:
        """缺省 variant：按 manifest 声明的 default_variant（eastmoney）具体化。"""
        router = ProviderRouter(_registry(fake_record))
        selection = router.select(_query(variant=None))
        assert selection.record is fake_record
        assert selection.variant is DataVariant.EASTMONEY

    def test_skips_disabled_and_unimplemented(self) -> None:
        manifest = ProviderManifest(provider="stub-provider")
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
        assert router.select(_query(interval="PT1M")).record is fake_record
        # P1M（月）不在 fake 能力目录中，不能与 PT1M（分钟）混淆
        with pytest.raises(NoProviderAvailableError):
            router.select(_query(interval="P1M"))

    def test_asset_type_must_match(self, fake_record: Any) -> None:
        # 通过直接构造能力检查验证 asset_type 与 variant 维度参与匹配
        assert fake_record.capabilities[0].supports(
            data_type=DataType.BARS,
            market="CN",
            asset_type=AssetType.ETF,
            interval=Interval.parse("P1D"),
            adjust=Adjust.NONE,
            variant=DataVariant.EASTMONEY,
        )
        assert not fake_record.capabilities[0].supports(
            data_type=DataType.BARS,
            market="US",
            asset_type=AssetType.ETF,
            interval=Interval.parse("P1D"),
            adjust=Adjust.NONE,
            variant=DataVariant.EASTMONEY,
        )
        assert not fake_record.capabilities[0].supports(
            data_type=DataType.BARS,
            market="CN",
            asset_type=AssetType.ETF,
            interval=Interval.parse("P1D"),
            adjust=Adjust.NONE,
            variant=DataVariant.SINA,
        )


class TestAdapterOperationRouting:
    def test_asset_types_resolve_to_distinct_operations(self, fake_record: Any) -> None:
        adapter = fake_record.adapter
        operations = {
            asset_type: adapter.resolve_operation(_query(asset_type=asset_type))
            for asset_type in (AssetType.STOCK, AssetType.ETF, AssetType.INDEX, AssetType.CONTRACT)
        }
        assert len(set(operations.values())) == 4
        assert operations == {
            AssetType.STOCK: "fake_stock_daily",
            AssetType.ETF: "fake_etf_daily",
            AssetType.INDEX: "fake_index_daily",
            AssetType.CONTRACT: "fake_contract_daily",
        }

    def test_minute_operation_for_stock(self, fake_record: Any) -> None:
        adapter = fake_record.adapter
        assert adapter.resolve_operation(_query(interval="PT1M")) == "fake_stock_minute"

    def test_no_cross_asset_operation_reuse(self, fake_record: Any) -> None:
        adapter = fake_record.adapter
        # etf + PT1M：能力目录声明了 etf 与 PT1M，但没有对应内部 operation
        with pytest.raises(UnsupportedCapabilityError):
            adapter.resolve_operation(_query(asset_type=AssetType.ETF, interval="PT1M"))


class TestMissingVariantResolution:
    """缺省 variant 的具体化：default 优先，default 不覆盖其余维度时回退。"""

    @staticmethod
    def _split_manifest() -> ProviderManifest:
        """两条按变体拆分的能力：eastmoney 独有 PT1M，sina 独有 P1D。"""

        def _cap(variant: str, interval: str) -> dict[str, Any]:
            return {
                "data_type": "bars",
                "markets": ["CN"],
                "asset_types": ["stock"],
                "delivery": ["rest"],
                "variants": [variant],
                "intervals": [{"code": interval}],
                "adjustments": ["none"],
                "fields": ["open", "close"],
            }

        return ProviderManifest(
            provider="split-source",
            default_variant={"stock": "sina"},
            capabilities=[_cap("eastmoney", "PT1M"), _cap("sina", "P1D")],  # type: ignore[arg-type]
        )

    def test_default_variant_resolved_when_it_matches(self) -> None:
        manifest = self._split_manifest()
        record = build_provider_record("split-source", manifest, _YesAdapter)
        router = ProviderRouter(_registry(record))
        selection = router.select(_query(variant=None))  # P1D → default sina
        assert selection.record is record
        assert selection.variant is DataVariant.SINA

    def test_fallback_to_first_matched_when_default_not_covered(self) -> None:
        """default（sina）不覆盖 PT1M：回退到声明顺序第一条匹配能力的第一个变体。"""
        manifest = self._split_manifest()
        record = build_provider_record("split-source", manifest, _YesAdapter)
        router = ProviderRouter(_registry(record))
        selection = router.select(_query(variant=None, interval="PT1M"))
        assert selection.record is record
        assert selection.variant is DataVariant.EASTMONEY


class TestRoutingPrecedence:
    """缺省路由配置（完整回退链）：链序生效、强制显式校验拒绝启动。"""

    @staticmethod
    def _record(name: str) -> Any:
        cap = {
            "data_type": "bars",
            "markets": ["CN"],
            "asset_types": ["stock"],
            "delivery": ["rest"],
            "variants": ["eastmoney"],
            "intervals": [{"code": "P1D"}],
            "adjustments": ["none"],
            "fields": ["open", "close"],
        }
        manifest = ProviderManifest(
            provider=name,
            default_variant="eastmoney",
            capabilities=[cap],  # type: ignore[arg-type]
        )
        return build_provider_record(name, manifest, _YesAdapter)

    def test_routing_order_ranks_before_registry_order(self) -> None:
        """注册顺序在后的 Provider，可经路由链排到缺省位。"""
        alpha = self._record("alpha-provider")
        zeta = self._record("zeta-provider")
        routing = {(DataType.BARS, "CN", AssetType.STOCK): ("zeta-provider", "alpha-provider")}
        router = ProviderRouter(ProviderRegistry([alpha, zeta], routing))
        selection = router.select(_query(variant=None))
        assert selection.record.name == "zeta-provider"
        # 显式指定不受路由链影响
        explicit = router.select(_query(provider="alpha-provider"))
        assert explicit.record.name == "alpha-provider"

    def test_disabled_provider_skipped_and_next_in_chain_used(self) -> None:
        """链首未启用（凭据缺失等）时 auto 路由跳过，落到链上下一个。"""
        first = self._record("first-provider")
        second = self._record("second-provider")
        first.enabled = False
        routing = {(DataType.BARS, "CN", AssetType.STOCK): ("first-provider", "second-provider")}
        router = ProviderRouter(ProviderRegistry([first, second], routing))
        selection = router.select(_query(variant=None))
        assert selection.record.name == "second-provider"

    def test_capability_mismatch_falls_back_along_chain(self) -> None:
        """链首不支持请求的具体维度（周期）时，回退到链上下一个。"""
        minute_cap = {
            "data_type": "bars",
            "markets": ["CN"],
            "asset_types": ["stock"],
            "delivery": ["rest"],
            "variants": ["eastmoney"],
            "intervals": [{"code": "PT1M"}],
            "adjustments": ["none"],
            "fields": ["open", "close"],
        }
        manifest = ProviderManifest(
            provider="minute-provider",
            default_variant="eastmoney",
            capabilities=[minute_cap],  # type: ignore[arg-type]
        )
        daily = self._record("daily-provider")
        minute = build_provider_record("minute-provider", manifest, _YesAdapter)
        routing = {(DataType.BARS, "CN", AssetType.STOCK): ("daily-provider", "minute-provider")}
        router = ProviderRouter(ProviderRegistry([daily, minute], routing))
        # P1D：daily 支持 → 命中链首；PT1M：daily 不支持 → 回退 minute
        assert router.select(_query(variant=None)).record.name == "daily-provider"
        assert router.select(_query(variant=None, interval="PT1M")).record.name == "minute-provider"

    def test_missing_routing_entry_rejected(self) -> None:
        """能力覆盖的组合缺路由条目：注册表构建失败（强制显式）。"""
        solo = self._record("solo-provider")
        with pytest.raises(ValueError, match="missing entries"):
            ProviderRegistry([solo], {})

    def test_unknown_provider_in_order_rejected(self) -> None:
        """order 引用未注册 Provider：注册表构建失败。"""
        solo = self._record("solo-provider")
        routing = {(DataType.BARS, "CN", AssetType.STOCK): ("ghost-provider",)}
        with pytest.raises(ValueError, match="unknown provider"):
            ProviderRegistry([solo], routing)

    def test_order_provider_without_capability_rejected(self) -> None:
        """order 引用未声明该组合能力的 Provider：注册表构建失败。"""
        bars_record = self._record("bars-provider")  # 仅 bars/CN/stock
        quote_cap = {
            "data_type": "quotes",
            "markets": ["CN"],
            "asset_types": ["stock"],
            "delivery": ["rest"],
            "variants": ["eastmoney"],
            "intervals": [],
            "adjustments": [],
            "fields": ["timestamp", "last_price"],
        }
        manifest = ProviderManifest(
            provider="quote-provider",
            default_variant="eastmoney",
            capabilities=[quote_cap],  # type: ignore[arg-type]
        )
        quote_record = build_provider_record("quote-provider", manifest, _YesAdapter)
        routing = {
            (DataType.BARS, "CN", AssetType.STOCK): ("quote-provider",),
            (DataType.QUOTE, "CN", AssetType.STOCK): ("quote-provider",),
        }
        with pytest.raises(ValueError, match="does not declare this capability"):
            ProviderRegistry([bars_record, quote_record], routing)

    def test_extra_routing_entry_rejected(self) -> None:
        """路由条目声明了无 Provider 覆盖的组合：注册表构建失败（防拼写漂移）。"""
        solo = self._record("solo-provider")
        routing = {
            (DataType.BARS, "CN", AssetType.STOCK): ("solo-provider",),
            (DataType.BARS, "US", AssetType.STOCK): ("solo-provider",),
        }
        with pytest.raises(ValueError, match="not covered by any provider"):
            ProviderRegistry([solo], routing)


# ---------------------------------------------------------------------------
# 实时快照路由（select_quote，data_type=quotes，无 interval/adjust 维度）
# ---------------------------------------------------------------------------


def _quote_manifest_data() -> dict[str, Any]:
    return {
        "provider": "quote-source",
        "default_variant": "tqsdk",
        "capabilities": [
            {
                "data_type": "quotes",
                "markets": ["CN"],
                "asset_types": ["contract"],
                "delivery": ["rest"],
                "variants": ["tqsdk"],
                "intervals": [],
                "adjustments": [],
                "fields": ["timestamp", "last_price"],
            }
        ],
        "operations": [
            {
                "match": {
                    "data_type": "quotes",
                    "market": "CN",
                    "asset_type": "contract",
                    "interval": None,
                    "adjust": None,
                    "variant": "tqsdk",
                },
                "operation": "get_quote",
            }
        ],
    }


def _quote_query(**overrides: Any):
    from tapehub.domain.models import QuoteQuery

    base: dict[str, Any] = {
        "asset_type": AssetType.CONTRACT,
        "instrument": Instrument.parse("CN-SHFE-AU2612"),
        "variant": DataVariant.TQSDK,
    }
    base.update(overrides)
    return QuoteQuery(**base)


def _quote_record(name: str = "quote-source") -> Any:
    from tapehub.providers.manifest import manifest_from_dict

    manifest = manifest_from_dict(_quote_manifest_data())
    manifest_data = manifest.model_dump()
    manifest_data["provider"] = name
    manifest = manifest_from_dict(manifest_data)
    return build_provider_record(name, manifest, _YesAdapter)


class TestSelectQuote:
    def test_explicit_provider_resolves_default_variant(self) -> None:
        router = ProviderRouter(_registry(_quote_record()))
        selection = router.select_quote(_quote_query(provider="quote-source", variant=None))
        assert selection.record.name == "quote-source"
        assert selection.variant == DataVariant.TQSDK  # default_variant 具体化

    def test_auto_selects_quote_capability(self) -> None:
        router = ProviderRouter(_registry(_quote_record()))
        selection = router.select_quote(_quote_query())
        assert selection.record.name == "quote-source"
        assert selection.variant == DataVariant.TQSDK

    def test_bars_only_provider_not_selected_for_quote(self, fake_record: Any) -> None:
        """只有 bars 能力的 Provider 不服务 quote 查询（data_type 精确匹配）。"""
        router = ProviderRouter(_registry(fake_record))
        with pytest.raises(NoProviderAvailableError):
            router.select_quote(_quote_query())

    def test_explicit_bars_only_provider_unsupported(self, fake_record: Any) -> None:
        router = ProviderRouter(_registry(fake_record))
        with pytest.raises(UnsupportedCapabilityError):
            router.select_quote(_quote_query(provider="fake-provider"))

    def test_quote_capability_does_not_serve_bars(self) -> None:
        """quote 能力不服务 bars 查询：data_type 不同，其余维度全部一致。"""
        record = _quote_record()
        router = ProviderRouter(_registry(record))
        with pytest.raises(NoProviderAvailableError):
            router.select(_query(asset_type=AssetType.CONTRACT, variant=None))

    def test_variant_must_match(self) -> None:
        router = ProviderRouter(_registry(_quote_record()))
        with pytest.raises(UnsupportedCapabilityError):
            router.select_quote(
                _quote_query(provider="quote-source", variant=DataVariant.EASTMONEY)
            )


class TestQuoteOperationRouting:
    def test_resolve_quote_operation(self) -> None:
        record = _quote_record()
        adapter = record.adapter
        assert adapter is not None
        assert adapter.resolve_quote_operation(_quote_query()) == "get_quote"

    def test_asset_type_must_match(self) -> None:
        record = _quote_record()
        adapter = record.adapter
        assert adapter is not None
        with pytest.raises(UnsupportedCapabilityError):
            adapter.resolve_quote_operation(
                _quote_query(asset_type=AssetType.CONTINUOUS)
            )
