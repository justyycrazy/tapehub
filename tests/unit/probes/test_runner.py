"""探针执行器测试（离线：Fake Provider 与注入的假 SDK 模块）。"""

import asyncio
import sys
import types
from pathlib import Path

import pytest
from probes.runner import MissingEnvError, _interpolate, run_provider_probe
from probes.schema import ProbeFile

from tapehub.providers.manifest import load_manifest_file
from tapehub.providers.registry import ProviderRegistry, build_provider_record
from tapehub.settings import Settings

FIXTURES = Path(__file__).parents[2] / "fixtures"


def _manifest() -> object:
    return load_manifest_file(FIXTURES / "manifests" / "fake_provider.yaml")


def _registry() -> ProviderRegistry:
    manifest = load_manifest_file(FIXTURES / "manifests" / "fake_provider.yaml")
    from tests.fixtures.providers.fake_provider import FakeBarProvider

    return ProviderRegistry(
        [build_provider_record("fake-provider", manifest, FakeBarProvider)]
    )


def _probe(**chain: object) -> ProbeFile:
    data = {
        "probe": "fake-provider",
        "version": 1,
        "targets": {
            "stock": {
                "instrument": "CN-SSE-600000",
                "window": {"start": "2025-01-01", "end": "2025-03-31"},
                "min_bars": 10,
            }
        },
        "chain": {"include_declared": True, **chain},
    }
    return ProbeFile.model_validate(data)


def _settings() -> Settings:
    return Settings(max_limit=1000, catalog_version="test")


def test_availability_confirmed() -> None:
    result = asyncio.run(
        run_provider_probe(_probe(), _registry(), _settings())
    )
    confirmed = [c for c in result.cases if c.verdict == "confirmed"]
    assert {c.case_id for c in confirmed} == {
        "stock-P1D-none-eastmoney",
        "stock-PT1M-none-eastmoney",
    }
    obs = confirmed[0].observation
    assert obs["count"] > 0
    assert obs["first_timestamp"] <= obs["last_timestamp"]
    assert result.call_budget["used"] >= 2


def test_min_bars_failure() -> None:
    # min_bars=10 应通过；失败路径用远超窗口 bar 数的期望验证
    data = _probe().model_dump()
    data["targets"]["stock"]["min_bars"] = 10_000_000
    result = asyncio.run(
        run_provider_probe(ProbeFile.model_validate(data), _registry(), _settings())
    )
    failed = [c for c in result.cases if c.verdict == "failed"]
    assert failed and all("min_bars" in c.reason for c in failed)


def test_dry_run_makes_no_calls() -> None:
    result = asyncio.run(
        run_provider_probe(_probe(), _registry(), _settings(), execute=False)
    )
    assert result.call_budget["used"] == 0
    assert all(c.verdict == "skipped" and "dry-run" in c.reason for c in result.cases)


def test_missing_requirements_skip_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAPEHUB_PROBE_TEST_TOKEN", raising=False)
    probe = ProbeFile.model_validate(
        _probe().model_dump()
        | {"requires": {"env": ["TAPEHUB_PROBE_TEST_TOKEN"]}}
    )
    result = asyncio.run(run_provider_probe(probe, _registry(), _settings()))
    assert result.file_skip_reason is not None
    assert "TAPEHUB_PROBE_TEST_TOKEN" in result.file_skip_reason
    assert result.cases and all(c.verdict == "skipped" for c in result.cases)


def test_budget_exhaustion_marks_remaining_skipped() -> None:
    data = _probe().model_dump()
    data["pacing"]["max_calls"] = 1
    result = asyncio.run(
        run_provider_probe(ProbeFile.model_validate(data), _registry(), _settings())
    )
    verdicts = [c.verdict for c in result.cases]
    assert "skipped" in verdicts
    budget_skipped = [
        c for c in result.cases if c.verdict == "skipped" and "budget" in c.reason
    ]
    assert budget_skipped


def test_interpolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAPEHUB_PROBE_TEST_TOKEN", "token-value")
    assert _interpolate("$TAPEHUB_PROBE_TEST_TOKEN") == "token-value"
    assert _interpolate({"k": ["$TAPEHUB_PROBE_TEST_TOKEN", 1]}) == {"k": ["token-value", 1]}
    with pytest.raises(MissingEnvError):
        _interpolate("$TAPEHUB_PROBE_MISSING_VAR")


def test_raw_runner_with_fake_module(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.ModuleType("probe_fake_sdk")
    fake.fetch_rows = lambda **kw: [{"close": 1.0}, {"close": 2.0}, {"close": 3.0}, {"close": 4.0}]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "probe_fake_sdk", fake)

    probe = ProbeFile.model_validate(
        _probe().model_dump()
        | {
            "raw": [
                {
                    "id": "fake-ok",
                    "module": "probe_fake_sdk",
                    "call": [{"module_func": "fetch_rows", "kwargs": {"n": 1}}],
                    "expect": {"status": "ok", "min_rows": 3},
                },
                {
                    "id": "fake-too-strict",
                    "module": "probe_fake_sdk",
                    "call": [{"module_func": "fetch_rows"}],
                    "expect": {"status": "ok", "min_rows": 100},
                },
                {
                    "id": "fake-observe",
                    "module": "probe_fake_sdk",
                    "call": [{"module_func": "fetch_rows"}],
                    "expect": {"status": "ok", "min_rows": 100},
                    "assert": False,
                },
            ]
        }
    )
    result = asyncio.run(run_provider_probe(probe, _registry(), _settings()))
    by_id = {c.case_id: c for c in result.cases if c.kind == "raw"}
    assert by_id["fake-ok"].verdict == "confirmed"
    assert by_id["fake-ok"].observation["rows"] == 4
    assert by_id["fake-too-strict"].verdict == "failed"
    assert by_id["fake-observe"].verdict == "observe"


def test_raw_runner_error_expectation(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.ModuleType("probe_fake_sdk_err")

    def boom(**kw: object) -> object:
        raise RuntimeError("upstream rate limited: too many requests")

    fake.fetch_rows = boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "probe_fake_sdk_err", fake)

    probe = ProbeFile.model_validate(
        _probe().model_dump()
        | {
            "raw": [
                {
                    "id": "err-matched",
                    "module": "probe_fake_sdk_err",
                    "call": [{"module_func": "fetch_rows"}],
                    "expect": {"status": "error", "error_contains": "rate limited"},
                },
                {
                    "id": "err-not-matched",
                    "module": "probe_fake_sdk_err",
                    "call": [{"module_func": "fetch_rows"}],
                    "expect": {"status": "error", "error_contains": "quota"},
                },
            ]
        }
    )
    result = asyncio.run(run_provider_probe(probe, _registry(), _settings()))
    by_id = {c.case_id: c for c in result.cases if c.kind == "raw"}
    assert by_id["err-matched"].verdict == "confirmed"
    assert by_id["err-not-matched"].verdict == "failed"
    assert "rate limited" in by_id["err-matched"].observation["error"]["text"]


def test_raw_missing_module_skipped() -> None:
    probe = ProbeFile.model_validate(
        _probe().model_dump()
        | {
            "raw": [
                {
                    "id": "ghost",
                    "module": "no_such_sdk_module_xyz",
                    "call": [{"module_func": "fetch"}],
                    "expect": {"status": "ok", "min_rows": 1},
                }
            ]
        }
    )
    result = asyncio.run(run_provider_probe(probe, _registry(), _settings()))
    (case,) = [c for c in result.cases if c.kind == "raw"]
    assert case.verdict == "skipped" and "not installed" in case.reason
