"""Twelve Data 完整 HTTP 链路契约测试（SDK 用桩 Mock，无真实网络）。"""

import pytest
from fastapi.testclient import TestClient

from tapehub.api.app import create_app
from tapehub.providers.registry import build_default_registry
from tests.conftest import make_settings
from tests.fixtures.providers import fake_websockets as fake_ws
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
    "start": "2026-01-01T00:00:00Z",
    "end": "2026-01-10T00:00:00Z",
    # 显式传 variant=twelvedata（缺省时按 default_variant 解析为同名变体）
    "variant": "twelvedata",
}

QUOTE_PARAMS = {
    "provider": "twelvedata",
    "asset_type": "forex",
    "instrument": "GLOBAL-OTC-EUR/USD",
    "variant": "twelvedata",
}


@pytest.fixture()
def twelvedata_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    install(monkeypatch)  # REST 桩（bars）
    fake_ws.install(monkeypatch)  # WS 假件（quotes：M2 快照走订阅缓存）
    settings = make_settings(max_limit=1000, twelvedata_api_key="test-key")
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
    # 外汇墙钟按悉尼解释，全市场统一输出北京时间：悉尼 01-02 00:00 = 北京 01-01 21:00
    assert data["bars"][0]["timestamp"] == "2026-01-01T21:00:00+08:00"
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
    assert len(entry["capabilities"]) == 3

    bars_caps = [c for c in entry["capabilities"] if c["data_type"] == "bars"]
    assert {tuple(cap["asset_types"]) for cap in bars_caps} == {("forex",), ("crypto",)}
    for cap in bars_caps:
        assert cap["variants"] == ["twelvedata"]
        assert cap["markets"] == ["GLOBAL"]
        assert [i["code"] for i in cap["intervals"]] == [
            "PT1M", "PT5M", "PT15M", "PT30M", "PT45M", "PT1H",
            "PT2H", "PT4H", "PT8H", "P1D", "P1W", "P1M",
        ]
        assert cap["adjustments"] == ["none"]
        assert cap["fields"] == ["open", "high", "low", "close"]

    # 实时快照：无周期/复权维度（显式空），fields 首版三项（extensions 待探针回填）
    quote_caps = [c for c in entry["capabilities"] if c["data_type"] == "quotes"]
    assert len(quote_caps) == 1
    quote_cap = quote_caps[0]
    assert quote_cap["asset_types"] == ["forex", "crypto"]
    assert quote_cap["variants"] == ["twelvedata"]
    assert quote_cap["markets"] == ["GLOBAL"]
    assert quote_cap["delivery"] == ["rest"]
    assert quote_cap["intervals"] == []
    assert quote_cap["adjustments"] == []
    # 2026-11-05 探针实测 forex/crypto 均未返回 volume → 不声明
    assert quote_cap["fields"] == ["timestamp", "last_price"]


def test_unsupported_interval_p3d(twelvedata_client: TestClient) -> None:  # type: ignore[no-untyped-def]
    params = {**FOREX_PARAMS, "interval": "P3D"}
    response = twelvedata_client.get("/v1/market/bars", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_CAPABILITY"


def test_crypto_chain(twelvedata_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    preset_values(monkeypatch, forex_values_rows(["2026-01-02 00:00:00"]))

    params = {
        **FOREX_PARAMS,
        "asset_type": "crypto",
        "instrument": "GLOBAL-CRYPTO-BTC/USD",
    }
    response = twelvedata_client.get("/v1/market/bars", params=params)
    assert response.status_code == 200

    from tests.fixtures.providers.stub_twelvedata import StubTDClient

    assert StubTDClient.instances[-1].calls[0]["symbol"] == "BTC/USD"
    assert response.json()["meta"]["provider"] == "twelvedata"


def test_default_variant_routes_to_declared_default(
    twelvedata_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缺省 variant：按 manifest default_variant（twelvedata）具体化并回显 meta。"""
    preset_values(monkeypatch, forex_values_rows(["2026-01-02 00:00:00"]))

    params = {k: v for k, v in FOREX_PARAMS.items() if k != "variant"}
    response = twelvedata_client.get("/v1/market/bars", params=params)
    assert response.status_code == 200
    assert response.json()["meta"]["variant"] == "twelvedata"


# ---------------------------------------------------------------------------
# 实时快照链路（REST /quote 同步应答；无流式）
# ---------------------------------------------------------------------------


def test_quote_chain_forex(twelvedata_client: TestClient) -> None:  # type: ignore[no-untyped-def]
    """外汇快照链路（WS 订阅缓存）：symbol 映射 + 分钟桶时间统一北京时间。"""
    response = twelvedata_client.get("/v1/market/quotes", params=QUOTE_PARAMS)
    assert response.status_code == 200
    body = response.json()

    ws = fake_ws.FakeWebSocket.instances[-1]
    subscribes = [m for m in ws.sent if m.get("action") == "subscribe"]
    assert subscribes and subscribes[0]["params"]["symbols"] == "EUR/USD"

    data = body["data"]
    assert data["instrument"] == "GLOBAL-OTC-EUR/USD"
    assert data["asset_type"] == "forex"
    quote = data["quote"]
    # WS price 事件 timestamp 为分钟桶 Unix 秒（探针实证），UTC → 北京时间
    from datetime import datetime

    from tapehub.domain.models import CN_TIMEZONE

    expected = datetime.fromtimestamp(fake_ws.INITIAL_TIMESTAMP, tz=CN_TIMEZONE).isoformat()
    assert quote["timestamp"] == expected
    assert quote["last_price"] == fake_ws.INITIAL_PRICE
    assert quote["volume"] is None  # WS price 事件无 volume（探针实证）
    assert quote["amount"] is None
    assert quote["bid_price1"] is None  # 上游无盘口：五档恒 null
    assert quote["ask_price5"] is None
    # extensions 白名单（2026-11-05 WS 探针实证回填）：4 个字符串字段
    assert quote["extensions"] == {
        "currency_base": "Euro",
        "currency_quote": "US Dollar",
        "type": "Physical Currency",  # EUR/USD price 事件无 exchange
    }

    meta = body["meta"]
    assert meta["provider"] == "twelvedata"
    assert meta["variant"] == "twelvedata"
    assert meta["received_at"].endswith("+08:00")


def test_quote_chain_crypto(twelvedata_client: TestClient) -> None:  # type: ignore[no-untyped-def]
    FakeWebSocket = fake_ws.FakeWebSocket
    FakeWebSocket.initial_payloads = {
        "BTC/USD": {
            "event": "price",
            "symbol": "BTC/USD",
            "currency_base": "Bitcoin",
            "currency_quote": "US Dollar",
            "exchange": "Binance",  # 加密货币带 exchange（探针实证）
            "type": "Digital Currency",
            "timestamp": fake_ws.INITIAL_TIMESTAMP,
            "price": 79838.73,
        }
    }

    params = {
        **QUOTE_PARAMS,
        "asset_type": "crypto",
        "instrument": "GLOBAL-CRYPTO-BTC/USD",
    }
    response = twelvedata_client.get("/v1/market/quotes", params=params)
    assert response.status_code == 200

    ws = FakeWebSocket.instances[-1]
    assert [m for m in ws.sent if m.get("action") == "subscribe"][0]["params"][
        "symbols"
    ] == "BTC/USD"
    quote = response.json()["data"]["quote"]
    assert quote["last_price"] == 79838.73
    assert quote["volume"] is None  # WS 加密货币事件无 volume（探针实证）
    assert quote["extensions"] == {
        "currency_base": "Bitcoin",
        "currency_quote": "US Dollar",
        "exchange": "Binance",
        "type": "Digital Currency",
    }
    assert response.json()["meta"]["provider"] == "twelvedata"


def test_quote_auto_routes(twelvedata_client: TestClient) -> None:  # type: ignore[no-untyped-def]
    """缺省 provider/variant：auto 路由命中 twelvedata（GLOBAL 外汇快照唯一候选）。"""
    params = {k: v for k, v in QUOTE_PARAMS.items() if k not in ("provider", "variant")}
    response = twelvedata_client.get("/v1/market/quotes", params=params)
    assert response.status_code == 200
    meta = response.json()["meta"]
    assert meta["provider"] == "twelvedata"
    assert meta["variant"] == "twelvedata"


def test_quote_unsupported_asset_type_422(twelvedata_client: TestClient) -> None:  # type: ignore[no-untyped-def]
    """quotes 能力仅声明 forex/crypto：能力目录即拒绝股票快照。"""
    params = {**QUOTE_PARAMS, "asset_type": "stock", "instrument": "CN-SSE-600000"}
    response = twelvedata_client.get("/v1/market/quotes", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_CAPABILITY"


def test_quote_missing_api_key_501(default_client) -> None:  # type: ignore[no-untyped-def]
    """未配置 API Key：快照请求与 bars 同样返回 PROVIDER_NOT_IMPLEMENTED。"""
    response = default_client.get("/v1/market/quotes", params=QUOTE_PARAMS)
    assert response.status_code == 501
    error = response.json()["error"]
    assert error["code"] == "PROVIDER_NOT_IMPLEMENTED"
    assert error["details"]["setting"] == "TAPEHUB_TWELVEDATA_API_KEY"
