"""manifest 加载与校验测试。"""

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
