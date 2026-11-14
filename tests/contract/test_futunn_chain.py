"""futunn 完整 HTTP 链路契约测试（HTTP 用桩 Mock，签名走真实 cryptography，无真实网络）。

auto 路由说明：futunn 置于各组合回退链尾，auto（无 provider 参数）请求会被
链上免凭据 Provider（baostock/akshare 等）截胡，故显式传 provider/variant=futunn
定向路由；凭据缺失时显式请求返回 PROVIDER_DISABLED（409）。
"""

import pytest
from fastapi.testclient import TestClient

from tapehub.api.app import create_app
from tapehub.providers.registry import build_default_registry
from tests.conftest import make_settings
from tests.fixtures.providers.stub_futunn import (
    envelope,
    install,
    kline_page,
    preset_payload,
    quote_payload,
    quote_row,
    write_test_rsa_key,
)

# 北京零点毫秒戳：2026-01-01 / 01-02；快照行情时间 2026-01-01T15:00+08:00
DAY1_MS = 1767196800000
DAY2_MS = 1767283200000
QUOTE_TIME_MS = 1767250800000

STOCK_PARAMS = {
    "provider": "futunn",
    "asset_type": "stock",
    "instrument": "CN-SSE-600000",
    "interval": "P1D",
    "start": "2026-01-01T00:00:00+08:00",
    "end": "2026-01-10T00:00:00+08:00",
    "adjust": "none",
    # 显式传 variant=futunn（auto 链中免凭据 Provider 会截胡，见模块 docstring）
    "variant": "futunn",
}

STOCK_QUOTE_PARAMS = {
    "provider": "futunn",
    "asset_type": "stock",
    "instrument": "CN-SSE-600000",
    "variant": "futunn",
}


@pytest.fixture()
def futunn_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> TestClient:
    install(monkeypatch)  # REST 桩（bars + quotes）
    key_path, _ = write_test_rsa_key(tmp_path_factory.mktemp("futunn-chain-keys"))
    settings = make_settings(
        max_limit=1000,
        futunn_app_key="test-app-key",
        futunn_private_key_path=key_path,
    )
    with TestClient(
        create_app(settings=settings, registry=build_default_registry(settings))
    ) as client:
        yield client


def test_stock_bars_chain(
    futunn_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    preset_payload(monkeypatch, kline_page([DAY1_MS, DAY2_MS]))

    response = futunn_client.get("/v1/market/bars", params=STOCK_PARAMS)
    assert response.status_code == 200
    body = response.json()

    data = body["data"]
    assert data["instrument"] == "CN-SSE-600000"
    assert data["asset_type"] == "stock"
    assert data["interval"] == "P1D"
    assert len(data["bars"]) == 2
    timestamps = [bar["timestamp"] for bar in data["bars"]]
    assert timestamps == sorted(timestamps)  # 统一升序
    # 北京零点毫秒戳 → 全市场统一输出北京时间
    assert data["bars"][0]["timestamp"] == "2026-01-01T00:00:00+08:00"
    assert data["bars"][0]["close"] == 10.5
    assert data["bars"][0]["volume"] == 1000.0
    assert data["bars"][0]["amount"] == 10500.0

    meta = body["meta"]
    assert meta["provider"] == "futunn"
    assert meta["variant"] == "futunn"
    assert meta["count"] == 2
    assert meta["partial"] is False
    assert body["warnings"] == []


def test_stock_quote_chain(
    futunn_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    preset_payload(monkeypatch, quote_payload([quote_row("SH.600000", QUOTE_TIME_MS)]))

    response = futunn_client.get("/v1/market/quotes", params=STOCK_QUOTE_PARAMS)
    assert response.status_code == 200
    body = response.json()

    data = body["data"]
    assert data["instrument"] == "CN-SSE-600000"
    assert data["asset_type"] == "stock"
    quote = data["quote"]
    assert quote["last_price"] == 100.0
    assert quote["volume"] == 12345.0
    assert quote["amount"] == 1234500.0
    assert quote["bid_price1"] is None  # 上游轻量快照无盘口五档
    assert quote["extensions"]["prev_close_price"] == 99.5  # 白名单透出
    assert quote["extensions"]["suspension"] is False
    # data_time 毫秒戳 → 北京时间
    assert quote["timestamp"] == "2026-01-01T15:00:00+08:00"

    meta = body["meta"]
    assert meta["provider"] == "futunn"
    assert meta["variant"] == "futunn"
    assert meta["received_at"].endswith("+08:00")


def test_capability_catalog(futunn_client: TestClient) -> None:
    response = futunn_client.get("/v1/capabilities?provider=futunn")
    assert response.status_code == 200
    entry = response.json()["data"][0]
    assert entry["provider"] == "futunn"
    assert entry["enabled"] is True
    # 单体系 Provider：标量声明 → 覆盖全部声明资产类型
    assert entry["default_variant"] == {"stock": "futunn", "index": "futunn", "etf": "futunn"}
    assert len(entry["capabilities"]) == 10  # bars 5 块 + quotes 5 块

    bars_caps = [c for c in entry["capabilities"] if c["data_type"] == "bars"]
    assert {tuple(cap["asset_types"])[0] for cap in bars_caps} == {"stock", "index", "etf"}
    markets = {
        tuple(cap["asset_types"])[0]: tuple(cap["markets"])[0] for cap in bars_caps
    }
    assert markets == {"stock": "US", "index": "CN", "etf": "CN"}  # US/HK stock 覆盖
    for cap in bars_caps:
        assert cap["variants"] == ["futunn"]
        # 8 核心周期全量声明
        assert [i["code"] for i in cap["intervals"]] == [
            "PT1M", "PT5M", "PT15M", "PT30M", "PT1H", "P1D", "P1W", "P1M",
        ]
        assert cap["fields"] == ["open", "high", "low", "close", "volume", "amount"]

    # history.start（2026-11-14 depth 探针实证回填）：CN stock 日/周/月
    # ≈1990-12-19（上市日锚）、分钟族 ≈2014-01-21（approx 90 天粒度）；
    # 其余组合（index/etf/HK/US）未单独 depth，保持 null 未核实
    by_key = {
        (tuple(cap["markets"])[0], tuple(cap["asset_types"])[0]): cap
        for cap in bars_caps
    }
    stock_history = {
        i["code"]: i["history"] for i in by_key[("CN", "stock")]["intervals"]
    }
    for code in ("PT1M", "PT5M", "PT15M", "PT30M", "PT1H"):
        assert stock_history[code]["start"] == "2014-01-21T09:45:00+08:00"
        assert stock_history[code]["end"] is None
        assert stock_history[code]["retention_days"] is None
    for code in ("P1D", "P1W", "P1M"):
        assert stock_history[code]["start"] == "1990-12-19T00:00:00+08:00"
    for key in (("CN", "index"), ("CN", "etf"), ("HK", "stock"), ("US", "stock")):
        for interval_entry in by_key[key]["intervals"]:
            assert interval_entry["history"] == {
                "start": None,
                "end": None,
                "retention_days": None,
            }

    adjustments = {
        (tuple(cap["markets"])[0], tuple(cap["asset_types"])[0]): cap["adjustments"]
        for cap in bars_caps
    }
    assert adjustments[("CN", "stock")] == ["none", "qfq", "hfq"]
    assert adjustments[("HK", "stock")] == ["none", "qfq", "hfq"]
    assert adjustments[("US", "stock")] == ["none", "qfq", "hfq"]
    assert adjustments[("CN", "index")] == ["none"]  # 指数无复权语义
    # ETF autype 生效性探针实证（2026-11-14：510300 日线 2024 全年三值
    # 242/242 close 全差异，人工确认扩三值）
    assert adjustments[("CN", "etf")] == ["none", "qfq", "hfq"]

    # 实时快照：无周期/复权维度（显式空），无盘口五档
    quote_caps = [c for c in entry["capabilities"] if c["data_type"] == "quotes"]
    assert {tuple(cap["asset_types"])[0] for cap in quote_caps} == {"stock", "index", "etf"}
    for cap in quote_caps:
        assert cap["intervals"] == []
        assert cap["adjustments"] == []
        assert cap["fields"] == ["timestamp", "last_price", "volume", "amount"]


def test_unsupported_interval_p2h_422(futunn_client: TestClient) -> None:
    """PT2H 不在声明周期面（8 核心周期）：请求返回 422。"""
    params = {**STOCK_PARAMS, "interval": "PT2H"}
    response = futunn_client.get("/v1/market/bars", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_CAPABILITY"


def test_unverified_etf_bse_422(futunn_client: TestClient) -> None:
    """上游品类表 BJ 无 ETF：还原表只收文档实证条目，明确拒绝而非透传上游。"""
    params = {**STOCK_PARAMS, "asset_type": "etf", "instrument": "CN-BSE-430047"}
    response = futunn_client.get("/v1/market/bars", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_CAPABILITY"


def test_us_otc_422(futunn_client: TestClient) -> None:
    """美股 OTC 上游不支持获取：exchange 白名单拒绝。"""
    params = {**STOCK_PARAMS, "instrument": "US-OTC-PINK"}
    response = futunn_client.get("/v1/market/bars", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_CAPABILITY"


def test_upstream_business_error_wrapped(
    futunn_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """信封非 0 业务码（-7 invalid_symbol）→ UPSTREAM_ERROR 502，SDK 异常不穿透。"""
    preset_payload(monkeypatch, envelope(None, ret_code=-7, ret_msg="invalid_symbol"))
    response = futunn_client.get("/v1/market/bars", params=STOCK_PARAMS)
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "UPSTREAM_ERROR"


def test_upstream_rate_limited_wrapped(
    futunn_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ret_code=-11（rate_limited）→ UPSTREAM_RATE_LIMITED 429。"""
    preset_payload(monkeypatch, envelope(None, ret_code=-11, ret_msg="rate limit exceeded"))
    response = futunn_client.get("/v1/market/bars", params=STOCK_PARAMS)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "UPSTREAM_RATE_LIMITED"


def test_sse_stream_unsupported(futunn_client: TestClient) -> None:
    """第一版不做流式（REST 无订阅接口、WS 推送专区未接入）：流式请求返回
    UNSUPPORTED_CAPABILITY（422 JSON）。"""
    stream_params = {
        "provider": "futunn",
        "asset_type": "stock",
        "instrument": "CN-SSE-600000",
        "variant": "futunn",
    }
    response = futunn_client.get("/v1/market/quotes/stream", params=stream_params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_CAPABILITY"


def test_missing_credentials_returns_clear_error(default_client) -> None:  # type: ignore[no-untyped-def]
    """未配置 AppKey/私钥路径时：Provider 不启用（能力目录可见），显式请求
    返回明确错误（携缺失变量名）。"""
    caps = default_client.get("/v1/providers/futunn/capabilities")
    assert caps.status_code == 200
    assert caps.json()["data"]["enabled"] is False

    response = default_client.get(
        "/v1/market/bars", params={**STOCK_PARAMS, "provider": "futunn"}
    )
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "PROVIDER_DISABLED"
    assert "TAPEHUB_FUTUNN_APP_KEY" in error["details"]["disabled_reason"]

    response = default_client.get("/v1/market/quotes", params=STOCK_QUOTE_PARAMS)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "PROVIDER_DISABLED"
    assert "TAPEHUB_FUTUNN_APP_KEY" in error["details"]["disabled_reason"]
