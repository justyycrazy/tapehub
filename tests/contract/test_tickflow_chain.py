"""TickFlow 完整 HTTP 链路契约测试（SDK 用桩 Mock，无真实网络）。"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tapehub.api.app import create_app
from tapehub.providers.registry import build_default_registry
from tapehub.settings import Settings
from tests.fixtures.providers.stub_tickflow import (
    StubTickFlow,
    install,
    klines_payload,
    sized_klines_payload,
)

BASE_PARAMS = {
    "provider": "tickflow",
    "asset_type": "stock",
    "instrument": "CN-SSE-600000",
    "interval": "P1D",
    "start": "2026-01-01",
    "end": "2026-01-10",
}


def preset_payload(monkeypatch: pytest.MonkeyPatch, payload: Any) -> None:
    """让每个新建的桩客户端都返回指定响应。"""
    original_init = StubTickFlow.__init__

    def init_with_payload(self: StubTickFlow, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.payload = payload

    monkeypatch.setattr(StubTickFlow, "__init__", init_with_payload)


@pytest.fixture()
def tickflow_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    install(monkeypatch)
    settings = Settings(max_limit=1000, tickflow_api_key="test-key")
    return TestClient(create_app(settings=settings, registry=build_default_registry(settings)))


def test_full_chain_default_adjust_is_qfq(
    tickflow_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    preset_payload(monkeypatch, klines_payload(["2026-01-%02d" % d for d in range(2, 10)]))

    response = tickflow_client.get("/v1/market/bars", params=BASE_PARAMS)
    assert response.status_code == 200
    body = response.json()

    client = StubTickFlow.instances[-1]
    assert client.api_key == "test-key"
    kwargs = client.calls[0]
    # 未传递 adjust → 股票默认 qfq → forward；所有参数显式传递
    assert kwargs["symbol"] == "600000.SH"
    assert kwargs["adjust"] == "forward"
    assert kwargs["period"] == "1d"

    data = body["data"]
    assert data["instrument"] == "CN-SSE-600000"
    assert len(data["bars"]) == 8
    timestamps = [bar["timestamp"] for bar in data["bars"]]
    assert timestamps == sorted(timestamps)
    assert data["bars"][0]["extensions"]["prev_close"] == 10.1

    meta = body["meta"]
    assert meta["provider"] == "tickflow"
    assert meta["count"] == 8
    assert meta["partial"] is False
    assert body["warnings"] == []


def test_truncation_warning_when_upstream_limit_reached(
    tickflow_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    preset_payload(monkeypatch, sized_klines_payload(10000))

    response = tickflow_client.get("/v1/market/bars", params=BASE_PARAMS)
    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["partial"] is True
    assert body["warnings"]  # 截断提示
    assert body["meta"]["count"] == 500  # 统一分页默认 limit


def test_missing_api_key_returns_clear_error(default_client) -> None:  # type: ignore[no-untyped-def]
    """未配置 API Key 时能力目录正常，数据请求返回明确错误。"""
    caps = default_client.get("/v1/providers/tickflow/capabilities")
    assert caps.status_code == 200
    assert caps.json()["data"]["enabled"] is True

    response = default_client.get("/v1/market/bars", params=BASE_PARAMS)
    assert response.status_code == 501
    error = response.json()["error"]
    assert error["code"] == "PROVIDER_NOT_IMPLEMENTED"
    assert error["details"]["setting"] == "TAPEHUB_TICKFLOW_API_KEY"


def test_capability_catalog(tickflow_client: TestClient) -> None:  # type: ignore[no-untyped-def]
    response = tickflow_client.get("/v1/capabilities?provider=tickflow")
    assert response.status_code == 200
    entry = response.json()["data"][0]
    assert entry["provider"] == "tickflow"
    assert entry["enabled"] is True
    cap = entry["capabilities"][0]
    assert cap["asset_types"] == ["stock"]
    assert [i["code"] for i in cap["intervals"]] == [
        "PT1M", "PT5M", "PT10M", "PT15M", "PT30M",
        "PT1H", "P1D", "P1W", "P1M", "P1Y",
    ]
    assert cap["adjustments"] == ["none", "qfq", "hfq"]


def test_unsupported_interval(tickflow_client: TestClient) -> None:  # type: ignore[no-untyped-def]
    # P3D 是合法 ISO 周期，但 TickFlow 未声明：能力目录即拒绝
    params = {**BASE_PARAMS, "interval": "P3D"}
    response = tickflow_client.get("/v1/market/bars", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_CAPABILITY"


def test_adjust_hfq_maps_to_backward(
    tickflow_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    preset_payload(monkeypatch, klines_payload(["2026-01-02"]))

    response = tickflow_client.get(
        "/v1/market/bars", params={**BASE_PARAMS, "adjust": "hfq"}
    )
    assert response.status_code == 200
    assert StubTickFlow.instances[-1].calls[0]["adjust"] == "backward"
