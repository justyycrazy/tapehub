"""探针文件 schema 校验测试（离线）。"""

import pytest
from probes.schema import CallSpec, ProbeFile, RawCaseSpec, TargetRef
from pydantic import ValidationError

from tapehub.domain.models import AssetType


def _base() -> dict:
    return {
        "probe": "akshare",
        "version": 1,
        "targets": {
            "stock": {
                "instrument": "CN-SSE-600000",
                "window": {"start": "2025-01-01", "end": "2025-12-31"},
            }
        },
        "chain": {"include_declared": True},
    }


def test_minimal_file_parses() -> None:
    probe = ProbeFile.model_validate(_base())
    assert probe.probe == "akshare"
    assert probe.targets["stock"].instrument == "CN-SSE-600000"
    assert probe.pacing.max_calls == 120


def test_unknown_key_rejected() -> None:
    with pytest.raises(ValidationError):
        ProbeFile.model_validate(_base() | {"unknown_key": 1})


def test_wrong_version_rejected() -> None:
    with pytest.raises(ValidationError):
        ProbeFile.model_validate(_base() | {"version": 99})


def test_skip_requires_reason() -> None:
    data = _base()
    data["chain"]["skip"] = [{"case": "stock-P1D-none-eastmoney"}]
    with pytest.raises(ValidationError):
        ProbeFile.model_validate(data)


def test_chain_or_raw_required() -> None:
    data = _base()
    del data["chain"]
    with pytest.raises(ValidationError):
        ProbeFile.model_validate(data)


def test_duplicate_raw_ids_rejected() -> None:
    data = _base()
    raw = {
        "id": "same-id",
        "module": "some_sdk",
        "call": [{"module_func": "fetch"}],
        "expect": {"status": "ok", "min_rows": 1},
    }
    data["raw"] = [raw, raw]
    with pytest.raises(ValidationError):
        ProbeFile.model_validate(data)


def test_call_exactly_one_target() -> None:
    with pytest.raises(ValidationError):
        CallSpec.model_validate({"method": "a", "module_func": "b"})
    with pytest.raises(ValidationError):
        CallSpec.model_validate({})
    assert CallSpec.model_validate({"method": "a"}).method == "a"


def test_expect_consistency() -> None:
    base = {"id": "x", "module": "m", "call": [{"module_func": "f"}]}
    with pytest.raises(ValidationError):
        RawCaseSpec.model_validate(base | {"expect": {"status": "ok"}})
    with pytest.raises(ValidationError):
        RawCaseSpec.model_validate(base | {"expect": {"status": "error"}})
    case = RawCaseSpec.model_validate(
        base | {"expect": {"status": "error", "error_contains": ""}, "assert": False}
    )
    assert case.assert_ is False
    assert case.expect.error_contains == ""


# ── 交易所限定目标引用（asset_type@MARKET-EXCHANGE）──────────────────────


def test_target_ref_exchange_qualifier_parses() -> None:
    """两段限定引用解析：market/exchange 分段正确，key 回写保留交易所段。"""
    ref = TargetRef.model_validate("stock@CN-SSE")
    assert ref.asset_type is AssetType.STOCK
    assert ref.market == "CN"
    assert ref.exchange == "SSE"
    assert ref.key == "stock@CN-SSE"
    # 既有两形态不受影响
    assert TargetRef.model_validate("stock").key == "stock"
    assert TargetRef.model_validate("stock@HK").key == "stock@HK"


def test_target_ref_exchange_without_market_rejected() -> None:
    """exchange 限定必须与 market 同现（交易所是市场内的细分段）。"""
    with pytest.raises(ValidationError, match="requires a market"):
        TargetRef.model_validate({"asset_type": "stock", "exchange": "SSE"})


def test_exchange_qualified_key_instrument_mismatch_rejected() -> None:
    """交易所限定键的标的与限定段不一致：加载期即拒绝（不留到展开期）。"""
    data = _base()
    data["targets"]["stock@CN-SSE"] = {
        "instrument": "CN-SZSE-000001",  # 键限定 SSE，标的是 SZSE
        "window": {"start": "2025-01-01", "end": "2025-12-31"},
    }
    with pytest.raises(ValidationError, match="does not match qualifier"):
        ProbeFile.model_validate(data)


def test_exchange_qualified_key_consistent_instrument_accepted() -> None:
    """交易所限定键与标的限定段一致：正常通过。"""
    data = _base()
    data["targets"]["stock@CN-SZSE"] = {
        "instrument": "CN-SZSE-000001",
        "window": {"start": "2025-01-01", "end": "2025-12-31"},
    }
    probe = ProbeFile.model_validate(data)
    assert probe.targets["stock@CN-SZSE"].instrument == "CN-SZSE-000001"
