"""TqSdk 完整 HTTP 链路契约测试（SDK 用桩 Mock，无真实网络）。"""


import pytest
from fastapi.testclient import TestClient

from tapehub.api.app import create_app
from tapehub.providers.registry import build_default_registry
from tapehub.settings import Settings
from tests.fixtures.providers.stub_tqsdk import (
    StubTqApi,
    install,
    klines_rows,
    preset_rows,
)

BASE_PARAMS = {
    "provider": "tqsdk",
    "asset_type": "contract",
    "instrument": "CN-SHFE-cu2607",
    "interval": "P1D",
    "start": "2026-01-01",
    "end": "2026-01-10",
}


@pytest.fixture()
def tqsdk_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    install(monkeypatch)
    settings = Settings(
        max_limit=1000, tqsdk_auth_user="user@qq.com", tqsdk_auth_password="secret"
    )
    return TestClient(create_app(settings=settings, registry=build_default_registry(settings)))


def test_full_chain_continuous(
    tqsdk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """主力连续链路：KQ.m@ 符号 + 无成交额字段。"""
    preset_rows(monkeypatch, klines_rows(["2026-01-02", "2026-01-05"]))

    params = {
        "provider": "tqsdk",
        "asset_type": "continuous",
        "instrument": "CN-SHFE-cu",
        "interval": "P1D",
        "start": "2026-01-01",
        "end": "2026-01-10",
    }
    response = tqsdk_client.get("/v1/market/bars", params=params)
    assert response.status_code == 200
    body = response.json()

    api = StubTqApi.instances[-1]
    assert api.auth is not None
    assert api.auth.user == "user@qq.com"
    assert api.calls[0]["symbol"] == "KQ.m@SHFE.cu"

    data = body["data"]
    assert data["instrument"] == "CN-SHFE-cu"
    assert len(data["bars"]) == 2
    timestamps = [bar["timestamp"] for bar in data["bars"]]
    assert timestamps == sorted(timestamps)
    assert data["bars"][0]["amount"] is None  # 上游无成交额字段
    assert data["bars"][0]["volume"] == 500.0

    meta = body["meta"]
    assert meta["provider"] == "tqsdk"
    assert meta["count"] == 2
    assert body["warnings"] == []


def test_capability_catalog(tqsdk_client: TestClient) -> None:  # type: ignore[no-untyped-def]
    response = tqsdk_client.get("/v1/capabilities?provider=tqsdk")
    assert response.status_code == 200
    entry = response.json()["data"][0]
    assert entry["provider"] == "tqsdk"
    assert entry["enabled"] is True
    cap = entry["capabilities"][0]
    assert cap["asset_types"] == ["contract", "continuous", "weighted-index"]
    assert [i["code"] for i in cap["intervals"]] == [
        "PT1M", "PT5M", "PT15M", "PT30M", "PT1H", "P1D",
    ]
    assert cap["adjustments"] == ["none"]
    assert cap["fields"] == ["open", "high", "low", "close", "volume"]


def test_missing_auth_returns_clear_error(default_client) -> None:  # type: ignore[no-untyped-def]
    """未配置快期账户时能力目录正常，数据请求返回明确错误。"""
    caps = default_client.get("/v1/providers/tqsdk/capabilities")
    assert caps.status_code == 200
    assert caps.json()["data"]["enabled"] is True

    response = default_client.get("/v1/market/bars", params=BASE_PARAMS)
    assert response.status_code == 501
    error = response.json()["error"]
    assert error["code"] == "PROVIDER_NOT_IMPLEMENTED"
    assert error["details"]["settings"] == [
        "TAPEHUB_TQSDK_AUTH_USER",
        "TAPEHUB_TQSDK_AUTH_PASSWORD",
    ]
    assert error["details"]["missing"] == ["user", "password"]


def test_unsupported_stock(tqsdk_client: TestClient) -> None:  # type: ignore[no-untyped-def]
    # TqSdk 仅声明期货行情：股票请求在能力目录即拒绝
    params = {
        **BASE_PARAMS,
        "asset_type": "stock",
        "instrument": "CN-SSE-600000",
    }
    response = tqsdk_client.get("/v1/market/bars", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_CAPABILITY"


def test_weighted_index_chain(
    tqsdk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    preset_rows(monkeypatch, klines_rows(["2026-01-02"]))
    params = {
        "provider": "tqsdk",
        "asset_type": "weighted-index",
        "instrument": "CN-SHFE-bu",
        "interval": "P1D",
        "start": "2026-01-01",
        "end": "2026-01-10",
    }
    response = tqsdk_client.get("/v1/market/bars", params=params)
    assert response.status_code == 200
    assert StubTqApi.instances[-1].calls[0]["symbol"] == "KQ.i@SHFE.bu"
