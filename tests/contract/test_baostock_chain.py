"""baostock 完整 HTTP 链路契约测试（SDK 用桩 Mock，无真实网络）。

覆盖点：
- CN stock bars 经显式 provider 走 baostock（免费无凭据恒启用），
  未传递 adjust → 股票默认 qfq → adjustflag "2"；
- CN index / CN ETF 经显式 provider 覆盖指数与 ETF 映射（ETF 默认 none）；
- auto 路由按回退链序裁决、停用者跳过的**机制**在
  tests/unit/test_registry_routing.py 用合成配置验证——契约面不绑定
  内置链序（顺序是可调配置）；
- 无凭据 Provider 无 PROVIDER_DISABLED 路径（对比 alphafeed）；
- 能力目录三块结构与 history 未核实（start 为 null）口径。
"""

import sys
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tapehub.api.app import create_app
from tapehub.providers.registry import build_default_registry
from tests.conftest import make_settings
from tests.fixtures.providers.stub_baostock import (
    StubBaostock,
    StubResultData,
    daily_stock_rows,
    minute_rows,
)

BASE_PARAMS = {
    "provider": "baostock",
    "asset_type": "stock",
    "instrument": "CN-SSE-600000",
    "interval": "P1D",
    # 时间边界必须带时区偏移；CN 标的统一用北京时间表达
    "start": "2026-01-01T00:00:00+08:00",
    "end": "2026-01-10T00:00:00+08:00",
}


@pytest.fixture()
def baostock_stub(monkeypatch: pytest.MonkeyPatch) -> StubBaostock:
    stub = StubBaostock()
    monkeypatch.setitem(sys.modules, "baostock", stub)
    return stub


@pytest.fixture()
def baostock_client(baostock_stub: StubBaostock) -> TestClient:
    settings = make_settings(max_limit=1000, catalog_version="test")
    with TestClient(
        create_app(settings=settings, registry=build_default_registry(settings))
    ) as client:
        yield client


def _first_query_kwargs(stub: StubBaostock) -> dict[str, Any]:
    """首个 query_history_k_data_plus 调用参数（calls 前面还有 login 记录）。"""
    for name, kwargs in stub.calls:
        if name == "query_history_k_data_plus":
            return kwargs
    raise AssertionError("no query call recorded")


def _last_query_kwargs(stub: StubBaostock) -> dict[str, Any]:
    """最后一次 query_history_k_data_plus 调用参数。"""
    for name, kwargs in reversed(stub.calls):
        if name == "query_history_k_data_plus":
            return kwargs
    raise AssertionError("no query call recorded")


def test_full_chain_default_adjust_is_qfq(
    baostock_client: TestClient, baostock_stub: StubBaostock
) -> None:
    baostock_stub.result = daily_stock_rows(["2026-01-%02d" % d for d in range(1, 11)])

    response = baostock_client.get("/v1/market/bars", params=BASE_PARAMS)
    assert response.status_code == 200
    body = response.json()

    # 未传递 adjust → 股票默认 qfq → adjustflag "2"；所有参数显式传递
    kwargs = _first_query_kwargs(baostock_stub)
    assert kwargs["code"] == "sh.600000"
    assert kwargs["adjustflag"] == "2"
    assert kwargs["frequency"] == "d"

    data = body["data"]
    assert data["instrument"] == "CN-SSE-600000"
    assert len(data["bars"]) == 10
    timestamps = [bar["timestamp"] for bar in data["bars"]]
    assert timestamps == sorted(timestamps)
    assert data["bars"][0]["timestamp"] == "2026-01-01T00:00:00+08:00"
    assert data["bars"][0]["extensions"]["prev_close"] == 12.65

    meta = body["meta"]
    assert meta["provider"] == "baostock"
    assert meta["variant"] == "baostock"
    assert meta["count"] == 10
    assert meta["request_id"].startswith("req_")


def test_index_explicit_provider(
    baostock_client: TestClient, baostock_stub: StubBaostock
) -> None:
    """CN 指数经显式 provider 走 baostock：sh.000001 映射 + 日频参数。"""
    baostock_stub.result = daily_stock_rows(["2026-01-05"])

    response = baostock_client.get(
        "/v1/market/bars",
        params={
            "provider": "baostock",
            "asset_type": "index",
            "instrument": "CN-SSE-000001",
            "interval": "P1D",
            "start": "2026-01-01T00:00:00+08:00",
            "end": "2026-01-10T00:00:00+08:00",
        },
    )
    assert response.status_code == 200
    assert response.json()["meta"]["provider"] == "baostock"
    kwargs = _first_query_kwargs(baostock_stub)
    assert kwargs["code"] == "sh.000001"
    assert kwargs["frequency"] == "d"


def test_etf_explicit_provider(
    baostock_client: TestClient, baostock_stub: StubBaostock
) -> None:
    """CN ETF 经显式 provider 走 baostock：sh.510050 映射 + 默认 adjust none。"""
    baostock_stub.result = daily_stock_rows(["2026-01-05"])

    response = baostock_client.get(
        "/v1/market/bars",
        params={
            "provider": "baostock",
            "asset_type": "etf",
            "instrument": "CN-SSE-510050",
            "interval": "P1D",
            "start": "2026-01-01T00:00:00+08:00",
            "end": "2026-01-10T00:00:00+08:00",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["provider"] == "baostock"
    # ETF 未传 adjust → 声明默认 none → adjustflag "3"
    kwargs = _first_query_kwargs(baostock_stub)
    assert kwargs["adjustflag"] == "3"
    assert kwargs["code"] == "sh.510050"


def test_minute_chain(baostock_client: TestClient, baostock_stub: StubBaostock) -> None:
    baostock_stub.result = minute_rows([("2026-01-05", "20260105093500000")])

    response = baostock_client.get(
        "/v1/market/bars", params={**BASE_PARAMS, "interval": "PT5M", "adjust": "none"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["bars"][0]["timestamp"] == "2026-01-05T09:35:00+08:00"
    kwargs = _first_query_kwargs(baostock_stub)
    assert kwargs["frequency"] == "5"
    assert kwargs["adjustflag"] == "3"


def test_etf_minute_chain(
    baostock_client: TestClient, baostock_stub: StubBaostock
) -> None:
    """ETF 分钟族链路（探针实证 2026-01-05 起声明）。"""
    baostock_stub.result = minute_rows([("2026-01-05", "20260105093500000")])

    response = baostock_client.get(
        "/v1/market/bars",
        params={**BASE_PARAMS, "asset_type": "etf", "instrument": "CN-SSE-510050",
                "interval": "PT5M", "adjust": "none"},
    )
    assert response.status_code == 200
    assert response.json()["data"]["bars"][0]["timestamp"] == "2026-01-05T09:35:00+08:00"
    kwargs = _first_query_kwargs(baostock_stub)
    assert kwargs["code"] == "sh.510050"
    assert kwargs["frequency"] == "5"


def test_index_minute_unsupported_capability(
    baostock_client: TestClient, baostock_stub: StubBaostock
) -> None:
    # 指数无分钟线：能力目录即拒绝，未触达适配器
    response = baostock_client.get(
        "/v1/market/bars",
        params={**BASE_PARAMS, "asset_type": "index", "instrument": "CN-SSE-000001",
                "interval": "PT5M"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_CAPABILITY"
    assert baostock_stub.calls == []


def test_baostock_always_enabled(baostock_client: TestClient) -> None:
    """免费无凭据 Provider：能力目录 enabled=true，无 PROVIDER_DISABLED 路径。"""
    response = baostock_client.get("/v1/providers/baostock/capabilities")
    assert response.status_code == 200
    assert response.json()["data"]["enabled"] is True


def test_capability_catalog(baostock_client: TestClient) -> None:
    response = baostock_client.get("/v1/capabilities?provider=baostock")
    assert response.status_code == 200
    entry = response.json()["data"][0]
    assert entry["provider"] == "baostock"
    assert entry["enabled"] is True
    assert entry["default_variant"] == {
        "stock": "baostock",
        "index": "baostock",
        "etf": "baostock",
    }
    caps = entry["capabilities"]
    # bars 三块：个股（日/周/月 + 分钟族）、指数（日/周/月）、ETF（日/周/月）
    assert [(cap["data_type"], cap["markets"], cap["asset_types"]) for cap in caps] == [
        ("bars", ["CN"], ["stock"]),
        ("bars", ["CN"], ["index"]),
        ("bars", ["CN"], ["etf"]),
    ]
    stock_cap, index_cap, etf_cap = caps
    assert stock_cap["variants"] == ["baostock"]
    assert [i["code"] for i in stock_cap["intervals"]] == [
        "P1D", "P1W", "P1M", "PT5M", "PT15M", "PT30M", "PT1H",
    ]
    assert stock_cap["adjustments"] == ["none", "qfq", "hfq"]
    assert [i["code"] for i in index_cap["intervals"]] == ["P1D", "P1W", "P1M"]
    assert index_cap["adjustments"] == ["none"]
    assert [i["code"] for i in etf_cap["intervals"]] == [
        "P1D", "P1W", "P1M", "PT5M", "PT15M", "PT30M", "PT1H",
    ]
    # ETF 复权经探针实证生效（2024 全年 qfq/hfq 逐行差异），回填三复权
    assert etf_cap["adjustments"] == ["none", "qfq", "hfq"]
    # history.start 探针实证回填：个股/指数 ≈1991-01-18（二分 90 天粒度 approx）、
    # 分钟族 2020-01-02（精确）、
    # ETF 日/周/月 2005-02-23（精确）、ETF 分钟族 2026-01-05（精确）
    daily_start = "1991-01-18T00:00:00+08:00"
    minute_start = "2020-01-02T00:00:00+08:00"
    etf_start = "2005-02-23T00:00:00+08:00"
    etf_minute_start = "2026-01-05T00:00:00+08:00"
    stock_starts = [i["history"]["start"] for i in stock_cap["intervals"]]
    assert stock_starts == [daily_start] * 3 + [minute_start] * 4
    assert [i["history"]["start"] for i in index_cap["intervals"]] == [daily_start] * 3
    etf_starts = [i["history"]["start"] for i in etf_cap["intervals"]]
    assert etf_starts == [etf_start] * 3 + [etf_minute_start] * 4


def test_etf_adjust_qfq_chain(
    baostock_client: TestClient, baostock_stub: StubBaostock
) -> None:
    """ETF 复权链路：显式 adjust=qfq → adjustflag "2"（探针实证生效后回填）。"""
    baostock_stub.result = daily_stock_rows(["2026-01-05"])

    response = baostock_client.get(
        "/v1/market/bars",
        params={**BASE_PARAMS, "asset_type": "etf", "instrument": "CN-SSE-510050",
                "adjust": "qfq"},
    )
    assert response.status_code == 200
    assert _first_query_kwargs(baostock_stub)["adjustflag"] == "2"
    assert baostock_client.get(
        "/v1/market/bars",
        params={**BASE_PARAMS, "asset_type": "etf", "instrument": "CN-SSE-510050",
                "adjust": "hfq"},
    ).status_code == 200
    assert _last_query_kwargs(baostock_stub)["adjustflag"] == "1"


def test_upstream_error_wrapped(
    baostock_client: TestClient, baostock_stub: StubBaostock
) -> None:
    baostock_stub.result = StubResultData(
        [], [], error_code="10004011", error_msg="无效的证券代码"
    )
    response = baostock_client.get("/v1/market/bars", params=BASE_PARAMS)
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "UPSTREAM_ERROR"
    assert "无效的证券代码" not in error["message"]  # 上游错误细节不经 message 泄露


def test_sdk_exception_wrapped(
    baostock_client: TestClient, baostock_stub: StubBaostock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(**kwargs: Any) -> None:
        raise RuntimeError("upstream boom")

    monkeypatch.setattr(baostock_stub, "query_history_k_data_plus", boom)
    response = baostock_client.get("/v1/market/bars", params=BASE_PARAMS)
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "UPSTREAM_ERROR"
    assert "boom" not in error["message"]  # 不泄露上游异常细节
