"""连接探针与 kwargs 引用扩展测试（离线：注入的假 SDK 模块，零网络）。

覆盖 M0 新增的探针框架机制：

- connection case：创建 → 首次取数 → 释放 的生命周期计时、并发共存、判定；
- ``$ctor`` 嵌套对象构造（如认证对象）；``$deadline_s`` 相对秒数；
- raw 调用 ``target: client`` 与 ``min_fields``（mapping 字段存在性判定）。
"""

import asyncio
import json
import sys
import time
import types
from pathlib import Path

import pytest
from probes.loader import expand_probe
from probes.runner import _interpolate, run_provider_probe
from probes.schema import CallSpec, ConnectionCaseSpec, ExpectSpec, ProbeFile
from pydantic import ValidationError

from tapehub.providers.manifest import load_manifest_file
from tapehub.providers.registry import ProviderRegistry, build_provider_record
from tests.conftest import fake_routing, make_settings

FIXTURES = Path(__file__).parents[2] / "fixtures"


class FakeTqApi:
    """连接探针用假客户端：记录创建/调用/释放，实例轨迹可断言。"""

    instances: list["FakeTqApi"] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.closed = False
        FakeTqApi.instances.append(self)

    def get_kline_serial(self, **kwargs: object) -> dict[str, int]:
        return {"rows": 2}

    def get_quote(self, **kwargs: object) -> dict[str, float]:
        return {"last_price": float("nan"), "bid_price1": 1.0}

    def wait(self) -> list[int]:
        return [1, 2, 3]

    def close(self) -> None:
        self.closed = True


class FakeTqAuth:
    def __init__(self, user: str = "", password: str = "") -> None:
        self.user = user
        self.password = password


class FailingClient:
    def __init__(self, **kwargs: object) -> None:
        raise RuntimeError("connection refused")

    def close(self) -> None:  # pragma: no cover - 不会到达
        pass


@pytest.fixture()
def fake_module(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    module = types.ModuleType("fake_tqsdk")
    module.FakeTqApi = FakeTqApi  # type: ignore[attr-defined]
    module.FakeTqAuth = FakeTqAuth  # type: ignore[attr-defined]
    module.FailingClient = FailingClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake_tqsdk", module)
    FakeTqApi.instances = []
    return module


def _registry() -> ProviderRegistry:
    manifest = load_manifest_file(FIXTURES / "manifests" / "fake_provider.yaml")
    from tests.fixtures.providers.fake_provider import FakeBarProvider

    return ProviderRegistry(
        [build_provider_record("fake-provider", manifest, FakeBarProvider)], fake_routing()
    )


def _connection_case(**overrides: object) -> dict[str, object]:
    case: dict[str, object] = {
        "id": "conn-ok",
        "purpose": "lifecycle timing",
        "module": "fake_tqsdk",
        "client": {
            "attr": "FakeTqApi",
            "kwargs": {
                "auth": {
                    "$ctor": {
                        "attr": "FakeTqAuth",
                        "kwargs": {"user": "$FAKE_USER", "password": "p"},
                    }
                },
                "disable_print": True,
            },
        },
        "first_call": {"method": "get_kline_serial", "kwargs": {"symbol": "X", "n": 2}},
        "close": {"method": "close"},
        "rounds": 2,
        "concurrent": 2,
    }
    case.update(overrides)
    return case


def _probe(
    connection: list[dict[str, object]], raw: list[dict[str, object]] | None = None
) -> ProbeFile:
    data: dict[str, object] = {
        "probe": "fake-provider",
        "version": 1,
        "targets": {
            "stock": {
                "instrument": "CN-SSE-600000",
                "window": {"start": "2025-01-01", "end": "2025-03-31"},
                "min_bars": 10,
            }
        },
        "connection": connection,
    }
    if raw is not None:
        data["raw"] = raw
    return ProbeFile.model_validate(data)


# ---------------------------------------------------------------------------
# schema 约束
# ---------------------------------------------------------------------------


def test_connection_close_must_be_client_method() -> None:
    with pytest.raises(ValidationError):
        ConnectionCaseSpec.model_validate(
            {
                "id": "conn",
                "module": "fake_tqsdk",
                "client": {"attr": "C", "kwargs": {}},
                "first_call": {"method": "start"},
                "close": {"module_func": "shutdown"},
            }
        )


def test_connection_call_target_is_implicit() -> None:
    with pytest.raises(ValidationError):
        ConnectionCaseSpec.model_validate(
            {
                "id": "conn",
                "module": "fake_tqsdk",
                "client": {"attr": "C", "kwargs": {}},
                "first_call": {"method": "start", "target": "client"},
                "close": {"method": "close"},
            }
        )


def test_expect_ok_requires_rows_or_fields() -> None:
    with pytest.raises(ValidationError):
        ExpectSpec.model_validate({"status": "ok"})
    spec = ExpectSpec.model_validate({"status": "ok", "min_fields": ["last_price"]})
    assert spec.min_fields == ["last_price"]


def test_call_target_accepts_client_and_result() -> None:
    assert CallSpec.model_validate({"method": "wait", "target": "client"}).target == "client"
    with pytest.raises(ValidationError):
        CallSpec.model_validate({"method": "wait", "target": "bogus"})


# ---------------------------------------------------------------------------
# kwargs 引用展开
# ---------------------------------------------------------------------------


def test_interpolate_ctor_constructs_object(
    fake_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_USER", "someone")
    value = _interpolate(
        {"auth": {"$ctor": {"attr": "FakeTqAuth", "kwargs": {"user": "$FAKE_USER"}}}},
        "fake_tqsdk",
    )
    assert isinstance(value["auth"], FakeTqAuth)
    assert value["auth"].user == "someone"


def test_interpolate_ctor_missing_env(fake_module: types.ModuleType) -> None:
    from probes.runner import MissingEnvError

    with pytest.raises(MissingEnvError):
        _interpolate(
            {"auth": {"$ctor": {"attr": "FakeTqAuth", "kwargs": {"user": "$NOT_SET_XYZ"}}}},
            "fake_tqsdk",
        )


def test_interpolate_deadline_s() -> None:
    before = time.time()
    deadline = _interpolate({"$deadline_s": 30})
    assert before + 29 < deadline < time.time() + 31


# ---------------------------------------------------------------------------
# loader 展开
# ---------------------------------------------------------------------------


def test_connection_plan_expected_calls(fake_module: types.ModuleType) -> None:
    probe = _probe([_connection_case()])
    manifest = load_manifest_file(FIXTURES / "manifests" / "fake_provider.yaml")
    expanded = expand_probe(probe, manifest)
    assert [p.spec.id for p in expanded.connection_cases] == ["conn-ok"]
    # rounds(2) x concurrent(2) x (创建+首调+释放)
    assert expanded.connection_cases[0].expected_calls == 12


def test_connection_id_collision_rejected(fake_module: types.ModuleType) -> None:
    # 跨 kind 碰撞（connection vs raw）与同 kind 重复都在 schema 层被拒绝
    with pytest.raises(ValidationError, match="overlap"):
        _probe(
            [_connection_case(id="conn-ok", rounds=1)],
            raw=[
                {
                    "id": "conn-ok",
                    "module": "fake_tqsdk",
                    "call": [{"method": "wait"}],
                    "expect": {"status": "ok", "min_rows": 1},
                }
            ],
        )
    with pytest.raises(ValidationError, match="duplicate connection case ids"):
        _probe([_connection_case(), _connection_case(rounds=1)])


# ---------------------------------------------------------------------------
# 执行器
# ---------------------------------------------------------------------------


def test_connection_lifecycle_confirmed(
    fake_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_USER", "someone")
    result = asyncio.run(
        run_provider_probe(_probe([_connection_case()]), _registry(), make_settings())
    )
    conn = [c for c in result.cases if c.kind == "connection"]
    assert len(conn) == 1
    case = conn[0]
    assert case.verdict == "confirmed"
    assert case.observation["instances_expected"] == 4
    assert case.observation["created"] == 4
    assert case.observation["first_call_ok"] == 4
    assert case.observation["closed"] == 4
    assert len(case.observation["timings"]) == 4
    # auth 经 $ctor 构造并传入每个实例
    assert all(
        isinstance(inst.kwargs["auth"], FakeTqAuth) for inst in FakeTqApi.instances
    )
    # 全部实例已释放
    assert all(inst.closed for inst in FakeTqApi.instances)
    assert result.call_budget["used"] >= 12


def test_connection_failure_marks_failed(
    fake_module: types.ModuleType,
) -> None:
    case = _connection_case(id="conn-fail", client={"attr": "FailingClient", "kwargs": {}})
    result = asyncio.run(run_provider_probe(_probe([case]), _registry(), make_settings()))
    conn = [c for c in result.cases if c.kind == "connection"]
    assert conn[0].verdict == "failed"
    assert any("connection refused" in e["text"] for e in conn[0].observation["errors"])


def test_first_call_timeout_still_closes_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """connection 首调超时：已创建的客户端在失败路径上仍被释放（防连接泄漏）。

    真实探针实证的泄漏形态：_spawn 首调挂起超时后，客户端随异常丢弃、
    永远无人 close（TqApi 及其私有 loop/asyncgen 由 executor
    线程持有至进程退出）。
    """

    class _HangingFirstCallClient:
        instances: list["_HangingFirstCallClient"] = []

        def __init__(self, **kwargs: object) -> None:
            self.closed = False
            _HangingFirstCallClient.instances.append(self)

        def first(self, **kwargs: object) -> None:
            time.sleep(2)  # 超过 case timeout_s，触发 wait_for 超时

        def close(self) -> None:
            self.closed = True

    module = types.ModuleType("fake_tqsdk_hang")
    module.HangingClient = _HangingFirstCallClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake_tqsdk_hang", module)
    _HangingFirstCallClient.instances.clear()

    probe = ProbeFile.model_validate(
        {
            "probe": "fake-provider",
            "version": 1,
            "pacing": {"call_interval_s": 0.01, "timeout_s": 0.2, "max_calls": 10},
            "targets": {
                "stock": {
                    "instrument": "CN-SSE-600000",
                    "window": {"start": "2025-01-01", "end": "2025-03-31"},
                    "min_bars": 10,
                }
            },
            "connection": [
                {
                    "id": "conn-hang",
                    "module": "fake_tqsdk_hang",
                    "client": {"attr": "HangingClient", "kwargs": {}},
                    "first_call": {"method": "first"},
                    "close": {"method": "close"},
                    "rounds": 1,
                    "concurrent": 1,
                }
            ],
        }
    )
    result = asyncio.run(
        run_provider_probe(
            probe,
            _registry(),
            make_settings(max_limit=1000, catalog_version="test"),
        )
    )
    conn = [c for c in result.cases if c.kind == "connection"]
    assert conn[0].verdict == "failed"
    # 首调超时的客户端仍被失败路径释放，而非随异常丢弃
    assert _HangingFirstCallClient.instances[-1].closed is True


def test_connection_dry_run_plans_calls(fake_module: types.ModuleType) -> None:
    result = asyncio.run(
        run_provider_probe(
            _probe([_connection_case()]), _registry(), make_settings(), execute=False
        )
    )
    conn = [c for c in result.cases if c.kind == "connection"]
    assert conn[0].verdict == "skipped"
    assert "planned 12 calls" in conn[0].reason


def test_raw_call_target_client_and_min_fields(
    fake_module: types.ModuleType,
) -> None:
    raw = [
        {
            "id": "raw-target-client",
            "module": "fake_tqsdk",
            "client": {"attr": "FakeTqApi", "kwargs": {}},
            # 首条作用于 client（返回 dict），第二条 target=client 强制回到
            # 客户端本身——若路由错误会 AttributeError
            "call": [
                {"method": "get_quote", "kwargs": {"symbol": "X"}},
                {"method": "wait", "target": "client", "kwargs": {}},
            ],
            "expect": {"status": "ok", "min_rows": 3},
        },
        {
            "id": "raw-min-fields",
            "module": "fake_tqsdk",
            "call": [{"module_func": "FakeTqApi", "kwargs": {}}],
            "expect": {"status": "ok", "min_fields": ["last_price", "bid_price1"]},
            "assert": False,
        },
        {
            "id": "raw-min-fields-missing",
            "module": "fake_tqsdk",
            "client": {"attr": "FakeTqApi", "kwargs": {}},
            "call": [{"method": "get_quote", "kwargs": {}}],
            "expect": {"status": "ok", "min_fields": ["last_price", "ask_price1"]},
        },
    ]
    result = asyncio.run(run_provider_probe(_probe([], raw=raw), _registry(), make_settings()))
    by_id = {c.case_id: c for c in result.cases if c.kind == "raw"}
    assert by_id["raw-target-client"].verdict == "confirmed"
    # min_fields 命中：observe（assert=false）
    assert by_id["raw-min-fields"].verdict == "observe"
    # min_fields 缺失字段：failed
    assert by_id["raw-min-fields-missing"].verdict == "failed"
    assert "fields_ok=False" in by_id["raw-min-fields-missing"].reason


def test_lookup_env_falls_back_to_settings(
    fake_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TAPEHUB_* 引用在进程环境缺失时回退 Settings（含 .env），非该前缀不回退。"""
    from probes import runner

    monkeypatch.setattr(
        runner,
        "_settings_env",
        make_settings(tqsdk_auth_user="user-from-env-file", tqsdk_auth_password="p"),
    )
    monkeypatch.delenv("TAPEHUB_TQSDK_AUTH_USER", raising=False)
    assert runner._lookup_env("TAPEHUB_TQSDK_AUTH_USER") == "user-from-env-file"
    # 非 TAPEHUB_ 前缀不做 Settings 回退
    monkeypatch.setenv("FAKE_PLAIN", "shell")
    assert runner._lookup_env("FAKE_PLAIN") == "shell"
    assert runner._lookup_env("NOT_SET_ANYWHERE_XYZ") is None


def test_bad_case_kwargs_isolated_not_fatal(
    fake_module: types.ModuleType,
) -> None:
    """单条 case 的 kwargs 配置错误只跳过该 case，不中止整轮运行。"""
    raw = [
        {
            "id": "raw-bad-ctor",
            "module": "fake_tqsdk",
            "client": {
                "attr": "FakeTqApi",
                "kwargs": {"auth": {"$ctor": {"attr": "NoSuchClass", "kwargs": {}}}},
            },
            "call": [{"method": "wait", "kwargs": {}}],
            "expect": {"status": "ok", "min_rows": 1},
        },
        {
            "id": "raw-good",
            "module": "fake_tqsdk",
            "call": [{"module_func": "FakeTqApi", "kwargs": {}}],
            "expect": {"status": "ok", "min_rows": 1},
            "assert": False,
        },
    ]
    result = asyncio.run(run_provider_probe(_probe([], raw=raw), _registry(), make_settings()))
    by_id = {c.case_id: c for c in result.cases if c.kind == "raw"}
    assert by_id["raw-bad-ctor"].verdict == "skipped"
    assert "kwargs resolve failed" in by_id["raw-bad-ctor"].reason
    # 后续 case 正常执行
    assert by_id["raw-good"].verdict == "observe"


def test_write_results_survives_unserializable_observation(
    fake_module: types.ModuleType, tmp_path: Path
) -> None:
    """观测混入不可深拷贝对象（SDK 内部句柄/asyncio Future）时证据照常落盘。

    回归背景：真实 tqsdk 探针曾因 to_json_dict 用 dataclasses.asdict 深拷贝
    观测树触发 cannot pickle '_asyncio.Future'，整轮运行的证据全部丢失。
    """
    from datetime import datetime

    from probes.report import CaseResult, RunResult, write_results

    from tapehub.domain.models import CN_TIMEZONE

    class UnDeepCopyable:
        """模拟 SDK 内部句柄/asyncio Future：asdict 深拷贝即崩（pickle 失败）。"""

        def __deepcopy__(self, memo: dict[int, object]) -> object:
            raise TypeError("cannot pickle '_asyncio.Future' object")

    fut = UnDeepCopyable()
    case = CaseResult(
        case_id="polluted",
        kind="raw",
        verdict="confirmed",
        reason="expectation matched",
        observation={"handle": fut, "rows": 2},
    )
    run = RunResult(
        provider="fake-provider",
        started_at=datetime(2026, 1, 1, tzinfo=CN_TIMEZONE),
        finished_at=datetime(2026, 1, 1, second=1, tzinfo=CN_TIMEZONE),
        cases=[case],
    )
    json_path, md_path = write_results(run, tmp_path)
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["cases"][0]["observation"]["rows"] == 2
    # 不可序列化对象降级为字符串表示，而不是让整轮证据丢失
    assert isinstance(payload["cases"][0]["observation"]["handle"], str)
    assert md_path.exists()
