"""TqSdk 完整 HTTP 链路契约测试（SDK 用桩 Mock，无真实网络）。"""


import pytest
from fastapi.testclient import TestClient

from tapehub.api.app import create_app
from tapehub.providers.registry import build_default_registry
from tests.conftest import make_settings
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
    # 显式传 variant=tqsdk（缺省时按 default_variant 解析为同名变体）
    "variant": "tqsdk",
}


@pytest.fixture()
def tqsdk_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    install(monkeypatch)
    settings = make_settings(
        max_limit=1000, tqsdk_auth_user="user@qq.com", tqsdk_auth_password="secret"
    )
    return TestClient(create_app(settings=settings, registry=build_default_registry(settings)))


def test_full_chain_continuous(
    tqsdk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """主力连续链路：KQ.m@ 符号 + 无成交额字段。"""
    preset_rows(monkeypatch, klines_rows(["2026-01-02", "2026-01-05"]))

    params = {
        **BASE_PARAMS,
        "asset_type": "continuous",
        "instrument": "CN-SHFE-cu",
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
    # Provider × 资产类型级缺省变体在能力目录透出（标量声明 → 覆盖全部资产类型）
    assert entry["default_variant"] == {
        "contract": "tqsdk",
        "continuous": "tqsdk",
        "weighted-index": "tqsdk",
        "stock": "tqsdk",
        "index": "tqsdk",
        "etf": "tqsdk",
    }
    assert entry["default_provider"] == [
        "contract",
        "continuous",
        "weighted-index",
        "etf",
    ]
    assert len(entry["capabilities"]) == 6  # 期货三类拆分 + 股票/指数/ETF
    cap = entry["capabilities"][0]
    assert cap["asset_types"] == ["contract"]
    assert cap["variants"] == ["tqsdk"]
    assert [i["code"] for i in cap["intervals"]] == [
        "PT1M", "PT5M", "PT15M", "PT30M", "PT1H", "P1D",
    ]
    assert cap["adjustments"] == ["none"]
    assert cap["fields"] == ["open", "high", "low", "close", "volume"]
    # 探针回填的 history.start 在能力目录可见（continuous 日线）
    continuous_cap = next(
        c for c in entry["capabilities"] if c["asset_types"] == ["continuous"]
    )
    p1d = next(i for i in continuous_cap["intervals"] if i["code"] == "P1D")
    assert p1d["history"]["start"] == "2016-01-04T16:00:00Z"


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


def test_unsupported_option(tqsdk_client: TestClient) -> None:  # type: ignore[no-untyped-def]
    # TqSdk 未声明期权：请求在能力目录即拒绝
    params = {
        **BASE_PARAMS,
        "asset_type": "option",
        "instrument": "CN-SSE-10004512",
    }
    response = tqsdk_client.get("/v1/market/bars", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_CAPABILITY"


def test_weighted_index_chain(
    tqsdk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    preset_rows(monkeypatch, klines_rows(["2026-01-02"]))
    params = {
        **BASE_PARAMS,
        "asset_type": "weighted-index",
        "instrument": "CN-SHFE-bu",
    }
    response = tqsdk_client.get("/v1/market/bars", params=params)
    assert response.status_code == 200
    assert StubTqApi.instances[-1].calls[0]["symbol"] == "KQ.i@SHFE.bu"


def test_default_variant_routes_to_declared_default(
    tqsdk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缺省 variant：按 manifest default_variant（tqsdk）具体化并回显 meta。"""
    preset_rows(monkeypatch, klines_rows(["2026-01-02"]))

    params = {k: v for k, v in BASE_PARAMS.items() if k != "variant"}
    response = tqsdk_client.get("/v1/market/bars", params=params)
    assert response.status_code == 200
    assert response.json()["meta"]["variant"] == "tqsdk"


def test_stock_chain_with_qfq_adjust(
    tqsdk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """股票链路：SSE.600000 代码映射 + adj_type 前复权（需专业版权限，探针实证）。"""
    preset_rows(monkeypatch, klines_rows(["2026-01-02", "2026-01-05"]))

    params = {
        **BASE_PARAMS,
        "asset_type": "stock",
        "instrument": "CN-SSE-600000",
        "adjust": "qfq",
    }
    response = tqsdk_client.get("/v1/market/bars", params=params)
    assert response.status_code == 200

    api = StubTqApi.instances[-1]
    assert api.calls[0]["symbol"] == "SSE.600000"
    assert api.calls[0]["adj_type"] == "F"

    data = response.json()["data"]
    # 股票行无持仓量列 → extensions 缺省为 null，不编造
    assert data["bars"][0]["amount"] is None


def test_etf_default_routes_to_declared_default_provider(
    tqsdk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ETF 是 tqsdk 声明的缺省资产类型：auto 路由命中并回显 meta.variant。"""
    preset_rows(monkeypatch, klines_rows(["2026-01-02"]))

    params = {
        **BASE_PARAMS,
        "asset_type": "etf",
        "instrument": "CN-SSE-510050",
    }
    params.pop("provider")
    response = tqsdk_client.get("/v1/market/bars", params=params)
    assert response.status_code == 200
    meta = response.json()["meta"]
    assert meta["provider"] == "tqsdk"
    assert meta["variant"] == "tqsdk"
    assert StubTqApi.instances[-1].calls[0]["symbol"] == "SSE.510050"
