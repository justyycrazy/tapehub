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
from tests.conftest import make_settings
from tests.fixtures.providers.stub_akshare import (
    StubAkshare,
    em_etf_rows,
    em_global_rows,
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
    settings = make_settings(max_limit=1000, catalog_version="test")
    return TestClient(create_app(settings=settings, registry=build_default_registry(settings)))


BASE_PARAMS = {
    "provider": "akshare",
    "asset_type": "stock",
    "instrument": "CN-SSE-600000",
    "interval": "P1D",
    # 时间边界必须带时区偏移；CN 标的统一用北京时间表达
    "start": "2026-01-01T00:00:00+08:00",
    "end": "2026-01-10T00:00:00+08:00",
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

    # 未传递 variant → 按 default_variant 解析为 eastmoney；未传递 adjust → 股票默认 qfq
    operation, kwargs = akshare_stub.calls[0]
    assert operation == "stock_zh_a_hist"
    assert kwargs["adjust"] == "qfq"
    assert kwargs["period"] == "daily"
    assert body["meta"]["variant"] == "eastmoney"

    data = body["data"]
    assert data["instrument"] == "CN-SSE-600000"
    assert data["interval"] == "P1D"
    assert len(data["bars"]) == 10
    timestamps = [bar["timestamp"] for bar in data["bars"]]
    assert timestamps == sorted(timestamps)
    assert data["bars"][0]["timestamp"] == "2026-01-01T00:00:00+08:00"
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
        "start": "2026-01-01T00:00:00+08:00",
        "end": "2026-01-10T00:00:00+08:00",
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


def test_etf_chain_explicit_provider(
    akshare_client: TestClient, akshare_stub: StubAkshare
) -> None:
    # CN ETF 缺省路由走 baostock（链上 akshare 为末位兜底），本用例显式
    # provider=akshare 走东财 ETF；未传 adjust → ETF 声明默认 none
    akshare_stub.set_rows("fund_etf_hist_em", em_etf_rows(["2026-01-02"]))
    params = {
        "provider": "akshare",
        "asset_type": "etf",
        "instrument": "CN-SSE-510050",
        "interval": "P1D",
        "start": "2026-01-01T00:00:00+08:00",
        "end": "2026-01-10T00:00:00+08:00",
    }
    response = akshare_client.get("/v1/market/bars", params=params)
    assert response.status_code == 200
    body = response.json()

    operation, kwargs = akshare_stub.calls[0]
    assert operation == "fund_etf_hist_em"
    assert kwargs["symbol"] == "510050"
    assert kwargs["adjust"] == ""  # ETF 未传 adjust → 声明默认 none → 不复权
    assert kwargs["period"] == "daily"

    assert body["meta"]["provider"] == "akshare"
    assert body["meta"]["variant"] == "eastmoney"
    bar = body["data"]["bars"][0]
    assert bar["timestamp"] == "2026-01-02T00:00:00+08:00"
    assert bar["volume"] == 2345600.0  # 东财成交量手 → 股
    assert bar["amount"] == 9876543.0


def test_hk_chain_explicit_provider(
    akshare_client: TestClient, akshare_stub: StubAkshare
) -> None:
    # HK 个股缺省路由走 alphafeed/tickflow（均需凭据），akshare 为末位免凭据
    # 兜底，本用例显式 provider=akshare 走东财港股；未传 adjust → stock 默认 qfq
    akshare_stub.set_rows("stock_hk_hist", em_global_rows(["2026-01-02"]))
    params = {
        "provider": "akshare",
        "asset_type": "stock",
        "instrument": "HK-HKEX-00700",
        "interval": "P1D",
        "start": "2026-01-01T00:00:00+08:00",
        "end": "2026-01-10T00:00:00+08:00",
    }
    response = akshare_client.get("/v1/market/bars", params=params)
    assert response.status_code == 200
    body = response.json()

    operation, kwargs = akshare_stub.calls[0]
    assert operation == "stock_hk_hist"
    assert kwargs["symbol"] == "00700"
    assert kwargs["adjust"] == "qfq"  # stock 未传 adjust → 声明默认 qfq
    assert body["meta"]["provider"] == "akshare"
    assert body["meta"]["variant"] == "eastmoney"
    bar = body["data"]["bars"][0]
    assert bar["timestamp"] == "2026-01-02T00:00:00+08:00"  # 港股=北京零点
    assert bar["volume"] == 345678.0  # 成交量原样透出（单位待探针定标）


def test_us_chain_explicit_provider(
    akshare_client: TestClient, akshare_stub: StubAkshare
) -> None:
    # US 个股缺省路由走 twelvedata/alphafeed/tickflow（均需凭据），akshare 为
    # 末位免凭据兜底；美股日线时间戳为美东零点 → 北京 13:00（EST）
    akshare_stub.set_rows("stock_us_hist", em_global_rows(["2026-01-02"]))
    params = {
        "provider": "akshare",
        "asset_type": "stock",
        "instrument": "US-NYSE-GE",
        "interval": "P1D",
        "start": "2026-01-01T00:00:00+08:00",
        "end": "2026-01-10T00:00:00+08:00",
    }
    response = akshare_client.get("/v1/market/bars", params=params)
    assert response.status_code == 200
    body = response.json()

    operation, kwargs = akshare_stub.calls[0]
    assert operation == "stock_us_hist"
    assert kwargs["symbol"] == "106.GE"
    assert kwargs["adjust"] == "qfq"
    bar = body["data"]["bars"][0]
    assert bar["timestamp"] == "2026-01-02T13:00:00+08:00"  # 美东零点（EST）
    assert bar["volume"] == 345678.0


def test_capability_catalog(akshare_client: TestClient) -> None:
    response = akshare_client.get("/v1/capabilities?provider=akshare")
    assert response.status_code == 200
    entry = response.json()["data"][0]
    assert entry["provider"] == "akshare"
    assert entry["enabled"] is True
    assert entry["default_variant"] == {
        "stock": "eastmoney",
        "index": "eastmoney",
        "etf": "eastmoney",
        "contract": "sina",
        "continuous": "sina",
    }
    caps = entry["capabilities"]
    # bars 十一块：个股（CN 东财/新浪/腾讯 + HK/US 东财）、指数（东财/新浪/腾讯）、
    # ETF（东财）、期货合约/主力连续（新浪）
    assert [(cap["data_type"], cap["markets"], cap["asset_types"]) for cap in caps] == [
        ("bars", ["CN"], ["stock"]),
        ("bars", ["CN"], ["stock"]),
        ("bars", ["CN"], ["stock"]),
        ("bars", ["CN"], ["index"]),
        ("bars", ["CN"], ["index"]),
        ("bars", ["CN"], ["index"]),
        ("bars", ["CN"], ["etf"]),
        ("bars", ["HK"], ["stock"]),
        ("bars", ["US"], ["stock"]),
        ("bars", ["CN"], ["contract"]),
        ("bars", ["CN"], ["continuous"]),
    ]
    etf_cap = caps[6]
    assert etf_cap["variants"] == ["eastmoney"]
    assert [i["code"] for i in etf_cap["intervals"]] == ["P1D", "P1W", "P1M"]
    assert etf_cap["adjustments"] == ["none", "qfq", "hfq"]
    # history.start 探针实证回填（报告 20261108T150042+0800，人工确认，
    # approx ±90 天；P1W/P1M 与 P1D 同上游端点同数据区间，同值声明）
    etf_start = "2005-03-20T00:00:00+08:00"
    hk_start = "1989-01-27T00:00:00+08:00"
    us_start = "1986-02-21T00:00:00+08:00"
    assert [i["history"]["start"] for i in etf_cap["intervals"]] == [etf_start] * 3
    hk_cap, us_cap = caps[7], caps[8]
    assert [i["history"]["start"] for i in hk_cap["intervals"]] == [hk_start] * 3
    assert [i["history"]["start"] for i in us_cap["intervals"]] == [us_start] * 3
    for overseas_cap in (hk_cap, us_cap):
        assert overseas_cap["variants"] == ["eastmoney"]
        assert [i["code"] for i in overseas_cap["intervals"]] == ["P1D", "P1W", "P1M"]
        assert overseas_cap["adjustments"] == ["none", "qfq", "hfq"]


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
