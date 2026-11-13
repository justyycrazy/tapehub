"""Tiingo 实时快照流（SSE）契约测试：假 websockets 桩走完整 HTTP 链路。

覆盖：响应头与首事件（status subscribed → quote）、心跳帧、断连显式
upstream_disconnected 事件与退避重连恢复、断开退订（引用归零）、
客户端上限 429（STREAM_LIMIT_REACHED）、上游并发订阅上限 429
（UPSTREAM_SUBSCRIPTION_LIMIT，按端点生效）、空闲退订 idle_unsubscribed。
"""

import contextlib
import json
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

import tapehub.providers.tiingo.runtime as tiingo_runtime
from tapehub.api.app import create_app
from tapehub.providers.registry import build_default_registry
from tests.conftest import make_settings
from tests.fixtures.providers import fake_tiingo_ws as fake_ws

_STREAM_PARAMS = {
    "provider": "tiingo",
    "asset_type": "forex",
    "instrument": "GLOBAL-OTC-EUR/USD",
}


@pytest.fixture()
def fake_ws_module(monkeypatch: pytest.MonkeyPatch) -> object:
    return fake_ws.install(monkeypatch)


@contextlib.contextmanager
def _client(**setting_overrides: object) -> Iterator[tuple[TestClient, object]]:
    """构建 SSE 测试应用；teardown 显式关闭 WS Runtime（防跨测试泄漏）。"""
    options: dict = {
        "tiingo_api_key": "test-key",
        "stream_heartbeat_seconds": 0.05,
        "stream_idle_timeout_seconds": 300.0,
    }
    options.update(setting_overrides)
    settings = make_settings(**options)
    registry = build_default_registry(settings)
    try:
        yield TestClient(create_app(settings=settings, registry=registry)), registry
    finally:
        adapter = registry.get("tiingo").adapter
        shutdown = getattr(adapter, "shutdown_runtime", None)
        if shutdown is not None:
            shutdown()


def _read_frames(resp, count: int) -> list[dict]:
    return _read_until(resp, lambda frames: len(frames) >= count)


def _read_until(resp, done, limit: int = 200) -> list[dict]:
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


def test_sse_headers_and_first_frames(fake_ws_module: object) -> None:
    with _client() as (client, _), client.stream(
        "GET", "/v1/market/quotes/stream", params=_STREAM_PARAMS
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["x-accel-buffering"] == "no"
        frames = _read_frames(resp, 2)
    # 首事件 status subscribed（WS 首条匹配 data 元素确认），随后首帧 quote
    # （TOP 元素 midPrice 回退 +08:00）
    assert frames[0]["event"] == "status"
    assert frames[0]["data"]["status"] == "subscribed"
    assert frames[0]["data"]["provider"] == "tiingo"
    assert frames[1]["event"] == "quote"
    assert frames[1]["data"]["quote"]["last_price"] == 1.0855
    assert frames[1]["data"]["quote"]["timestamp"].endswith("+08:00")
    assert frames[1]["data"]["variant"] == "tiingo"
    assert frames[1]["data"]["received_at"].endswith("+08:00")


def test_sse_heartbeat_when_idle(fake_ws_module: object) -> None:
    # 首包后无增量（auto_price 关闭）：心跳帧按配置周期出现
    with _client() as (client, _), client.stream(
        "GET", "/v1/market/quotes/stream", params=_STREAM_PARAMS
    ) as resp:
        frames = _read_frames(resp, 4)
    kinds = [frame["event"] for frame in frames]
    assert kinds[0] == "status"
    assert kinds[1] == "quote"
    assert "heartbeat" in kinds[2:]
    heartbeat = next(frame for frame in frames if frame["event"] == "heartbeat")
    assert heartbeat["data"]["type"] == "heartbeat"
    assert heartbeat["data"]["ts"].endswith("+08:00")


def test_sse_disconnect_emits_status_and_recovers(
    fake_ws_module: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """断连绝不静默：显式 upstream_disconnected → 退避重连自动重订恢复 subscribed。"""
    fake_ws.FakeWebSocket.auto_price_seconds = 0.02  # 持续喂流，保证断连后可观测恢复
    fake_ws.FakeWebSocket.drop_after_messages = 3  # 订阅确认 + 首包 + 一条增量后断连
    monkeypatch.setattr(tiingo_runtime, "_RECONNECT_BACKOFF_SECONDS", 0.02)

    def _recovered(frames: list[dict]) -> bool:
        statuses = [f["data"]["status"] for f in frames if f["event"] == "status"]
        return "upstream_disconnected" in statuses and statuses.count("subscribed") >= 2

    with _client() as (client, _), client.stream(
        "GET", "/v1/market/quotes/stream", params=_STREAM_PARAMS
    ) as resp:
        frames = _read_until(resp, _recovered, limit=100)

    statuses = [f["data"]["status"] for f in frames if f["event"] == "status"]
    assert "upstream_disconnected" in statuses
    assert statuses.count("subscribed") >= 2  # 重订恢复（绝不静默断流）


def test_sse_disconnect_unsubscribes(fake_ws_module: object) -> None:
    """流关闭即本地退订：引用归零（上游退订经 linger，见单测断言）。"""
    with _client() as (client, registry):
        with client.stream(
            "GET", "/v1/market/quotes/stream", params=_STREAM_PARAMS
        ) as resp:
            _read_frames(resp, 2)
        # 在 teardown 关闭 Runtime 之前抓引用（shutdown 清空 adapter._runtimes）
        adapter = registry.get("tiingo").adapter
        runtime = adapter._runtimes["forex"]
        for _ in range(100):
            if not runtime._refs:
                break
            time.sleep(0.02)
        assert not runtime._refs  # 引用已释放（linger 窗口内快照可读，上游订阅延迟退订）


def test_sse_stream_limit_reached(fake_ws_module: object) -> None:
    with _client(stream_max_clients=1) as (client, _), client.stream(
        "GET", "/v1/market/quotes/stream", params=_STREAM_PARAMS
    ):
        # 第一条流占用期间，第二条被服务容量闸门拒绝（统一 JSON 错误）
        second = client.get("/v1/market/quotes/stream", params=_STREAM_PARAMS)
        assert second.status_code == 429
        body = second.json()
        assert body["error"]["code"] == "STREAM_LIMIT_REACHED"
        assert body["error"]["details"]["limit"] == 1


def test_sse_upstream_subscription_limit(fake_ws_module: object) -> None:
    """上游并发订阅软上限（按端点生效）：同端点新 symbol 急切 429。"""
    gbp_params = {
        "provider": "tiingo",
        "asset_type": "forex",
        "instrument": "GLOBAL-OTC-EUR/GBP",
    }
    with _client(tiingo_subscription_limit=1) as (client, _), client.stream(
        "GET", "/v1/market/quotes/stream", params=_STREAM_PARAMS
    ) as resp:
        _read_frames(resp, 2)  # 首个流占用 forex 端点唯一订阅额度
        second = client.get("/v1/market/quotes/stream", params=gbp_params)
        assert second.status_code == 429
        body = second.json()
        assert body["error"]["code"] == "UPSTREAM_SUBSCRIPTION_LIMIT"
        assert body["error"]["details"]["limit"] == 1


def test_sse_idle_timeout_ends_stream(fake_ws_module: object) -> None:
    """空闲退订：无行情事件超时 → idle_unsubscribed 显式收尾后结束流。"""
    with _client(stream_idle_timeout_seconds=0.15) as (client, _), client.stream(
        "GET", "/v1/market/quotes/stream", params=_STREAM_PARAMS
    ) as resp:
        frames = _read_until(
            resp,
            lambda fs: fs[-1]["event"] == "status"
            and fs[-1]["data"]["status"] == "idle_unsubscribed",
        )
    assert frames[-1]["data"]["status"] == "idle_unsubscribed"
