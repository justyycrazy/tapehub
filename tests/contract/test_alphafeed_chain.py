"""AlphaFeed 完整 HTTP 链路契约测试（SDK 用桩 Mock，无真实网络）。"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tapehub.api.app import create_app
from tapehub.providers.registry import build_default_registry
from tests.conftest import make_settings
from tests.fixtures.providers.stub_alphafeed import (
    StubAlphaFeed,
    install,
    klines_payload,
    sized_klines_payload,
)

BASE_PARAMS = {
    "provider": "alphafeed",
    "asset_type": "stock",
    "instrument": "CN-SSE-600519",
    "interval": "P1D",
    "start": "2026-01-01T00:00:00+08:00",
    "end": "2026-01-10T00:00:00+08:00",
    # 显式传 variant=alphafeed（缺省时按 default_variant 解析为同名变体）
    "variant": "alphafeed",
}


def preset_payload(monkeypatch: pytest.MonkeyPatch, payload: Any) -> None:
    """让每个新建的桩客户端都返回指定响应。"""
    original_init = StubAlphaFeed.__init__

    def init_with_payload(self: StubAlphaFeed, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.payload = payload

    monkeypatch.setattr(StubAlphaFeed, "__init__", init_with_payload)


@pytest.fixture()
def alphafeed_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    install(monkeypatch)
    settings = make_settings(max_limit=1000, alphafeed_api_key="test-key")
    return TestClient(create_app(settings=settings, registry=build_default_registry(settings)))


def test_full_chain_default_adjust_is_qfq(
    alphafeed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    preset_payload(monkeypatch, klines_payload(["2026-01-%02d" % d for d in range(2, 10)]))

    response = alphafeed_client.get("/v1/market/bars", params=BASE_PARAMS)
    assert response.status_code == 200
    body = response.json()

    client = StubAlphaFeed.instances[-1]
    assert client.api_key == "test-key"
    kwargs = client.calls[0]
    # 未传递 adjust → 股票默认 qfq → forward；所有参数显式传递
    assert kwargs["symbol"] == "600519.SH"
    assert kwargs["adjust"] == "forward"
    assert kwargs["period"] == "1d"

    data = body["data"]
    assert data["instrument"] == "CN-SSE-600519"
    assert len(data["bars"]) == 8
    timestamps = [bar["timestamp"] for bar in data["bars"]]
    assert timestamps == sorted(timestamps)
    # 桩 epoch 按 UTC 零点编码，换算北京时间即 08:00
    assert data["bars"][0]["timestamp"] == "2026-01-02T08:00:00+08:00"
    assert data["bars"][0]["extensions"]["prev_close"] == 20.1

    meta = body["meta"]
    assert meta["provider"] == "alphafeed"
    assert meta["count"] == 8
    assert meta["partial"] is False
    assert body["warnings"] == []


def test_truncation_warning_when_upstream_limit_reached(
    alphafeed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    preset_payload(monkeypatch, sized_klines_payload(10000))

    response = alphafeed_client.get("/v1/market/bars", params=BASE_PARAMS)
    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["partial"] is True
    assert body["warnings"]  # 截断提示
    assert body["meta"]["count"] == 500  # 统一分页默认 limit


def test_missing_api_key_returns_clear_error(default_client) -> None:  # type: ignore[no-untyped-def]
    """未配置 API Key 时：Provider 不启用（能力目录可见），显式请求返回明确错误。"""
    caps = default_client.get("/v1/providers/alphafeed/capabilities")
    assert caps.status_code == 200
    assert caps.json()["data"]["enabled"] is False

    response = default_client.get(
        "/v1/market/bars", params={**BASE_PARAMS, "provider": "alphafeed"}
    )
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "PROVIDER_DISABLED"
    assert "TAPEHUB_ALPHAFEED_API_KEY" in error["details"]["disabled_reason"]


def test_capability_catalog(alphafeed_client: TestClient) -> None:  # type: ignore[no-untyped-def]
    response = alphafeed_client.get("/v1/capabilities?provider=alphafeed")
    assert response.status_code == 200
    entry = response.json()["data"][0]
    assert entry["provider"] == "alphafeed"
    assert entry["enabled"] is True
    caps = entry["capabilities"]
    # 两个 bars 能力块：CN 与 HK/US（分钟族不声明，AlphaFeed 周期枚举也没有 10m）
    assert [(cap["data_type"], cap["markets"]) for cap in caps] == [
        ("bars", ["CN"]),
        ("bars", ["HK", "US"]),
    ]
    for cap in caps:
        assert cap["asset_types"] == ["stock"]
        assert cap["variants"] == ["alphafeed"]
        assert [i["code"] for i in cap["intervals"]] == ["P1D", "P1W", "P1M"]
        assert cap["adjustments"] == ["none", "qfq", "hfq"]


def test_pt10m_unsupported(alphafeed_client: TestClient) -> None:  # type: ignore[no-untyped-def]
    # PT10M 是合法 ISO 周期，但 AlphaFeed 未声明：能力目录即拒绝
    params = {**BASE_PARAMS, "interval": "PT10M"}
    response = alphafeed_client.get("/v1/market/bars", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_CAPABILITY"


def test_adjust_hfq_maps_to_backward(
    alphafeed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    preset_payload(monkeypatch, klines_payload(["2026-01-02"]))

    response = alphafeed_client.get(
        "/v1/market/bars", params={**BASE_PARAMS, "adjust": "hfq"}
    )
    assert response.status_code == 200
    assert StubAlphaFeed.instances[-1].calls[0]["adjust"] == "backward"


def test_default_variant_routes_to_declared_default(
    alphafeed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缺省 variant：按 manifest default_variant（alphafeed）具体化并回显 meta。"""
    preset_payload(monkeypatch, klines_payload(["2026-01-02"]))

    params = {k: v for k, v in BASE_PARAMS.items() if k != "variant"}
    response = alphafeed_client.get("/v1/market/bars", params=params)
    assert response.status_code == 200
    assert response.json()["meta"]["variant"] == "alphafeed"


@pytest.mark.parametrize(
    ("instrument", "expected_symbol"),
    [
        ("HK-HKEX-00700", "00700.HK"),
        ("US-NASDAQ-AAPL", "AAPL.US"),
    ],
)
def test_hk_us_bars_chain(
    alphafeed_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    instrument: str,
    expected_symbol: str,
) -> None:
    """港股/美股日线链路：标的代码后缀映射与响应回显。"""
    preset_payload(monkeypatch, klines_payload(["2026-01-02"]))

    response = alphafeed_client.get(
        "/v1/market/bars", params={**BASE_PARAMS, "instrument": instrument}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["instrument"] == instrument
    assert StubAlphaFeed.instances[-1].calls[0]["symbol"] == expected_symbol
