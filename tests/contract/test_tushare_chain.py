"""Tushare 完整 HTTP 链路契约测试（SDK 用桩 Mock，无真实网络）。"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tapehub.api.app import create_app
from tapehub.providers.registry import build_default_registry
from tests.conftest import make_settings
from tests.fixtures.providers.stub_tushare import futures_rows, install, stock_rows

BASE_PARAMS = {
    "provider": "tushare",
    "asset_type": "stock",
    "instrument": "CN-SSE-600000",
    "interval": "P1D",
    "start": "2026-01-01T00:00:00+08:00",
    "end": "2026-01-10T00:00:00+08:00",
    # 显式传 variant=tushare（缺省时按 default_variant 解析为同名变体）
    "variant": "tushare",
}


def preset_rows(module: Any, rows: list[dict[str, Any]]) -> None:
    module.pro_bar_rows = rows


@pytest.fixture()
def tushare_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Any]:
    stub = install(monkeypatch)
    settings = make_settings(max_limit=1000, tushare_token="test-token")
    with TestClient(
        create_app(settings=settings, registry=build_default_registry(settings))
    ) as client:
        yield client, stub


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
    assert data["bars"][0]["timestamp"] == "2026-01-02T00:00:00+08:00"
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
        **BASE_PARAMS,
        "asset_type": "contract",
        "instrument": "CN-SHFE-CU2401",
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


@pytest.mark.parametrize(
    ("code", "expected_ts_code"),
    [
        # CZCE 与统一标准码同为 4 位年月，直通（探针实证 4 位可用、3 位旧写法全空）
        ("CN-CZCE-RM2701", "RM2701.ZCE"),
        # 其余四所后缀直拼（探针实证）
        ("CN-DCE-V2601", "V2601.DCE"),
    ],
)
def test_futures_chain_exchange_suffixes(
    code: str,
    expected_ts_code: str,
    monkeypatch: pytest.MonkeyPatch,
    tushare_client: tuple[TestClient, Any],
) -> None:
    """扩容后的期货交易所后缀贯穿 HTTP 全链路（CZCE 4 位年月码直通）。"""
    http, stub = tushare_client
    stub.set_rows("fut_daily", futures_rows(["2026-01-02"]))

    params = {**BASE_PARAMS, "asset_type": "contract", "instrument": code}
    response = http.get("/v1/market/bars", params=params)
    assert response.status_code == 200
    operation, kwargs = stub.last_pro.calls[0]
    assert operation == "fut_daily"
    assert kwargs["ts_code"] == expected_ts_code
    assert response.json()["data"]["instrument"] == code


def test_continuous_chain(
    monkeypatch: pytest.MonkeyPatch, tushare_client: tuple[TestClient, Any]
) -> None:
    """主力连续贯穿全链路：品种+L 主连（CFFEX 主力直拼，映射见 request_mapper）。"""
    http, stub = tushare_client
    stub.set_rows("fut_daily", futures_rows(["2026-01-02"]))

    params = {**BASE_PARAMS, "asset_type": "continuous", "instrument": "CN-SHFE-CU"}
    response = http.get("/v1/market/bars", params=params)
    assert response.status_code == 200
    body = response.json()
    operation, kwargs = stub.last_pro.calls[0]
    assert operation == "fut_daily"
    assert kwargs["ts_code"] == "CUL.SHF"
    assert body["data"]["instrument"] == "CN-SHFE-CU"
    assert body["data"]["bars"][0]["volume"] == 54321.0  # 期货保持手


def test_missing_token_returns_clear_error(default_client) -> None:  # type: ignore[no-untyped-def]
    """未配置 Token 时：Provider 不启用（能力目录可见），显式请求返回明确错误。"""
    caps = default_client.get("/v1/providers/tushare/capabilities")
    assert caps.status_code == 200
    assert caps.json()["data"]["enabled"] is False

    response = default_client.get("/v1/market/bars", params={**BASE_PARAMS, "provider": "tushare"})
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "PROVIDER_DISABLED"
    assert "TAPEHUB_TUSHARE_TOKEN" in error["details"]["disabled_reason"]


def test_capability_catalog(tushare_client: tuple[TestClient, Any]) -> None:  # type: ignore[no-untyped-def]
    http, _ = tushare_client
    response = http.get("/v1/capabilities?provider=tushare")
    assert response.status_code == 200
    entry = response.json()["data"][0]
    assert entry["provider"] == "tushare"
    assert entry["enabled"] is True
    assert len(entry["capabilities"]) == 3
    stock_cap = next(cap for cap in entry["capabilities"] if cap["asset_types"] == ["stock"])
    assert stock_cap["variants"] == ["tushare"]
    assert [i["code"] for i in stock_cap["intervals"]] == ["P1D", "P1W", "P1M"]
    assert stock_cap["adjustments"] == ["none", "qfq", "hfq"]
    contract_cap = next(cap for cap in entry["capabilities"] if cap["asset_types"] == ["contract"])
    assert [i["code"] for i in contract_cap["intervals"]] == ["P1D"]
    assert contract_cap["adjustments"] == ["none"]
    continuous_cap = next(
        cap for cap in entry["capabilities"] if cap["asset_types"] == ["continuous"]
    )
    assert [i["code"] for i in continuous_cap["intervals"]] == ["P1D"]
    assert continuous_cap["adjustments"] == ["none"]
    # history.start 探针回填：主连二分下限触底
    assert continuous_cap["intervals"][0]["history"]["start"] == "1991-01-01T00:00:00+08:00"


def test_weekly_interval(
    tushare_client: tuple[TestClient, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    http, stub = tushare_client
    preset_rows(stub, stock_rows(["2026-01-05"]))

    params = {**BASE_PARAMS, "interval": "P1W"}
    response = http.get("/v1/market/bars", params=params)
    assert response.status_code == 200
    assert stub.pro_bar_calls[0]["freq"] == "W"


def test_default_variant_routes_to_declared_default(
    tushare_client: tuple[TestClient, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """缺省 variant：按 manifest default_variant（tushare）具体化并回显 meta。"""
    http, stub = tushare_client
    preset_rows(stub, stock_rows(["2026-01-02"]))

    params = {k: v for k, v in BASE_PARAMS.items() if k != "variant"}
    response = http.get("/v1/market/bars", params=params)
    assert response.status_code == 200
    assert response.json()["meta"]["variant"] == "tushare"
