"""TickFlow 完整 HTTP 链路契约测试（SDK 用桩 Mock，无真实网络）。"""

from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tapehub.api.app import create_app
from tapehub.domain.models import CN_TIMEZONE
from tapehub.providers.registry import build_default_registry
from tests.conftest import make_settings
from tests.fixtures.providers.stub_tickflow import (
    StubTickFlow,
    install,
    klines_payload,
    preset_quote_payload,
    quote_payload,
    sized_klines_payload,
)

BASE_PARAMS = {
    "provider": "tickflow",
    "asset_type": "stock",
    "instrument": "CN-SSE-600000",
    "interval": "P1D",
    "start": "2026-01-01T00:00:00+08:00",
    "end": "2026-01-10T00:00:00+08:00",
    # 显式传 variant=tickflow（缺省时按 default_variant 解析为同名变体）
    "variant": "tickflow",
}


def preset_payload(monkeypatch: pytest.MonkeyPatch, payload: Any) -> None:
    """让每个新建的桩客户端都返回指定响应。"""
    original_init = StubTickFlow.__init__

    def init_with_payload(self: StubTickFlow, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.payload = payload

    monkeypatch.setattr(StubTickFlow, "__init__", init_with_payload)


@pytest.fixture()
def tickflow_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    install(monkeypatch)
    settings = make_settings(max_limit=1000, tickflow_api_key="test-key")
    return TestClient(create_app(settings=settings, registry=build_default_registry(settings)))


def test_full_chain_default_adjust_is_qfq(
    tickflow_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    preset_payload(monkeypatch, klines_payload(["2026-01-%02d" % d for d in range(2, 10)]))

    response = tickflow_client.get("/v1/market/bars", params=BASE_PARAMS)
    assert response.status_code == 200
    body = response.json()

    client = StubTickFlow.instances[-1]
    assert client.api_key == "test-key"
    kwargs = client.calls[0]
    # 未传递 adjust → 股票默认 qfq → forward；所有参数显式传递
    assert kwargs["symbol"] == "600000.SH"
    assert kwargs["adjust"] == "forward"
    assert kwargs["period"] == "1d"

    data = body["data"]
    assert data["instrument"] == "CN-SSE-600000"
    assert len(data["bars"]) == 8
    timestamps = [bar["timestamp"] for bar in data["bars"]]
    assert timestamps == sorted(timestamps)
    assert data["bars"][0]["extensions"]["prev_close"] == 10.1

    meta = body["meta"]
    assert meta["provider"] == "tickflow"
    assert meta["count"] == 8
    assert meta["partial"] is False
    assert body["warnings"] == []


def test_truncation_warning_when_upstream_limit_reached(
    tickflow_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    preset_payload(monkeypatch, sized_klines_payload(10000))

    response = tickflow_client.get("/v1/market/bars", params=BASE_PARAMS)
    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["partial"] is True
    assert body["warnings"]  # 截断提示
    assert body["meta"]["count"] == 500  # 统一分页默认 limit


def test_missing_api_key_returns_clear_error(default_client) -> None:  # type: ignore[no-untyped-def]
    """未配置 API Key 时：Provider 不启用（能力目录可见），显式请求返回明确错误。"""
    caps = default_client.get("/v1/providers/tickflow/capabilities")
    assert caps.status_code == 200
    assert caps.json()["data"]["enabled"] is False

    response = default_client.get("/v1/market/bars", params={**BASE_PARAMS, "provider": "tickflow"})
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "PROVIDER_DISABLED"
    assert "TAPEHUB_TICKFLOW_API_KEY" in error["details"]["disabled_reason"]


def test_capability_catalog(tickflow_client: TestClient) -> None:  # type: ignore[no-untyped-def]
    response = tickflow_client.get("/v1/capabilities?provider=tickflow")
    assert response.status_code == 200
    entry = response.json()["data"][0]
    assert entry["provider"] == "tickflow"
    assert entry["enabled"] is True
    caps = entry["capabilities"]
    # bars 三块逐市场声明（history 已按 20261107T145801 探针报告回填：早上市
    # 基准标的二分 90 天粒度收敛、三市场深度不同——原合并块取跨市场最早
    # US 值对 CN 过度声明 29 年，已拆块；分钟族免费档无分钟权限（403 三轮
    # 实证）无法核实，保持 null）+ quotes 快照能力三市场合并
    assert [(cap["data_type"], cap["markets"]) for cap in caps] == [
        ("bars", ["CN"]),
        ("bars", ["HK"]),
        ("bars", ["US"]),
        ("quotes", ["CN", "HK", "US"]),
    ]
    expected_starts = {
        "CN": "1991-01-18T00:00:00+08:00",
        "HK": "1980-02-15T00:00:00+08:00",
        "US": "1962-02-17T00:00:00+08:00",
    }
    for cap in caps[:3]:
        assert cap["asset_types"] == ["stock"]
        assert cap["variants"] == ["tickflow"]
        # 分钟族不声明（免费档无权限，2026-11-07 拍板与 alphafeed 同口径）
        assert [i["code"] for i in cap["intervals"]] == ["P1D", "P1W", "P1M"]
        assert cap["adjustments"] == ["none", "qfq", "hfq"]
        starts = {i["code"]: i["history"]["start"] for i in cap["intervals"]}
        assert starts["P1D"] == starts["P1W"] == starts["P1M"] == expected_starts[cap["markets"][0]]
    # quotes 快照能力：无 interval/adjust 维度，三市场 stock
    quotes_cap = caps[3]
    assert quotes_cap["data_type"] == "quotes"
    assert quotes_cap["markets"] == ["CN", "HK", "US"]
    assert quotes_cap["intervals"] == []
    assert quotes_cap["adjustments"] == []


def test_unsupported_interval(tickflow_client: TestClient) -> None:  # type: ignore[no-untyped-def]
    # P3D 是合法 ISO 周期，但 TickFlow 未声明：能力目录即拒绝
    params = {**BASE_PARAMS, "interval": "P3D"}
    response = tickflow_client.get("/v1/market/bars", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_CAPABILITY"


def test_adjust_hfq_maps_to_backward(
    tickflow_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    preset_payload(monkeypatch, klines_payload(["2026-01-02"]))

    response = tickflow_client.get(
        "/v1/market/bars", params={**BASE_PARAMS, "adjust": "hfq"}
    )
    assert response.status_code == 200
    assert StubTickFlow.instances[-1].calls[0]["adjust"] == "backward"


@pytest.mark.parametrize("interval", ["PT1M", "PT10M", "PT1H"])
def test_minute_periods_unsupported(
    tickflow_client: TestClient, interval: str
) -> None:
    # 分钟族不声明（免费档无权限）：能力目录即拒绝
    response = tickflow_client.get(
        "/v1/market/bars", params={**BASE_PARAMS, "interval": interval}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_CAPABILITY"


def test_default_variant_routes_to_declared_default(
    tickflow_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缺省 variant：按 manifest default_variant（tickflow）具体化并回显 meta。"""
    preset_payload(monkeypatch, klines_payload(["2026-01-02"]))

    params = {k: v for k, v in BASE_PARAMS.items() if k != "variant"}
    response = tickflow_client.get("/v1/market/bars", params=params)
    assert response.status_code == 200
    assert response.json()["meta"]["variant"] == "tickflow"


# ---------------------------------------------------------------------------
# 港股 / 美股 K 线链路与实时快照链路（M2，GET /v1/market/quotes）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("instrument", "symbol"),
    [
        ("HK-HKEX-00700", "00700.HK"),
        ("US-NASDAQ-AAPL", "AAPL.US"),
    ],
)
def test_hk_us_bars_full_chain(
    tickflow_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    instrument: str,
    symbol: str,
) -> None:
    preset_payload(monkeypatch, klines_payload(["2026-01-05", "2026-01-06"]))

    response = tickflow_client.get(
        "/v1/market/bars", params={**BASE_PARAMS, "instrument": instrument}
    )
    assert response.status_code == 200
    body = response.json()
    kwargs = StubTickFlow.instances[-1].calls[0]
    assert kwargs["symbol"] == symbol
    assert body["data"]["instrument"] == instrument
    assert body["meta"]["provider"] == "tickflow"
    assert len(body["data"]["bars"]) == 2


QUOTE_PARAMS = {
    "provider": "tickflow",
    "asset_type": "stock",
    "instrument": "CN-SSE-600000",
    "variant": "tickflow",
}


def test_quote_full_chain(
    tickflow_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A股快照链路：单标的 symbols 调用 + extensions 白名单。"""
    preset_quote_payload(monkeypatch, quote_payload("600000.SH"))

    response = tickflow_client.get("/v1/market/quotes", params=QUOTE_PARAMS)
    assert response.status_code == 200
    body = response.json()

    client = StubTickFlow.instances[-1]
    assert client.quote_calls == [{"symbols": ["600000.SH"]}]

    data = body["data"]
    assert data["instrument"] == "CN-SSE-600000"
    assert data["asset_type"] == "stock"
    assert body["meta"]["provider"] == "tickflow"
    quote = data["quote"]
    assert quote["last_price"] == 10.25
    assert quote["volume"] == 123456.0
    assert quote["amount"] == 1266924.0
    # 行情时间统一北京时间（上游毫秒时间戳归一化）
    assert quote["timestamp"] == "2026-01-05T08:00:00+08:00"
    # 盘口上游无源字段恒 null；extensions 白名单透出会话/名称/涨跌幅/开高低昨收
    assert quote["bid_price1"] is None
    assert quote["extensions"]["session"] == "regular"
    assert quote["extensions"]["name"] == "浦发银行"
    assert quote["extensions"]["change_pct"] == 1.49
    assert quote["extensions"]["prev_close"] == 10.1


@pytest.mark.parametrize(
    ("instrument", "symbol"),
    [
        ("HK-HKEX-00700", "00700.HK"),
        ("US-NASDAQ-AAPL", "AAPL.US"),
    ],
)
def test_quote_hk_us_chain(
    tickflow_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    instrument: str,
    symbol: str,
) -> None:
    preset_quote_payload(monkeypatch, quote_payload(symbol))

    response = tickflow_client.get(
        "/v1/market/quotes", params={**QUOTE_PARAMS, "instrument": instrument}
    )
    assert response.status_code == 200
    assert StubTickFlow.instances[-1].quote_calls == [{"symbols": [symbol]}]
    assert response.json()["data"]["instrument"] == instrument


def test_quote_contract_violation_is_upstream_error(
    tickflow_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上游契约违背（空 data）→ UPSTREAM_ERROR，不伪造快照。"""
    preset_quote_payload(monkeypatch, {"data": []})

    response = tickflow_client.get("/v1/market/quotes", params=QUOTE_PARAMS)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "UPSTREAM_ERROR"


# ---------------------------------------------------------------------------
# auto 路由优先级：tickflow 不抢占既有默认路由（注册序末位）
# ---------------------------------------------------------------------------


def test_auto_routing_precedence(tickflow_client: TestClient) -> None:
    """重叠能力归既有 Provider（tqsdk / twelvedata），独占能力归 tickflow。"""
    from tapehub.application.provider_router import ProviderRouter
    from tapehub.domain.models import AssetType, Instrument, QuoteQuery

    settings = make_settings(
        tickflow_api_key="test-key",
        tqsdk_auth_user="user@example.com",
        tqsdk_auth_password="secret",
        twelvedata_api_key="test-twelvedata-key",
    )
    router = ProviderRouter(build_default_registry(settings))

    def _quote(instrument: str) -> str:
        selection = router.select_quote(
            QuoteQuery(
                asset_type=AssetType.STOCK, instrument=Instrument.parse(instrument)
            )
        )
        return selection.record.name

    def _bars(instrument: str) -> str:
        from tapehub.domain.intervals import Interval
        from tapehub.domain.models import Adjust, BarQuery

        selection = router.select(
            BarQuery(
                asset_type=AssetType.STOCK,
                instrument=Instrument.parse(instrument),
                interval=Interval.parse("P1D"),
                start=datetime(2026, 1, 1, tzinfo=CN_TIMEZONE),
                end=datetime(2026, 1, 31, tzinfo=CN_TIMEZONE),
                adjust=Adjust.NONE,
                offset=0,
                limit=10,
            )
        )
        return selection.record.name

    # CN stock 快照与 US stock K 线：既有默认（tqsdk / twelvedata）不受影响
    assert _quote("CN-SSE-600000") == "tqsdk"
    assert _bars("US-NASDAQ-AAPL") == "twelvedata"
    # tickflow 独占能力（HK 两类、US 快照）自动路由
    assert _quote("HK-HKEX-00700") == "tickflow"
    assert _quote("US-NASDAQ-AAPL") == "tickflow"
    assert _bars("HK-HKEX-00700") == "tickflow"

    # alphafeed 启用时：接管 HK 日线缺省（2026-11-07 拍板），US/CN 缺省不变
    af_settings = make_settings(
        alphafeed_api_key="test-alphafeed-key",
        tickflow_api_key="test-key",
        tqsdk_auth_user="user@example.com",
        tqsdk_auth_password="secret",
        twelvedata_api_key="test-twelvedata-key",
    )
    af_router = ProviderRouter(build_default_registry(af_settings))

    def _bars_af(instrument: str) -> str:
        from tapehub.domain.intervals import Interval
        from tapehub.domain.models import Adjust, BarQuery

        selection = af_router.select(
            BarQuery(
                asset_type=AssetType.STOCK,
                instrument=Instrument.parse(instrument),
                interval=Interval.parse("P1D"),
                start=datetime(2026, 1, 1, tzinfo=CN_TIMEZONE),
                end=datetime(2026, 1, 31, tzinfo=CN_TIMEZONE),
                adjust=Adjust.NONE,
                offset=0,
                limit=10,
            )
        )
        return selection.record.name

    assert _bars_af("HK-HKEX-00700") == "alphafeed"
    assert _bars_af("US-NASDAQ-AAPL") == "twelvedata"

    # alphafeed 启用时同时接管 HK/US 快照缺省；CN 快照缺省仍 tqsdk
    def _quote_af(instrument: str) -> str:
        selection = af_router.select_quote(
            QuoteQuery(
                asset_type=AssetType.STOCK, instrument=Instrument.parse(instrument)
            )
        )
        return selection.record.name

    assert _quote_af("HK-HKEX-00700") == "alphafeed"
    assert _quote_af("US-NASDAQ-AAPL") == "alphafeed"
    assert _quote_af("CN-SSE-600000") == "tqsdk"
