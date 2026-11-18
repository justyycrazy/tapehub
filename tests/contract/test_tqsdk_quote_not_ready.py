"""tqsdk 实时快照首包超时（QUOTE_NOT_READY 408）契约测试（backlog #2）。

经假 SDK 走完整 HTTP 链路：冷订阅首包超时 → 适配器抛 ``QuoteNotReadyError``
→ 408 + 统一错误结构（details 携带 symbol 与 timeout_s）。

订阅租约在 408 路径的退订语义（回收阀门可达性）由单元级既有用例锚定
（``test_tqsdk_provider.py::test_quote_lease_released_on_first_packet_timeout``
与 ``::test_quote_lease_released_on_client_cancel``），本文件不重复。
"""

import types

import pytest
from fastapi.testclient import TestClient

from tapehub.api.app import create_app
from tapehub.providers.registry import build_default_registry
from tests.conftest import make_settings
from tests.fixtures.providers import fake_tqsdk_runtime as fake_rt

_QUOTE_PARAMS = {
    "provider": "tqsdk",
    "asset_type": "contract",
    "instrument": "CN-SHFE-AU2612",
}


@pytest.fixture()
def fake_tqsdk(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    return fake_rt.install(monkeypatch)


def _client(**setting_overrides: object):  # type: ignore[no-untyped-def]
    """构建测试应用；调用方 teardown 显式关闭 tqsdk Runtime（遗留线程会
    偷走后续注入的类属性旋钮，见 test_quote_stream.py 同款注释）。"""
    options: dict = {
        "tqsdk_conn_mode": "shared",
        "tqsdk_auth_user": "u@example.com",
        "tqsdk_auth_password": "secret",
        "tqsdk_first_snapshot_timeout": 0.3,
    }
    options.update(setting_overrides)
    settings = make_settings(**options)
    registry = build_default_registry(settings)
    return registry, TestClient(create_app(settings=settings, registry=registry))


def test_quote_first_packet_timeout_maps_to_408(
    fake_tqsdk: types.ModuleType,
) -> None:
    """冷订阅首包永不到达（first_packet_ticks 极大）→ 408 QUOTE_NOT_READY。"""
    fake_rt.FakeTqApi.quote_first_packet_ticks = 100_000
    registry, client = _client()
    try:
        resp = client.get("/v1/market/quotes", params=_QUOTE_PARAMS)
    finally:
        adapter = registry.get("tqsdk").adapter
        shutdown = getattr(adapter, "shutdown_runtime", None)
        if shutdown is not None:
            shutdown()
    assert resp.status_code == 408
    body = resp.json()
    assert body["error"]["code"] == "QUOTE_NOT_READY"
    assert body["error"]["details"]["timeout_s"] == 0.3
    assert "symbol" in body["error"]["details"]
    assert body["error"]["request_id"].startswith("req_")
