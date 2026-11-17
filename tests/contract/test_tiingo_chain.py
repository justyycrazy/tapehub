"""Tiingo 完整 HTTP 链路契约测试（HTTP/WS 用桩 Mock，无真实网络）。"""

import pytest
from fastapi.testclient import TestClient

from tapehub.api.app import create_app
from tapehub.providers.registry import build_default_registry
from tests.conftest import make_settings
from tests.fixtures.providers import fake_tiingo_ws as fake_ws
from tests.fixtures.providers.stub_tiingo import (
    StubClient,
    crypto_rows,
    forex_rows,
    install,
    preset_payload,
)

FOREX_PARAMS = {
    "provider": "tiingo",
    "asset_type": "forex",
    "instrument": "GLOBAL-OTC-EUR/USD",
    "interval": "P1D",
    "start": "2026-01-01T00:00:00Z",
    "end": "2026-01-10T00:00:00Z",
    # 显式传 variant=tiingo（缺省时按 default_variant 解析为同名变体）
    "variant": "tiingo",
}

QUOTE_PARAMS = {
    "provider": "tiingo",
    "asset_type": "forex",
    "instrument": "GLOBAL-OTC-EUR/USD",
    "variant": "tiingo",
}


@pytest.fixture()
def tiingo_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    install(monkeypatch)  # REST 桩（bars）
    fake_ws.install(monkeypatch)  # WS 假件（quotes：M2 快照走订阅缓存）
    settings = make_settings(max_limit=1000, tiingo_api_key="test-key")
    with TestClient(
        create_app(settings=settings, registry=build_default_registry(settings))
    ) as client:
        yield client


def test_forex_chain(tiingo_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    preset_payload(
        monkeypatch, forex_rows(["2026-01-02T00:00:00.000Z", "2026-01-03T00:00:00.000Z"])
    )

    response = tiingo_client.get("/v1/market/bars", params=FOREX_PARAMS)
    assert response.status_code == 200
    body = response.json()

    data = body["data"]
    assert data["instrument"] == "GLOBAL-OTC-EUR/USD"
    assert data["asset_type"] == "forex"
    assert data["interval"] == "P1D"
    assert len(data["bars"]) == 2
    timestamps = [bar["timestamp"] for bar in data["bars"]]
    assert timestamps == sorted(timestamps)  # 统一升序
    # date 为 ISO UTC（官方口径）→ 全市场统一输出北京时间：UTC 00:00 = 北京 08:00
    assert data["bars"][0]["timestamp"] == "2026-01-02T08:00:00+08:00"
    assert data["bars"][0]["close"] == 1.0855
    assert data["bars"][0]["volume"] is None  # 外汇文档仅 OHLC，无 volume

    meta = body["meta"]
    assert meta["provider"] == "tiingo"
    assert meta["count"] == 2
    assert meta["partial"] is False  # 上游无文档化行数上限，不编造截断结论
    assert body["warnings"] == []


def test_crypto_chain(tiingo_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    preset_payload(monkeypatch, crypto_rows(["2026-01-02T00:00:00.000Z"]))

    params = {
        **FOREX_PARAMS,
        "asset_type": "crypto",
        "instrument": "GLOBAL-CRYPTO-BTC/USD",
    }
    response = tiingo_client.get("/v1/market/bars", params=params)
    assert response.status_code == 200

    call = StubClient.instances[-1].calls[-1]
    assert call["params"]["tickers"] == "btcusd"  # 小写去斜杠
    body = response.json()
    assert body["data"]["bars"][0]["volume"] == 12345.6  # 加密 volume 有值（base 币）
    assert body["meta"]["provider"] == "tiingo"


def test_capability_catalog(tiingo_client: TestClient) -> None:
    response = tiingo_client.get("/v1/capabilities?provider=tiingo")
    assert response.status_code == 200
    entry = response.json()["data"][0]
    assert entry["provider"] == "tiingo"
    assert entry["enabled"] is True
    assert len(entry["capabilities"]) == 3

    bars_caps = [c for c in entry["capabilities"] if c["data_type"] == "bars"]
    assert {tuple(cap["asset_types"]) for cap in bars_caps} == {
        ("forex",),
        ("crypto",),
    }
    caps_by_asset = {tuple(cap["asset_types"])[0]: cap for cap in bars_caps}
    forex_cap = caps_by_asset["forex"]
    crypto_cap = caps_by_asset["crypto"]
    # 周期面收窄：两资产均仅 PT1H/PT4H/P1D
    assert [i["code"] for i in forex_cap["intervals"]] == ["PT1H", "PT4H", "P1D"]
    assert [i["code"] for i in crypto_cap["intervals"]] == ["PT1H", "PT4H", "P1D"]
    for cap in bars_caps:
        assert cap["variants"] == ["tiingo"]
        assert cap["adjustments"] == ["none"]
    # history 全局统一口径回填：fx 三周期 2020-01-01T08:00 档案硬边界
    # 锚保持（已月首）；crypto 实测早于 2020-01-01 统一归一 2020-01-01
    # （P1D 旧实测 ≈2011-08-25、分钟族 ≈2018-01-01 落档）
    for interval in forex_cap["intervals"]:
        assert interval["history"]["start"] == "2020-01-01T08:00:00+08:00"
        assert interval["history"]["end"] is None
    for interval in crypto_cap["intervals"]:
        expected = "2020-01-01T00:00:00+08:00"
        assert interval["history"]["start"] == expected
        assert interval["history"]["end"] is None

    fields_by_asset = {tuple(cap["asset_types"])[0]: cap["fields"] for cap in bars_caps}
    assert fields_by_asset["forex"] == ["open", "high", "low", "close"]
    assert fields_by_asset["crypto"] == ["open", "high", "low", "close", "volume"]

    # 实时快照：无周期/复权维度（显式空）；WS 形态未实证，fields 首版两项
    quote_caps = [c for c in entry["capabilities"] if c["data_type"] == "quotes"]
    assert len(quote_caps) == 1
    quote_cap = quote_caps[0]
    assert quote_cap["asset_types"] == ["forex", "crypto"]
    assert quote_cap["variants"] == ["tiingo"]
    assert quote_cap["markets"] == ["GLOBAL"]
    assert quote_cap["delivery"] == ["rest"]
    assert quote_cap["intervals"] == []
    assert quote_cap["adjustments"] == []
    # fields 按 WS 探针实证扩容：单档 bid/ask（fx Q 数组含
    # bidSize/bidPrice/midPrice/askSize/askPrice；crypto 同 + exchange）
    assert quote_cap["fields"] == [
        "timestamp", "last_price", "bid_price1", "ask_price1", "bid_volume1", "ask_volume1",
    ]


def test_unsupported_interval_p1w_422(tiingo_client: TestClient) -> None:
    """上游 resampleFreq 无周线：能力目录即不声明，请求返回 422。"""
    params = {**FOREX_PARAMS, "interval": "P1W"}
    response = tiingo_client.get("/v1/market/bars", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_CAPABILITY"


def test_wrong_exchange_namespace_422(tiingo_client: TestClient) -> None:
    """外汇必须使用 OTC 命名空间：GLOBAL-CRYPTO 为合法标准代码，外汇
    路径不接受，明确拒绝而非透传上游。"""
    params = {**FOREX_PARAMS, "instrument": "GLOBAL-CRYPTO-EUR/USD"}
    response = tiingo_client.get("/v1/market/bars", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_CAPABILITY"


def test_default_variant_routes_to_declared_default(
    tiingo_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缺省 variant：按 manifest default_variant（tiingo）具体化并回显 meta。"""
    preset_payload(monkeypatch, forex_rows(["2026-01-02T00:00:00.000Z"]))

    params = {k: v for k, v in FOREX_PARAMS.items() if k != "variant"}
    response = tiingo_client.get("/v1/market/bars", params=params)
    assert response.status_code == 200
    assert response.json()["meta"]["variant"] == "tiingo"


# 注：两 Provider 均启用时 GLOBAL forex 的 auto 胜者由回退链序裁决（可调
# 配置），不在此断言具体胜者——机制测试见 tests/unit/test_registry_routing.py
# （合成置换配置）。本文件只覆盖 tiingo 自身的显式链路与变体语义。


def test_missing_api_key_returns_clear_error(default_client) -> None:  # type: ignore[no-untyped-def]
    """未配置 API Key 时：Provider 不启用（能力目录可见），显式请求返回明确错误。"""
    caps = default_client.get("/v1/providers/tiingo/capabilities")
    assert caps.status_code == 200
    assert caps.json()["data"]["enabled"] is False

    response = default_client.get(
        "/v1/market/bars", params={**FOREX_PARAMS, "provider": "tiingo"}
    )
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "PROVIDER_DISABLED"
    assert "TAPEHUB_TIINGO_API_KEY" in error["details"]["disabled_reason"]


# ---------------------------------------------------------------------------
# 实时快照链路（WS 订阅缓存同源）
# ---------------------------------------------------------------------------


def test_quote_chain_forex(tiingo_client: TestClient) -> None:
    """外汇快照链路（WS 订阅缓存）：TOP 元素 mid 回退 + bid/ask 防御映射。"""
    response = tiingo_client.get("/v1/market/quotes", params=QUOTE_PARAMS)
    assert response.status_code == 200
    body = response.json()

    ws = fake_ws.FakeWebSocket.instances[-1]
    frames = ws.subscribe_frames("eurusd")
    # forex 阈值探针实证为 5（文档口径 7 被上游拒绝）
    assert frames and frames[0]["eventData"]["thresholdLevel"] == 5
    assert frames[0]["authorization"] == "test-key"  # token 随帧传递

    data = body["data"]
    assert data["instrument"] == "GLOBAL-OTC-EUR/USD"
    assert data["asset_type"] == "forex"
    quote = data["quote"]
    assert quote["last_price"] == 1.0855  # TOP 元素无 lastPrice → midPrice 回退
    assert quote["bid_price1"] == 1.085
    assert quote["ask_price1"] == 1.086
    assert quote["volume"] is None  # WS 形态未实证，volume 恒 null
    assert quote["ask_price5"] is None
    # 样本无 exchange（forex top 文档无该字段）；快照带 Runtime 到达戳 →
    # 新鲜度 age（测试桩即时投递为 0）
    assert quote["extensions"] == {"snapshot_age_ms": 0}
    # date ISO UTC → 北京时间
    from datetime import datetime

    from tapehub.domain.models import CN_TIMEZONE

    expected = datetime(2026, 1, 5, 8, tzinfo=CN_TIMEZONE).isoformat()
    assert quote["timestamp"] == expected

    meta = body["meta"]
    assert meta["provider"] == "tiingo"
    assert meta["variant"] == "tiingo"
    assert meta["received_at"].endswith("+08:00")


def test_quote_chain_crypto(tiingo_client: TestClient) -> None:
    """加密快照链路：Trade 元素 lastPrice 直接取值、crypto 端点 thresholdLevel=2。"""
    fake_ws.FakeWebSocket.initial_elements = {
        "btcusd": fake_ws.trade_element("btcusd", 43120.75, exchange="Binance")
    }

    params = {
        **QUOTE_PARAMS,
        "asset_type": "crypto",
        "instrument": "GLOBAL-CRYPTO-BTC/USD",
    }
    response = tiingo_client.get("/v1/market/quotes", params=params)
    assert response.status_code == 200

    ws = fake_ws.FakeWebSocket.instances[-1]
    frames = ws.subscribe_frames("btcusd")
    assert frames[0]["eventData"]["thresholdLevel"] == 2  # crypto 端点
    quote = response.json()["data"]["quote"]
    assert quote["last_price"] == 43120.75
    # 白名单透出；快照带 Runtime 到达戳 → 新鲜度 age（测试桩即时投递为 0）
    assert quote["extensions"] == {"exchange": "Binance", "snapshot_age_ms": 0}
    assert response.json()["meta"]["provider"] == "tiingo"


# 注：快照面的 GLOBAL forex auto 胜者同样由回退链序裁决（可调配置），
# 不在此断言具体胜者——机制测试见 tests/unit/test_registry_routing.py。


def test_quote_missing_api_key_returns_clear_error(default_client) -> None:  # type: ignore[no-untyped-def]
    """未配置 API Key：快照请求与 bars 同样返回 PROVIDER_DISABLED。"""
    response = default_client.get("/v1/market/quotes", params=QUOTE_PARAMS)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "PROVIDER_DISABLED"
    assert "TAPEHUB_TIINGO_API_KEY" in error["details"]["disabled_reason"]
