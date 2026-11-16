"""实时快照流（SSE）契约测试：经假 SDK 走完整 HTTP 链路，零网络零凭据。

覆盖：响应头与首事件（status subscribed → quote）、心跳帧、慢客户端
coalesce、断开退订（引用归零）、客户端上限 429、空闲退订与代际回收
场景（假 SDK 模拟 04:00 重建，流收到 recycling 后随重订恢复）。
"""

import contextlib
import json
import time
import types
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from tapehub.api.app import create_app
from tapehub.providers.registry import build_default_registry
from tests.conftest import make_settings
from tests.fixtures.providers import fake_tqsdk_runtime as fake_rt

_STREAM_PARAMS = {
    "provider": "tqsdk",
    "asset_type": "contract",
    "instrument": "CN-SHFE-AU2612",
}


@pytest.fixture()
def fake_tqsdk(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    return fake_rt.install(monkeypatch)


@contextlib.contextmanager
def _client(**setting_overrides: object) -> Iterator[TestClient]:
    """构建 SSE 测试应用；teardown 必须关闭 tqsdk Runtime。

    遗留的 Runtime 线程会在后台继续 wait_update，偷走后续测试注入的
    类属性故障次数（fail_wait_update_times），必须显式善后。
    """
    options: dict = {
        "tqsdk_conn_mode": "shared",
        "tqsdk_auth_user": "u@example.com",
        "tqsdk_auth_password": "secret",
        "stream_heartbeat_seconds": 0.05,
        "stream_idle_timeout_seconds": 300.0,
    }
    options.update(setting_overrides)
    settings = make_settings(**options)
    registry = build_default_registry(settings)
    try:
        yield TestClient(create_app(settings=settings, registry=registry))
    finally:
        adapter = registry.get("tqsdk").adapter
        shutdown = getattr(adapter, "shutdown_runtime", None)
        if shutdown is not None:
            shutdown()


def _read_frames(resp, count: int) -> list[dict]:
    """读取前 count 个完整 SSE 帧（event + data 两行）后停止。"""
    return _read_until(resp, lambda frames: len(frames) >= count)


def _read_until(resp, done, limit: int = 200) -> list[dict]:
    """读取 SSE 帧直到 ``done(frames)`` 为真（上限 limit 帧防御）。"""
    frames: list[dict] = []
    event_name: str | None = None
    for line in resp.iter_lines():
        if line.startswith("event: "):
            event_name = line[len("event: ") :]
        elif line.startswith("data: ") and event_name is not None:
            frames.append({"event": event_name, "data": json.loads(line[len("data: ") :])})
            event_name = None
            if done(frames) or len(frames) >= limit:
                break
    return frames


def test_sse_headers_and_first_frames(fake_tqsdk: types.ModuleType) -> None:
    with _client() as client, client.stream(
        "GET", "/v1/market/quotes/stream", params=_STREAM_PARAMS
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["x-accel-buffering"] == "no"
        frames = _read_frames(resp, 4)
    # 冷订阅等待期内允许心跳帧先行（2026-11 修订：首事件前也保活）；首个
    # 业务事件仍是 status subscribed → 首包 quote 的顺序（P5 实证字段口径）
    kinds = [frame["event"] for frame in frames]
    assert "status" in kinds and "quote" in kinds
    assert kinds.index("status") < kinds.index("quote")
    status_frame = frames[kinds.index("status")]
    assert status_frame["data"]["status"] == "subscribed"
    assert status_frame["data"]["provider"] == "tqsdk"
    quote_frame = frames[kinds.index("quote")]
    assert quote_frame["data"]["quote"]["last_price"] == 848.24
    assert quote_frame["data"]["variant"] == "tqsdk"
    assert quote_frame["data"]["received_at"].endswith("+08:00")


def test_sse_heartbeat_when_idle(fake_tqsdk: types.ModuleType) -> None:
    # 首包到达后无增量：心跳帧按配置周期出现（含首事件之前的等待期）
    with _client() as client, client.stream(
        "GET", "/v1/market/quotes/stream", params=_STREAM_PARAMS
    ) as resp:
        frames = _read_frames(resp, 5)
    kinds = [frame["event"] for frame in frames]
    assert "status" in kinds and "quote" in kinds and "heartbeat" in kinds
    assert kinds.index("status") < kinds.index("quote")
    heartbeat = next(frame for frame in frames if frame["event"] == "heartbeat")
    assert heartbeat["data"]["type"] == "heartbeat"
    assert heartbeat["data"]["ts"].endswith("+08:00")


def test_sse_stream_limit_reached(fake_tqsdk: types.ModuleType) -> None:
    with _client(stream_max_clients=1) as client, client.stream(
        "GET", "/v1/market/quotes/stream", params=_STREAM_PARAMS
    ):
        # 第一条流占用期间，第二条被容量闸门拒绝（统一 JSON 错误）
        second = client.get("/v1/market/quotes/stream", params=_STREAM_PARAMS)
        assert second.status_code == 429
        body = second.json()
        assert body["error"]["code"] == "STREAM_LIMIT_REACHED"
        assert body["error"]["details"]["limit"] == 1


def test_sse_disconnect_unsubscribes(fake_tqsdk: types.ModuleType) -> None:
    """客户端断开：流关闭即本地退订（引用归零、快照停止维护）。"""
    from tapehub.providers.tqsdk.adapter import ProviderAdapter

    with _client() as gen_client:
        adapter = gen_client.app.state.registry.get("tqsdk").adapter
        assert isinstance(adapter, ProviderAdapter)
        with gen_client.stream(
            "GET", "/v1/market/quotes/stream", params=_STREAM_PARAMS
        ) as resp:
            _read_frames(resp, 1)
        # 连接关闭后引用释放（时序上略滞后于响应关闭，轮询等待）
        deadline = time.time() + 5
        while time.time() < deadline:
            runtime = adapter._runtime  # type: ignore[attr-defined]
            if runtime is not None and "SHFE.au2612" not in runtime._sub_refs:  # type: ignore[attr-defined]
                break
            time.sleep(0.05)
        runtime = adapter._runtime  # type: ignore[attr-defined]
        assert "SHFE.au2612" not in runtime._sub_refs  # type: ignore[attr-defined]


def test_sse_idle_timeout_ends_stream(fake_tqsdk: types.ModuleType) -> None:
    """空闲退订：超时窗口无行情事件时发 idle_unsubscribed 并结束流。"""

    def _ended(frames: list[dict]) -> bool:
        return any(
            frame["event"] == "status"
            and frame["data"]["status"] == "idle_unsubscribed"
            for frame in frames
        )

    with _client(stream_idle_timeout_seconds=0.15) as client, client.stream(
        "GET", "/v1/market/quotes/stream", params=_STREAM_PARAMS
    ) as resp:
        frames = _read_until(resp, _ended)
    kinds = [frame["event"] for frame in frames]
    assert "status" in kinds  # 订阅确认（心跳可能先行，位置不固定）
    assert "heartbeat" in kinds
    assert frames[-1]["event"] == "status"
    assert frames[-1]["data"]["status"] == "idle_unsubscribed"


def test_sse_recycle_emits_status_and_recovers(
    fake_tqsdk: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """04:00 代际回收场景：流收到 recycling → 新一代重订 → subscribed 恢复。"""
    from tapehub.providers.tqsdk.runtime import TqSdkRuntime

    # 类级注入：下一次回收时刻恒为 0.4s 后（模拟 04:00 触达；第一代的
    # next_recycle 在构造时计算，须在建连前 patch）
    monkeypatch.setattr(TqSdkRuntime, "_next_recycle_ts", lambda self: time.time() + 0.4)

    with _client(stream_heartbeat_seconds=0.2) as client:
        # 预热：建立 Runtime 与既有订阅（流注册时立即收到 subscribed + 快照）
        warmup = client.get("/v1/market/quotes", params=_STREAM_PARAMS)
        assert warmup.status_code == 200

        def _recovered(frames: list[dict]) -> bool:
            statuses = [f["data"]["status"] for f in frames if f["event"] == "status"]
            return "recycling" in statuses and statuses.count("subscribed") >= 2

        with client.stream(
            "GET", "/v1/market/quotes/stream", params=_STREAM_PARAMS
        ) as resp:
            frames = _read_until(resp, _recovered)
    statuses = [frame["data"]["status"] for frame in frames if frame["event"] == "status"]
    # 回收事件与重订恢复都到达（绝不静默断流）
    assert "recycling" in statuses
    assert statuses.count("subscribed") >= 2


def test_sse_stream_rejects_per_request_mode(fake_tqsdk: types.ModuleType) -> None:
    """per_request 回滚模式无长连接：流式不支持，统一 JSON 错误。"""
    with _client(tqsdk_conn_mode="per_request") as client:
        resp = client.get("/v1/market/quotes/stream", params=_STREAM_PARAMS)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "UNSUPPORTED_CAPABILITY"
