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
    preset_quote_payload,
    quote_payload,
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
    with TestClient(
        create_app(settings=settings, registry=build_default_registry(settings))
    ) as client:
        yield client


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


@pytest.mark.parametrize(
    ("asset_type", "instrument", "expected_symbol"),
    [
        # 指数/ETF 与 stock 共用统一 klines 端点与后缀表（探针 2026-11-15
        # 第 7/8 轮实证：上游不做证券类型路由拒绝，映射层零分支）
        ("index", "CN-SSE-000001", "000001.SH"),
        ("etf", "CN-SSE-510050", "510050.SH"),
    ],
)
def test_index_etf_chain(
    asset_type: str,
    instrument: str,
    expected_symbol: str,
    alphafeed_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CN 指数/ETF 贯穿全链路：指数无复权（显式传 none）、ETF 默认 qfq。"""
    preset_payload(monkeypatch, klines_payload(["2026-01-02"]))

    params = {
        **{k: v for k, v in BASE_PARAMS.items() if k != "variant"},
        "asset_type": asset_type,
        "instrument": instrument,
    }
    response = alphafeed_client.get("/v1/market/bars", params=params)
    assert response.status_code == 200
    body = response.json()

    kwargs = StubAlphaFeed.instances[-1].calls[0]
    assert kwargs["symbol"] == expected_symbol
    # 缺省 adjust：stock/etf 为 qfq→forward，index 为 none（docs/api.md 默认值约定）
    expected_adjust = "none" if asset_type == "index" else "forward"
    assert kwargs["adjust"] == expected_adjust
    assert kwargs["period"] == "1d"
    assert body["data"]["instrument"] == instrument
    assert body["meta"]["variant"] == "alphafeed"


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
    # bars 六块（CN stock/index/etf + HK/US stock 逐块声明：三市场 history
    # 深度不同拆块；index/etf history 未核实与 stock 不同亦独立成块——
    # 2026-11-15 扩容；分钟族不声明、周期枚举无 10m）+ quotes 一块
    assert [(cap["data_type"], cap["markets"]) for cap in caps] == [
        ("bars", ["CN"]),
        ("bars", ["CN"]),
        ("bars", ["CN"]),
        ("bars", ["HK"]),
        ("bars", ["US"]),
        ("quotes", ["CN", "HK", "US"]),
    ]
    assert [cap["asset_types"] for cap in caps[:3]] == [["stock"], ["index"], ["etf"]]
    # CN stock history 逐市场回填（二分收敛估计窗口右端零点，approx ±90 天）；
    # 指数/ETF history 未核实保持 null（chain 二分后回填）
    assert caps[0]["intervals"][0]["history"]["start"] == "1991-01-14T00:00:00+08:00"
    assert all(i["history"]["start"] is None for i in caps[1]["intervals"])
    assert all(i["history"]["start"] is None for i in caps[2]["intervals"])
    assert caps[1]["adjustments"] == ["none"]  # 指数无复权
    assert caps[2]["adjustments"] == ["none", "qfq", "hfq"]  # ETF 复权生效（诊断实证）
    for cap in caps[3:5]:
        assert cap["asset_types"] == ["stock"]
        assert cap["variants"] == ["alphafeed"]
        assert [i["code"] for i in cap["intervals"]] == ["P1D", "P1W", "P1M"]
        assert cap["adjustments"] == ["none", "qfq", "hfq"]
    assert [cap["intervals"][0]["history"]["start"] for cap in caps[3:5]] == [
        "1980-02-15T00:00:00+08:00",
        "1962-02-17T00:00:00+08:00",
    ]
    quotes_cap = caps[5]
    # 快照无周期/复权维度，显式空
    assert quotes_cap["intervals"] == []
    assert quotes_cap["adjustments"] == []
    assert quotes_cap["fields"] == ["timestamp", "last_price", "volume", "amount"]


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


@pytest.mark.parametrize(
    ("instrument", "expected_symbol"),
    [
        ("CN-SSE-600519", "600519.SH"),
        ("HK-HKEX-00700", "00700.HK"),
        ("US-NASDAQ-AAPL", "AAPL.US"),
    ],
)
def test_quote_chain_three_markets(
    alphafeed_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    instrument: str,
    expected_symbol: str,
) -> None:
    """三市场快照链路：单标的 symbols 调用与响应归一化。"""
    preset_quote_payload(monkeypatch, quote_payload(symbol=expected_symbol))

    response = alphafeed_client.get(
        "/v1/market/quotes",
        params={
            "provider": "alphafeed",
            "asset_type": "stock",
            "instrument": instrument,
            "variant": "alphafeed",
        },
    )
    assert response.status_code == 200
    body = response.json()

    assert StubAlphaFeed.instances[-1].calls[0] == {"symbols": [expected_symbol]}
    data = body["data"]
    assert data["instrument"] == instrument
    quote = data["quote"]
    assert quote["last_price"] == 20.25
    assert quote["volume"] == 200.0
    assert quote["timestamp"] == "2026-01-05T08:00:00+08:00"
    assert quote["extensions"]["name"] == "测试标的"
    assert quote["extensions"]["change_pct"] == 0.00746

    meta = body["meta"]
    assert meta["provider"] == "alphafeed"
    assert meta["variant"] == "alphafeed"
