"""探针文件 schema 校验测试（离线）。"""

import pytest
from probes.schema import CallSpec, ProbeFile, RawCaseSpec
from pydantic import ValidationError


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
