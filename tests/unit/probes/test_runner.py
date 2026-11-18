"""探针执行器测试（离线：Fake Provider 与注入的假 SDK 模块）。"""

import asyncio
import logging
import sys
import types
from pathlib import Path

import pytest
from probes.runner import MissingEnvError, _interpolate, run_provider_probe
from probes.schema import ProbeFile

from tapehub.domain.errors import UpstreamError
from tapehub.providers.manifest import load_manifest_file
from tapehub.providers.registry import ProviderRegistry, build_provider_record
from tapehub.settings import Settings
from tests.conftest import make_settings, single_provider_routing

FIXTURES = Path(__file__).parents[2] / "fixtures"


def _manifest() -> object:
    return load_manifest_file(FIXTURES / "manifests" / "fake_provider.yaml")


def _registry() -> ProviderRegistry:
    manifest = load_manifest_file(FIXTURES / "manifests" / "fake_provider.yaml")
    from tests.fixtures.providers.fake_provider import FakeBarProvider

    record = build_provider_record("fake-provider", manifest, FakeBarProvider)
    return ProviderRegistry([record], single_provider_routing(record))


def _probe(**chain: object) -> ProbeFile:
    data = {
        "probe": "fake-provider",
        "version": 2,
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
    return make_settings(max_limit=1000, catalog_version="test")


def test_availability_confirmed() -> None:
    result = asyncio.run(
        run_provider_probe(_probe(), _registry(), _settings())
    )
    confirmed = [c for c in result.cases if c.verdict == "confirmed"]
    assert {c.case_id for c in confirmed} == {
        "stock-P1D-none-eastmoney",
        "stock-PT1M-none-eastmoney",
        # quotes 声明随 include_declared 自动展开（fake manifest 新增 quotes 能力）
        "quote-stock-eastmoney",
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


class _FakeDataFrame:
    """最小 DataFrame 形状桩（不引入 pandas）：shape[0] 为行数。"""

    def __init__(self, rows: int) -> None:
        self.shape = (rows, 15)

    def __repr__(self) -> str:
        return f"<FakeDataFrame rows={self.shape[0]}>"


def test_raw_runner_counts_dataframe_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """tushare 等上游全系返回 pandas DataFrame：raw 判定须按 shape 识别行数。"""
    fake = types.ModuleType("probe_fake_sdk_df")
    fake.fetch_table = lambda **kw: _FakeDataFrame(22)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "probe_fake_sdk_df", fake)

    probe = ProbeFile.model_validate(
        _probe().model_dump()
        | {
            "raw": [
                {
                    "id": "fake-df-ok",
                    "module": "probe_fake_sdk_df",
                    "call": [{"module_func": "fetch_table"}],
                    "expect": {"status": "ok", "min_rows": 1},
                },
                {
                    "id": "fake-df-too-strict",
                    "module": "probe_fake_sdk_df",
                    "call": [{"module_func": "fetch_table"}],
                    "expect": {"status": "ok", "min_rows": 100},
                },
            ]
        }
    )
    result = asyncio.run(run_provider_probe(probe, _registry(), _settings()))
    by_id = {c.case_id: c for c in result.cases if c.kind == "raw"}
    assert by_id["fake-df-ok"].verdict == "confirmed"
    assert by_id["fake-df-ok"].observation["rows"] == 22
    assert by_id["fake-df-too-strict"].verdict == "failed"


def test_raw_runner_closes_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """声明 close 的客户端型 raw case：成功/失败路径都释放客户端，
    且 close 不消耗调用预算（与 connection 探针同款「显式释放」语义）。"""

    class _Client:
        instances: list["_Client"] = []

        def __init__(self) -> None:
            self.closed = False
            _Client.instances.append(self)

        def fetch(self, **kw: object) -> list[dict[str, float]]:
            if kw.get("boom"):
                raise RuntimeError("upstream boom")
            return [{"close": 1.0}, {"close": 2.0}]

        def close(self) -> None:
            self.closed = True

    fake = types.ModuleType("probe_fake_sdk_client")
    fake.Client = _Client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "probe_fake_sdk_client", fake)
    _Client.instances.clear()

    probe = ProbeFile.model_validate(
        _probe().model_dump()
        | {
            "raw": [
                {
                    "id": "client-ok",
                    "module": "probe_fake_sdk_client",
                    "client": {"attr": "Client", "kwargs": {}},
                    "call": [{"method": "fetch"}],
                    "close": {"method": "close"},
                    "expect": {"status": "ok", "min_rows": 1},
                },
                {
                    "id": "client-fail",
                    "module": "probe_fake_sdk_client",
                    "client": {"attr": "Client", "kwargs": {}},
                    "call": [{"method": "fetch", "kwargs": {"boom": True}}],
                    "close": {"method": "close"},
                    "expect": {"status": "ok", "min_rows": 1},
                },
            ]
        }
    )
    result = asyncio.run(run_provider_probe(probe, _registry(), _settings()))
    by_id = {c.case_id: c for c in result.cases if c.kind == "raw"}
    ok = by_id["client-ok"]
    fail = by_id["client-fail"]
    assert ok.verdict == "confirmed"
    assert ok.observation["closed"] is True
    # 构造(1) + 调用(1)；close 是本地释放动作，不计预算
    assert ok.observation["calls_made"] == 2
    assert fail.verdict == "failed"
    assert fail.observation["closed"] is True  # 调用失败仍释放
    assert [c.closed for c in _Client.instances] == [True, True]


def test_count_rows_table_shapes() -> None:
    """_count_rows 边界：表形鸭子类型识别，非表形对象仍返回 None。"""
    from probes.runner import _count_rows

    assert _count_rows(_FakeDataFrame(22)) == 22  # DataFrame 形（rows, cols）
    assert _count_rows(type("S", (), {"shape": (22,)})()) == 22  # Series 形（rows,）
    assert _count_rows([1, 2]) == 2
    assert _count_rows({"k": 1}) == 1
    assert _count_rows(object()) is None  # 无 shape
    assert _count_rows(type("X", (), {"shape": 3})()) is None  # shape 非元组


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


def _exploding_registry() -> ProviderRegistry:
    """适配器抛未识别 SDK 异常的注册表：验证探针兜底而不是崩溃。"""
    from tapehub.providers.base import BaseProviderAdapter

    class ExplodingAdapter(BaseProviderAdapter):
        implemented = True

        async def fetch_bars(self, query: object) -> object:
            raise RuntimeError("simulated push2 rate limit")

    manifest = load_manifest_file(FIXTURES / "manifests" / "fake_provider.yaml")
    record = build_provider_record("fake-provider", manifest, ExplodingAdapter)
    return ProviderRegistry([record], single_provider_routing(record))


def test_unexpected_sdk_error_becomes_failed_case() -> None:
    probe = _probe(history_depth={"targets": ["stock"], "earliest": "2020-01-01", "max_calls": 4})
    result = asyncio.run(run_provider_probe(probe, _exploding_registry(), _settings()))
    assert result.cases, "run must complete with recorded cases"
    assert all(c.verdict == "failed" for c in result.cases if c.kind == "history_depth")
    failed = next(c for c in result.cases if c.kind == "history_depth")
    assert "simulated push2 rate limit" in failed.observation["error"]["error_text"]


def _registry_for(manifest_name: str) -> ProviderRegistry:
    from tests.fixtures.providers.fake_provider import FakeBarProvider

    manifest = load_manifest_file(FIXTURES / "manifests" / manifest_name)
    record = build_provider_record(manifest.provider, manifest, FakeBarProvider)
    return ProviderRegistry([record], single_provider_routing(record))


def _multi_var_probe(provider: str, chain_extra: dict) -> ProbeFile:
    """probe 名必须与 manifest 的 provider 名一致，否则文件级跳过。"""
    data = {
        "probe": provider,
        "version": 2,
        "targets": {
            "stock": {
                "instrument": "CN-SSE-600000",
                "window": {"start": "2025-01-01", "end": "2025-03-31"},
            }
        },
        "pacing": {"call_interval_s": 0.1},
        "chain": {"include_declared": True, **chain_extra},
    }
    return ProbeFile.model_validate(data)


def test_variant_consistency_confirmed_on_identical_series() -> None:
    """两 variant 映射同一 operation → 序列一致 → confirmed。"""
    registry = _registry_for("multi_variant_same.yaml")
    probe = _multi_var_probe(
        "multi-var-same",
        {
            "variant_consistency": {
                "targets": ["stock"],
                "window": {"start": "2025-01-01", "end": "2025-03-31"},
            }
        },
    )
    result = asyncio.run(run_provider_probe(probe, registry, _settings()))
    consistency = [c for c in result.cases if c.kind == "variant_consistency"]
    # manifest 声明 none/qfq 两种 adjust → 每个 (interval × adjust) 一条 case
    assert {c.case_id for c in consistency} == {
        "variant-consistency-stock-P1D-none",
        "variant-consistency-stock-P1D-qfq",
    }
    assert all(c.verdict == "confirmed" for c in consistency)
    comparisons = [item for c in consistency for item in c.observation["comparisons"]]
    assert comparisons
    assert all(item["identical"] is True for item in comparisons)


def test_variant_consistency_suspect_on_divergence() -> None:
    """两 variant 序列分歧 → suspect，并记录差异幅度与首个分歧时间戳。"""
    registry = _registry_for("multi_variant.yaml")
    probe = _multi_var_probe(
        "multi-var",
        {
            "adjust_diff": {
                "targets": ["stock"],
                "window": {"start": "2025-01-01", "end": "2025-03-31"},
                "pairs": [["none", "qfq"]],
            },
            "variant_consistency": {
                "targets": ["stock"],
                "window": {"start": "2025-01-01", "end": "2025-03-31"},
            },
        },
    )
    result = asyncio.run(run_provider_probe(probe, registry, _settings()))
    consistency = [c for c in result.cases if c.kind == "variant_consistency"]
    assert {c.verdict for c in consistency} == {"suspect"}
    assert all("cross-variant inconsistency" in c.reason for c in consistency)
    comparisons = [item for c in consistency for item in c.observation["comparisons"]]
    assert comparisons, "every consistency case must record pairwise comparisons"
    assert all(item["identical"] is False for item in comparisons)
    assert all(item["max_abs_diff"] > 0 for item in comparisons)
    assert all(item["first_divergence"] is not None for item in comparisons)

    # adjust_diff：同一 variant 内 none 与 qfq 由不同 operation 生成 → 有差异 → 复权生效
    diffs = [c for c in result.cases if c.kind == "adjust_diff"]
    assert len(diffs) == 2
    assert all(c.verdict == "confirmed" for c in diffs)


def test_variant_consistency_tolerance_confirms_precision_noise() -> None:
    """容差吸收表示层差异：none 差 2.0 / qfq 差 1.0，容差 1.5 → none suspect、qfq confirmed。"""
    registry = _registry_for("multi_variant.yaml")

    def _probe_with(tolerance: float) -> ProbeFile:
        return _multi_var_probe(
            "multi-var",
            {
                "variant_consistency": {
                    "targets": ["stock"],
                    "window": {"start": "2025-01-01", "end": "2025-03-31"},
                    "tolerance": tolerance,
                }
            },
        )

    strict = asyncio.run(
        run_provider_probe(_probe_with(1.5), registry, _settings())
    )
    by_id = {c.case_id: c for c in strict.cases if c.kind == "variant_consistency"}
    assert by_id["variant-consistency-stock-P1D-none"].verdict == "suspect"
    assert by_id["variant-consistency-stock-P1D-qfq"].verdict == "confirmed"
    assert "within tolerance 1.5" in by_id["variant-consistency-stock-P1D-qfq"].reason

    loose = asyncio.run(
        run_provider_probe(_probe_with(3.0), registry, _settings())
    )
    by_id = {c.case_id: c for c in loose.cases if c.kind == "variant_consistency"}
    assert all(c.verdict == "confirmed" for c in by_id.values())
    comparisons = [item for c in by_id.values() for item in c.observation["comparisons"]]
    # 差异幅度不因容差而丢失：max_abs_diff 仍如实记录
    assert max(item["max_abs_diff"] for item in comparisons) == pytest.approx(2.0)


def test_history_depth_full_mode_exact_estimate() -> None:
    """full_history_targets 命中的目标走全量模式：单次调用给出精确最早 bar。"""
    registry = _registry_for("multi_variant.yaml")
    probe = _multi_var_probe(
        "multi-var",
        {
            "history_depth": {
                "targets": ["stock"],
                "full_history_targets": ["stock"],
                "earliest": "2024-01-01",
                "max_calls": 6,
            }
        },
    )
    result = asyncio.run(run_provider_probe(probe, registry, _settings()))
    cases = [c for c in result.cases if c.kind == "history_depth"]
    # manifest 声明 eastmoney/sina 两个 variant → 全量展开为两条全量模式 case
    assert {c.case_id for c in cases} == {
        "history-depth-stock-P1D-eastmoney",
        "history-depth-stock-P1D-sina",
    }
    for case in cases:
        assert case.verdict == "observe"
        # manifest 声明 start=null → 相容性 unknown（判定不成立不报错）
        assert "declaration cross-check: unknown" in case.reason
        obs = case.observation
        assert obs["approx"] is False
        assert obs["complete"] is True
        assert obs["calls_used"] == 1
        assert obs["earliest_estimate"] is not None
        # 声明复核字段（2026-11-18 裁决：判定只看 first_bar）
        assert obs["declared_start"] is None
        assert obs["declaration_compat"] == "unknown"


def test_run_result_is_fully_json_serializable() -> None:
    """运行结果（含 adjust_diff 的 first_divergence datetime）必须整体可序列化。

    回归守卫：此前 adjust_diff 观测里的 datetime 导致 json.dumps 在落盘
    最后一步崩溃，整轮真实上游探针的证据丢失。
    """
    import json

    from probes.report import to_json_dict

    registry = _registry_for("multi_variant.yaml")
    probe = _multi_var_probe(
        "multi-var",
        {
            "adjust_diff": {
                "targets": ["stock"],
                "window": {"start": "2025-01-01", "end": "2025-03-31"},
                "pairs": [["none", "qfq"]],
            },
            "variant_consistency": {
                "targets": ["stock"],
                "window": {"start": "2025-01-01", "end": "2025-03-31"},
            },
            "history_depth": {"targets": ["stock"], "earliest": "2024-01-01", "max_calls": 2},
        },
    )
    result = asyncio.run(run_provider_probe(probe, registry, _settings()))
    payload = json.dumps(to_json_dict(result), ensure_ascii=False)
    assert '"adjust_diff"' in payload
    assert "first_divergence" in payload


def test_raw_runner_dotted_method_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """CallSpec method 支持「资源.方法」点链：逐段 getattr 后调用（单段不变）。"""

    class _FakeQuotes:
        def get(self, **kwargs: object) -> list[dict[str, object]]:
            return [{"symbol": "600000.SH", "last_price": 10.0}]

    fake = types.ModuleType("probe_fake_sdk")
    client = types.SimpleNamespace(quotes=_FakeQuotes())
    fake.TickFlow = lambda **kw: client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "probe_fake_sdk", fake)

    probe = ProbeFile.model_validate(
        _probe().model_dump()
        | {
            "raw": [
                {
                    "id": "fake-dotted",
                    "module": "probe_fake_sdk",
                    "client": {"attr": "TickFlow"},
                    "call": [{"method": "quotes.get", "kwargs": {"symbols": ["600000.SH"]}}],
                    "expect": {"status": "ok", "min_rows": 1},
                }
            ]
        }
    )
    result = asyncio.run(run_provider_probe(probe, _registry(), _settings()))
    case = next(c for c in result.cases if c.case_id == "fake-dotted")
    assert case.verdict == "confirmed"
    assert case.observation["rows"] == 1


# ---------------------------------------------------------------------------
# 子步骤日志行（每次真实上游调用一行 INFO，列式布局与 case 进度行对齐）
# ---------------------------------------------------------------------------


def _step_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "[step" in r.getMessage()]


def test_raw_step_lines_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """raw case 子步骤行：client 构造 / 每 call（序号+方法+行数）/ close 逐步可见。"""

    class _Client:
        def fetch(self, **kw: object) -> list[dict[str, float]]:
            return [{"close": 1.0}, {"close": 2.0}]

        def close(self) -> None:
            pass

    fake = types.ModuleType("probe_fake_sdk_steps")
    fake.Client = _Client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "probe_fake_sdk_steps", fake)

    probe = ProbeFile.model_validate(
        _probe().model_dump()
        | {
            "pacing": {"call_interval_s": 0.01},
            "raw": [
                {
                    "id": "steps-raw",
                    "module": "probe_fake_sdk_steps",
                    "client": {"attr": "Client", "kwargs": {}},
                    "call": [{"method": "fetch"}],
                    "close": {"method": "close"},
                    "expect": {"status": "ok", "min_rows": 1},
                }
            ],
        }
    )
    with caplog.at_level(logging.INFO, logger="probes.runner"):
        result = asyncio.run(run_provider_probe(probe, _registry(), _settings()))
    case = next(c for c in result.cases if c.case_id == "steps-raw")
    assert case.verdict == "confirmed"
    steps = _step_messages(caplog)
    # client 构造算第 1 次调用，fetch 是 call#2；kwargs 值绝不出现在日志行
    assert any("client Client → ok" in m for m in steps)
    assert any("call#2 fetch → 2 rows" in m for m in steps)
    assert any("close → ok" in m for m in steps)
    assert not any("close=1.0" in m for m in steps)  # 脱敏：结果值不进日志


def test_connection_step_lines_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """connection case 子步骤行：每实例 created/first_call 计时 + closed 计时。"""

    class _ConnClient:
        def first(self, **kw: object) -> dict[str, int]:
            return {"ok": 1}

        def close(self) -> None:
            pass

    fake = types.ModuleType("probe_fake_sdk_conn_steps")
    fake.Conn = _ConnClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "probe_fake_sdk_conn_steps", fake)

    probe = ProbeFile.model_validate(
        _probe().model_dump()
        | {
            "pacing": {"call_interval_s": 0.01},
            "connection": [
                {
                    "id": "conn-steps",
                    "module": "probe_fake_sdk_conn_steps",
                    "client": {"attr": "Conn", "kwargs": {}},
                    "first_call": {"method": "first"},
                    "close": {"method": "close"},
                    "rounds": 1,
                    "concurrent": 2,
                }
            ],
        }
    )
    with caplog.at_level(logging.INFO, logger="probes.runner"):
        result = asyncio.run(run_provider_probe(probe, _registry(), _settings()))
    case = next(c for c in result.cases if c.case_id == "conn-steps")
    assert case.verdict == "confirmed"
    steps = _step_messages(caplog)
    assert any("round1#1 created=" in m and "first_call=" in m for m in steps)
    assert any("round1#2 created=" in m and "first_call=" in m for m in steps)
    assert any("round1#1 closed=" in m for m in steps)
    assert any("round1#2 closed=" in m for m in steps)


def test_adjust_diff_and_variant_step_lines_logged(caplog: pytest.LogCaptureFixture) -> None:
    """adjust_diff / variant_consistency：每侧抓取一行子步骤（标签+bar 数）。"""
    registry = _registry_for("multi_variant.yaml")
    window = {"start": "2025-01-01", "end": "2025-03-31"}
    probe = _multi_var_probe(
        "multi-var",
        {
            "adjust_diff": {"targets": ["stock"], "window": window, "pairs": [["none", "qfq"]]},
            "variant_consistency": {"targets": ["stock"], "window": window},
        },
    )
    with caplog.at_level(logging.INFO, logger="probes.runner"):
        result = asyncio.run(run_provider_probe(probe, registry, _settings()))
    assert [c for c in result.cases if c.kind == "adjust_diff"]
    steps = _step_messages(caplog)
    assert any("baseline(none) → " in m and "bars" in m for m in steps)
    assert any("compared(qfq) → " in m and "bars" in m for m in steps)
    # variant_consistency 的 variants 是数据源 variant（eastmoney/sina），逐个抓取
    assert any("variant=eastmoney → " in m and "bars" in m for m in steps)
    assert any("variant=sina → " in m and "bars" in m for m in steps)


def test_history_depth_phase_step_lines_logged(caplog: pytest.LogCaptureFixture) -> None:
    """history_depth 二分模式：era/bisect 阶段子步骤行带预算水位。"""
    registry = _registry_for("multi_variant.yaml")
    probe = _multi_var_probe(
        "multi-var",
        {"history_depth": {"targets": ["stock"], "earliest": "2024-01-01", "max_calls": 6}},
    )
    with caplog.at_level(logging.INFO, logger="probes.runner"):
        result = asyncio.run(run_provider_probe(probe, registry, _settings()))
    cases = [c for c in result.cases if c.kind == "history_depth"]
    assert cases and all(c.verdict == "observe" for c in cases)
    steps = _step_messages(caplog)
    phase_steps = [m for m in steps if "era#" in m or "bisect#" in m or "verify#" in m]
    assert phase_steps, "bisect-mode history depth must emit phase step lines"
    assert any("(预算 " in m for m in phase_steps)


def test_quote_availability_confirmed() -> None:
    """快照可用性：全链路 fetch_quote 复验，dims 不携带周期/复权维度。"""
    result = asyncio.run(run_provider_probe(_probe(), _registry(), _settings()))
    quotes = [c for c in result.cases if c.kind == "quote_availability"]
    assert {c.case_id for c in quotes} == {"quote-stock-eastmoney"}
    case = quotes[0]
    assert case.verdict == "confirmed"
    assert case.observation["last_price"] == 10.0
    assert case.observation["timestamp"]
    assert "interval" not in case.dims and "adjust" not in case.dims
    assert case.dims["variant"] == "eastmoney"
    assert result.call_budget["used"] >= 3


def test_quote_no_usable_price_failed() -> None:
    """last/bid/ask 全空判 failed（订阅未就绪 NaN 置空、权限静默降级的捕捉）。"""
    data = _probe().model_dump()
    # fake 适配器对 index 快照返回 last/bid/ask 全空（模拟指数快照语义）
    data["targets"] = {
        "index": {
            "instrument": "CN-SSE-000001",
            "window": {"start": "2025-01-01", "end": "2025-03-31"},
        }
    }
    result = asyncio.run(
        run_provider_probe(ProbeFile.model_validate(data), _registry(), _settings())
    )
    quotes = [c for c in result.cases if c.kind == "quote_availability"]
    assert [c.verdict for c in quotes] == ["failed"]
    assert "no usable price" in quotes[0].reason
    assert quotes[0].observation["last_price"] is None


def test_quote_adapter_error_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """适配器失败（上游「无有效行情」语义）→ failed，错误进 observation。"""
    registry = _registry()

    async def _boom(query: object) -> object:
        raise UpstreamError("no valid quote item", details={"provider": "fake-provider"})

    record = registry.get("fake-provider")
    assert record is not None and record.adapter is not None
    monkeypatch.setattr(record.adapter, "fetch_quote", _boom)
    result = asyncio.run(run_provider_probe(_probe(), registry, _settings()))
    quotes = [c for c in result.cases if c.kind == "quote_availability"]
    assert [c.verdict for c in quotes] == ["failed"]
    assert quotes[0].observation["error"]["error_code"] == "UPSTREAM_ERROR"
