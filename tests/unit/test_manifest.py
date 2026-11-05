"""manifest 加载与校验测试。"""

from datetime import datetime

import pytest

from tapehub.domain.errors import ManifestError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import AssetType, DataType
from tapehub.providers.manifest import load_manifest_file, manifest_from_dict
from tests.conftest import FIXTURES_DIR


def test_load_fake_manifest() -> None:
    manifest = load_manifest_file(FIXTURES_DIR / "manifests" / "fake_provider.yaml")
    assert manifest.provider == "fake-provider"
    assert manifest.enabled is True
    assert len(manifest.capabilities) == 1
    assert len(manifest.operations) == 5

    capability = manifest.to_domain_capabilities()[0]
    assert capability.data_type == DataType.BARS
    assert capability.intervals[0].code == Interval.parse("P1D")
    assert capability.intervals[0].history.start is None
    assert capability.intervals[0].history.end is None
    assert AssetType.STOCK in capability.asset_types


def test_invalid_interval_rejected_at_load() -> None:
    with pytest.raises(ManifestError):
        load_manifest_file(FIXTURES_DIR / "manifests" / "bad_interval.yaml")


def test_invalid_interval_in_operations_rejected() -> None:
    data = {
        "provider": "broken",
        "enabled": True,
        "capabilities": [],
        "operations": [
            {
                "match": {
                    "data_type": "bars",
                    "market": "CN",
                    "asset_type": "stock",
                    "interval": "tick",
                    "adjust": "none",
                    "variant": "eastmoney",
                },
                "operation": "whatever",
            }
        ],
    }
    with pytest.raises(ManifestError):
        manifest_from_dict(data)


def test_duplicate_operation_match_rejected() -> None:
    match = {
        "data_type": "bars",
        "market": "CN",
        "asset_type": "stock",
        "interval": "P1D",
        "adjust": "none",
        "variant": "eastmoney",
    }
    data = {
        "provider": "broken",
        "enabled": True,
        "capabilities": [],
        "operations": [
            {"match": match, "operation": "op_a"},
            {"match": match, "operation": "op_b"},
        ],
    }
    with pytest.raises(ManifestError):
        manifest_from_dict(data)


def test_pt1m_and_p1m_are_different_operations() -> None:
    """PT1M 与 P1M 是不同周期，必须映射到不同 operation 才能同时声明。"""
    data = {
        "provider": "two-intervals",
        "enabled": True,
        "capabilities": [],
        "operations": [
            {
                "match": {
                    "data_type": "bars",
                    "market": "CN",
                    "asset_type": "stock",
                    "interval": "PT1M",
                    "adjust": "none",
                    "variant": "eastmoney",
                },
                "operation": "minute_op",
            },
            {
                "match": {
                    "data_type": "bars",
                    "market": "CN",
                    "asset_type": "stock",
                    "interval": "P1M",
                    "adjust": "none",
                    "variant": "eastmoney",
                },
                "operation": "month_op",
            },
        ],
    }
    manifest = manifest_from_dict(data)
    assert len(manifest.operations) == 2


def _capability_data(variants: list[str], asset_types: list[str]) -> dict:
    return {
        "data_type": "bars",
        "markets": ["CN"],
        "asset_types": asset_types,
        "delivery": ["rest"],
        "variants": variants,
        "intervals": [{"code": "P1D", "history": {"start": None, "end": None}}],
        "adjustments": ["none"],
        "fields": ["open", "high", "low", "close"],
    }


def test_default_variant_scalar_covers_all_asset_types() -> None:
    """标量 default_variant 作用于全部资产类型，并归一化为映射。"""
    data = {
        "provider": "single-source",
        "enabled": True,
        "default_variant": "tqsdk",
        "default_provider": ["contract", "continuous"],
        "capabilities": [_capability_data(["tqsdk"], ["contract", "continuous"])],
        "operations": [],
    }
    manifest = manifest_from_dict(data)
    resolved = manifest.resolved_default_variants()
    assert resolved == {AssetType.CONTRACT: "tqsdk", AssetType.CONTINUOUS: "tqsdk"}


def test_default_variant_mapping_resolved() -> None:
    """映射 default_variant 按资产类型覆盖，与声明顺序无关。"""
    data = {
        "provider": "multi-source",
        "enabled": True,
        "default_variant": {"stock": "eastmoney", "index": "sina"},
        "default_provider": ["stock", "index"],
        "capabilities": [_capability_data(["eastmoney", "sina", "tencent"], ["stock", "index"])],
        "operations": [],
    }
    manifest = manifest_from_dict(data)
    assert manifest.resolved_default_variants() == {
        AssetType.STOCK: "eastmoney",
        AssetType.INDEX: "sina",
    }


def test_default_variant_required_when_capabilities_exist() -> None:
    """有 capabilities 但未声明 default_variant：加载失败，不做隐式推断。"""
    data = {
        "provider": "no-default",
        "enabled": True,
        "capabilities": [_capability_data(["eastmoney"], ["stock"])],
        "operations": [],
    }
    with pytest.raises(ManifestError):
        manifest_from_dict(data)


def test_default_variant_mapping_missing_asset_type_rejected() -> None:
    """映射未覆盖 capabilities 中出现的资产类型：加载失败。"""
    data = {
        "provider": "partial-map",
        "enabled": True,
        "default_variant": {"stock": "eastmoney"},
        "capabilities": [_capability_data(["eastmoney", "sina"], ["stock", "index"])],
        "operations": [],
    }
    with pytest.raises(ManifestError):
        manifest_from_dict(data)


def test_default_variant_not_among_declared_variants_rejected() -> None:
    """default_variant 不在该资产类型声明的 variants 中：加载失败，不编造。"""
    data = {
        "provider": "bad-default",
        "enabled": True,
        "default_variant": {"stock": "binance"},
        "capabilities": [_capability_data(["eastmoney", "sina"], ["stock"])],
        "operations": [],
    }
    with pytest.raises(ManifestError):
        manifest_from_dict(data)


def test_default_variant_forbidden_without_capabilities() -> None:
    """无 capabilities 的空壳 manifest 不允许声明 default_variant。"""
    data = {
        "provider": "empty-shell",
        "enabled": False,
        "default_variant": "eastmoney",
        "capabilities": [],
        "operations": [],
    }
    with pytest.raises(ManifestError):
        manifest_from_dict(data)


def test_default_provider_required_when_capabilities_exist() -> None:
    """有 capabilities 但未声明 default_provider：加载失败，不允许隐式缺省。"""
    data = {
        "provider": "no-default-provider",
        "enabled": True,
        "default_variant": "eastmoney",
        "capabilities": [_capability_data(["eastmoney"], ["stock"])],
        "operations": [],
    }
    with pytest.raises(ManifestError):
        manifest_from_dict(data)


def test_default_provider_empty_list_opts_out() -> None:
    """default_provider: [] 表示对任何资产类型都不是缺省，合法。"""
    data = {
        "provider": "opt-out",
        "enabled": True,
        "default_variant": "eastmoney",
        "default_provider": [],
        "capabilities": [_capability_data(["eastmoney"], ["stock"])],
        "operations": [],
    }
    manifest = manifest_from_dict(data)
    assert manifest.default_provider == []


def test_default_provider_claims_uncovered_asset_type_rejected() -> None:
    """声明未持有能力的资产类型：加载失败，不编造。"""
    data = {
        "provider": "over-claim",
        "enabled": True,
        "default_variant": "eastmoney",
        "default_provider": ["stock", "forex"],
        "capabilities": [_capability_data(["eastmoney"], ["stock"])],
        "operations": [],
    }
    with pytest.raises(ManifestError):
        manifest_from_dict(data)


def test_default_provider_declared_without_capabilities_rejected() -> None:
    """无 capabilities 的空壳 manifest 不允许声明 default_provider。"""
    data = {
        "provider": "empty-shell",
        "enabled": False,
        "default_provider": ["stock"],
        "capabilities": [],
        "operations": [],
    }
    with pytest.raises(ManifestError):
        manifest_from_dict(data)


def test_retention_days_declaration_roundtrip() -> None:
    """retention_days（滚动保留天数）声明加载与领域透传。"""
    cap = _capability_data(["eastmoney"], ["stock"])
    cap["intervals"] = [
        {"code": "PT1M", "history": {"retention_days": 35, "end": None}},
        {"code": "P1D", "history": {"start": "2018-01-01T16:00:00Z", "end": None}},
    ]
    data = {
        "provider": "rolling",
        "enabled": True,
        "default_variant": "eastmoney",
        "default_provider": ["stock"],
        "capabilities": [cap],
        "operations": [],
    }
    manifest = manifest_from_dict(data)
    entry = manifest.to_domain_capabilities()[0].intervals
    assert entry[0].history.retention_days == 35
    assert entry[0].history.start is None
    assert entry[1].history.start == datetime.fromisoformat("2018-01-01T16:00:00+00:00")
    assert entry[1].history.retention_days is None


def test_start_and_retention_days_mutually_exclusive() -> None:
    """start 与 retention_days 同时声明：加载失败（语义互斥）。"""
    cap = _capability_data(["eastmoney"], ["stock"])
    cap["intervals"] = [
        {
            "code": "PT1M",
            "history": {"start": "2025-01-01T00:00:00Z", "retention_days": 35, "end": None},
        }
    ]
    data = {
        "provider": "both",
        "enabled": True,
        "default_variant": "eastmoney",
        "default_provider": ["stock"],
        "capabilities": [cap],
        "operations": [],
    }
    with pytest.raises(ManifestError):
        manifest_from_dict(data)


def test_quote_operation_with_null_dimensions() -> None:
    """quotes 快照操作：无意义维度（周期/复权）显式 null，加载与查重正常。"""
    data = {
        "provider": "quote-only",
        "enabled": True,
        "default_variant": "tqsdk",
        "default_provider": ["contract"],
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
    manifest = manifest_from_dict(data)
    op = manifest.operations[0]
    assert op.match.interval is None
    assert op.match.adjust is None
    assert op.operation == "get_quote"
    # quotes 能力维度为空：to_domain 后同样为空列表
    cap = manifest.to_domain_capabilities()[0]
    assert cap.intervals == []
    assert cap.adjustments == []
    # 相同 null 维度的重复 match 仍被查重拒绝
    duplicate = {**data, "operations": data["operations"] * 2}
    with pytest.raises(Exception, match="duplicate operation"):
        manifest_from_dict(duplicate)
