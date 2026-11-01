"""akshare 完整 HTTP 链路契约测试（SDK 用桩 Mock，无真实网络）。

同时覆盖「未传递参数的声明默认值」行为：
- 股票未传递 adjust → 默认 qfq（前复权）；
- 未传递 variant → 默认 eastmoney（东方财富）。
"""

import sys
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tapehub.api.app import create_app
from tapehub.providers.registry import build_default_registry
from tapehub.settings import Settings
from tests.fixtures.providers.stub_akshare import (
    StubAkshare,
    em_stock_rows,
    sina_index_rows,
    stock_rows_en,
)


@pytest.fixture()
def akshare_stub(monkeypatch: pytest.MonkeyPatch) -> StubAkshare:
    stub = StubAkshare()
    monkeypatch.setitem(sys.modules, "akshare", stub)
    return stub


@pytest.fixture()
def akshare_client(akshare_stub: StubAkshare) -> TestClient:
    settings = Settings(max_limit=1000, catalog_version="test")
    return TestClient(create_app(settings=settings, registry=build_default_registry(settings)))


BASE_PARAMS = {
    "provider": "akshare",
    "asset_type": "stock",
    "instrument": "CN-SSE-600000",
    "interval": "P1D",
    "start": "2026-01-01",
    "end": "2026-01-10",
}


def test_full_chain_default_variant_and_adjust(
    akshare_client: TestClient, akshare_stub: StubAkshare
) -> None:
    akshare_stub.set_rows("stock_zh_a_hist", em_stock_rows(
        ["2026-01-%02d" % d for d in range(1, 11)]
    ))

    response = akshare_client.get("/v1/market/bars", params=BASE_PARAMS)
    assert response.status_code == 200
    body = response.json()

    # 未传递 variant → 默认 eastmoney；未传递 adjust → 股票默认 qfq
    operation, kwargs = akshare_stub.calls[0]
    assert operation == "stock_zh_a_hist"
    assert kwargs["adjust"] == "qfq"
    assert kwargs["period"] == "daily"

    data = body["data"]
    assert data["instrument"] == "CN-SSE-600000"
    assert data["interval"] == "P1D"
    assert len(data["bars"]) == 10
    timestamps = [bar["timestamp"] for bar in data["bars"]]
    assert timestamps == sorted(timestamps)
    assert data["bars"][0]["timestamp"] == "2026-01-01T00:00:00Z"
    # 东财成交量手 → 股
    assert data["bars"][0]["volume"] == 1234500.0

    meta = body["meta"]
    assert meta["provider"] == "akshare"
    assert meta["count"] == 10
    assert meta["request_id"].startswith("req_")


def test_explicit_variant_sina(
    akshare_client: TestClient, akshare_stub: StubAkshare
) -> None:
    akshare_stub.set_rows("stock_zh_a_daily", stock_rows_en(["2026-01-02"]))

    params = {**BASE_PARAMS, "variant": "sina", "adjust": "none"}
    response = akshare_client.get("/v1/market/bars", params=params)
    assert response.status_code == 200

    operation, kwargs = akshare_stub.calls[0]
    assert operation == "stock_zh_a_daily"
    assert kwargs["symbol"] == "sh600000"
    assert kwargs["adjust"] == ""  # adjust=none 显式映射为不复权


def test_index_chain(akshare_client: TestClient, akshare_stub: StubAkshare) -> None:
    akshare_stub.set_rows("stock_zh_index_daily", sina_index_rows(["2026-01-02"]))

    params = {
        "provider": "akshare",
        "asset_type": "index",
        "instrument": "CN-SSE-000001",
        "interval": "P1D",
        "start": "2026-01-01",
        "end": "2026-01-10",
        "variant": "sina",
    }
    response = akshare_client.get("/v1/market/bars", params=params)
    assert response.status_code == 200
    body = response.json()
    # 指数未传递 adjust → 默认 none；新浪指数无成交额
    bar = body["data"]["bars"][0]
    assert bar["close"] == 3005.0
    assert bar["amount"] is None
    assert bar["volume"] == 1.0


def test_unsupported_variant_capability(
    akshare_client: TestClient, akshare_stub: StubAkshare
) -> None:
    # 新浪个股仅日线：周线 + sina 在能力目录即不支持
    params = {**BASE_PARAMS, "variant": "sina", "interval": "P1W"}
    response = akshare_client.get("/v1/market/bars", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_CAPABILITY"
    assert akshare_stub.calls == []  # 未触达适配器


def test_stock_default_adjust_is_qfq_via_capability(
    akshare_client: TestClient, akshare_stub: StubAkshare
) -> None:
    # 默认 qfq 驱动路由：tencent 个股同样支持 qfq，请求成功
    akshare_stub.set_rows("stock_zh_a_hist_tx", stock_rows_en(["2026-01-02"]))
    params = {**BASE_PARAMS, "variant": "tencent"}
    response = akshare_client.get("/v1/market/bars", params=params)
    assert response.status_code == 200
    _, kwargs = akshare_stub.calls[0]
    assert kwargs["adjust"] == "qfq"


def test_unsupported_instrument_error_mapping(
    akshare_client: TestClient, akshare_stub: StubAkshare
) -> None:
    # HKEX 不在已核实映射内：二级路由之后由适配器返回 UNSUPPORTED_CAPABILITY
    params = {**BASE_PARAMS, "instrument": "CN-HKEX-00700"}
    response = akshare_client.get("/v1/market/bars", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_CAPABILITY"


def test_upstream_exception_wrapped(
    akshare_client: TestClient, akshare_stub: StubAkshare, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(**kwargs: Any) -> None:
        raise RuntimeError("upstream boom")

    monkeypatch.setattr(akshare_stub, "stock_zh_a_hist", boom)
    response = akshare_client.get("/v1/market/bars", params=BASE_PARAMS)
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "UPSTREAM_ERROR"
    assert "boom" not in error["message"]  # 不泄露上游异常细节
