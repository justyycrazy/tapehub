"""Twelve Data 完整 HTTP 链路契约测试（SDK 用桩 Mock，无真实网络）。"""

import pytest
from fastapi.testclient import TestClient

from tapehub.api.app import create_app
from tapehub.providers.registry import build_default_registry
from tapehub.settings import Settings
from tests.fixtures.providers.stub_twelvedata import (
    forex_values_rows,
    install,
    preset_values,
)

FOREX_PARAMS = {
    "provider": "twelvedata",
    "asset_type": "forex",
    "instrument": "GLOBAL-OTC-EUR/USD",
    "interval": "P1D",
    "start": "2026-01-01",
    "end": "2026-01-10",
}


@pytest.fixture()
def twelvedata_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    install(monkeypatch)
    settings = Settings(max_limit=1000, twelvedata_api_key="test-key")
    return TestClient(create_app(settings=settings, registry=build_default_registry(settings)))


def test_forex_chain(twelvedata_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    preset_values(monkeypatch, forex_values_rows(["2026-01-%02d" % d for d in range(2, 10)]))

    response = twelvedata_client.get("/v1/market/bars", params=FOREX_PARAMS)
    assert response.status_code == 200
    body = response.json()

    data = body["data"]
    assert data["instrument"] == "GLOBAL-OTC-EUR/USD"
    assert data["asset_type"] == "forex"
    assert data["interval"] == "P1D"
    assert len(data["bars"]) == 8
    timestamps = [bar["timestamp"] for bar in data["bars"]]
    assert timestamps == sorted(timestamps)  # 上游倒序 → 统一升序
    assert data["bars"][0]["timestamp"] == "2026-01-02T00:00:00Z"
    assert data["bars"][0]["close"] == 1.0855
    assert data["bars"][0]["volume"] is None  # 外汇无 volume 字段

    meta = body["meta"]
    assert meta["provider"] == "twelvedata"
    assert meta["count"] == 8
    assert meta["partial"] is False
    assert body["warnings"] == []


def test_missing_api_key_returns_clear_error(default_client) -> None:  # type: ignore[no-untyped-def]
    """未配置 API Key 时能力目录正常，数据请求返回明确错误。"""
    caps = default_client.get("/v1/providers/twelvedata/capabilities")
    assert caps.status_code == 200
    assert caps.json()["data"]["enabled"] is True

    response = default_client.get("/v1/market/bars", params=FOREX_PARAMS)
    assert response.status_code == 501
    error = response.json()["error"]
    assert error["code"] == "PROVIDER_NOT_IMPLEMENTED"
    assert error["details"]["setting"] == "TAPEHUB_TWELVEDATA_API_KEY"


def test_capability_catalog(twelvedata_client: TestClient) -> None:  # type: ignore[no-untyped-def]
    response = twelvedata_client.get("/v1/capabilities?provider=twelvedata")
    assert response.status_code == 200
    entry = response.json()["data"][0]
    assert entry["provider"] == "twelvedata"
    assert entry["enabled"] is True
    assert len(entry["capabilities"]) == 2
    asset_types = {tuple(cap["asset_types"]) for cap in entry["capabilities"]}
    assert asset_types == {("forex",), ("crypto",)}
    for cap in entry["capabilities"]:
        assert cap["markets"] == ["GLOBAL"]
        assert [i["code"] for i in cap["intervals"]] == [
            "PT1M", "PT5M", "PT15M", "PT30M", "PT1H", "P1D", "P1W", "P1M",
        ]
        assert cap["adjustments"] == ["none"]
        assert cap["fields"] == ["open", "high", "low", "close"]


def test_unsupported_interval_p3d(twelvedata_client: TestClient) -> None:  # type: ignore[no-untyped-def]
    params = {**FOREX_PARAMS, "interval": "P3D"}
    response = twelvedata_client.get("/v1/market/bars", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_CAPABILITY"


def test_crypto_chain(twelvedata_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    preset_values(monkeypatch, forex_values_rows(["2026-01-02 00:00:00"]))

    params = {
        "provider": "twelvedata",
        "asset_type": "crypto",
        "instrument": "GLOBAL-CRYPTO-BTC/USD",
        "interval": "P1D",
        "start": "2026-01-01",
        "end": "2026-01-10",
    }
    response = twelvedata_client.get("/v1/market/bars", params=params)
    assert response.status_code == 200

    from tests.fixtures.providers.stub_twelvedata import StubTDClient

    assert StubTDClient.instances[-1].calls[0]["symbol"] == "BTC/USD"
    assert response.json()["meta"]["provider"] == "twelvedata"
