"""Tushare 完整 HTTP 链路契约测试（SDK 用桩 Mock，无真实网络）。"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tapehub.api.app import create_app
from tapehub.providers.registry import build_default_registry
from tapehub.settings import Settings
from tests.fixtures.providers.stub_tushare import futures_rows, install, stock_rows

BASE_PARAMS = {
    "provider": "tushare",
    "asset_type": "stock",
    "instrument": "CN-SSE-600000",
    "interval": "P1D",
    "start": "2026-01-01",
    "end": "2026-01-10",
}


def preset_rows(module: Any, rows: list[dict[str, Any]]) -> None:
    module.pro_bar_rows = rows


@pytest.fixture()
def tushare_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Any]:
    stub = install(monkeypatch)
    settings = Settings(max_limit=1000, tushare_token="test-token")
    client = TestClient(create_app(settings=settings, registry=build_default_registry(settings)))
    return client, stub


def test_full_chain_default_adjust_is_qfq(
    monkeypatch: pytest.MonkeyPatch, tushare_client: tuple[TestClient, Any]
) -> None:
    http, stub = tushare_client
    preset_rows(stub, stock_rows(["2026-01-%02d" % d for d in range(2, 10)]))

    response = http.get("/v1/market/bars", params=BASE_PARAMS)
    assert response.status_code == 200
    body = response.json()

    kwargs = stub.pro_bar_calls[0]
    # 未传递 adjust → 股票默认 qfq；所有参数显式传递
    assert kwargs["ts_code"] == "600000.SH"
    assert kwargs["adj"] == "qfq"
    assert kwargs["freq"] == "D"

    data = body["data"]
    assert data["instrument"] == "CN-SSE-600000"
    assert len(data["bars"]) == 8
    timestamps = [bar["timestamp"] for bar in data["bars"]]
    assert timestamps == sorted(timestamps)
    assert data["bars"][0]["timestamp"] == "2026-01-02T00:00:00Z"
    # 手→股、千元→元
    assert data["bars"][0]["volume"] == 1234500.0
    assert data["bars"][0]["amount"] == 12345000.0

    meta = body["meta"]
    assert meta["provider"] == "tushare"
    assert meta["count"] == 8
    assert meta["partial"] is False


def test_futures_chain(
    monkeypatch: pytest.MonkeyPatch, tushare_client: tuple[TestClient, Any]
) -> None:
    http, stub = tushare_client
    stub.set_rows("fut_daily", futures_rows(["2026-01-02"]))

    params = {
        "provider": "tushare",
        "asset_type": "contract",
        "instrument": "CN-SHFE-CU2401",
        "interval": "P1D",
        "start": "2026-01-01",
        "end": "2026-01-10",
    }
    response = http.get("/v1/market/bars", params=params)
    assert response.status_code == 200
    body = response.json()
    operation, kwargs = stub.last_pro.calls[0]
    assert operation == "fut_daily"
    assert kwargs["ts_code"] == "CU2401.SHF"
    bar = body["data"]["bars"][0]
    assert bar["close"] == 68300.0
    assert bar["volume"] == 54321.0  # 期货保持手
    assert bar["extensions"]["settle"] == 68250.0


def test_missing_token_returns_clear_error(default_client) -> None:  # type: ignore[no-untyped-def]
    """未配置 Token 时能力目录正常，数据请求返回明确错误。"""
    caps = default_client.get("/v1/providers/tushare/capabilities")
    assert caps.status_code == 200
    assert caps.json()["data"]["enabled"] is True

    response = default_client.get("/v1/market/bars", params=BASE_PARAMS)
    assert response.status_code == 501
    error = response.json()["error"]
    assert error["code"] == "PROVIDER_NOT_IMPLEMENTED"
    assert error["details"]["setting"] == "TAPEHUB_TUSHARE_TOKEN"


def test_capability_catalog(tushare_client: tuple[TestClient, Any]) -> None:  # type: ignore[no-untyped-def]
    http, _ = tushare_client
    response = http.get("/v1/capabilities?provider=tushare")
    assert response.status_code == 200
    entry = response.json()["data"][0]
    assert entry["provider"] == "tushare"
    assert entry["enabled"] is True
    assert len(entry["capabilities"]) == 2
    stock_cap = next(cap for cap in entry["capabilities"] if cap["asset_types"] == ["stock"])
    assert [i["code"] for i in stock_cap["intervals"]] == ["P1D", "P1W", "P1M"]
    assert stock_cap["adjustments"] == ["none", "qfq", "hfq"]
    contract_cap = next(cap for cap in entry["capabilities"] if cap["asset_types"] == ["contract"])
    assert [i["code"] for i in contract_cap["intervals"]] == ["P1D"]
    assert contract_cap["adjustments"] == ["none"]


def test_weekly_interval(
    tushare_client: tuple[TestClient, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    http, stub = tushare_client
    preset_rows(stub, stock_rows(["2026-01-05"]))

    params = {**BASE_PARAMS, "interval": "P1W"}
    response = http.get("/v1/market/bars", params=params)
    assert response.status_code == 200
    assert stub.pro_bar_calls[0]["freq"] == "W"
