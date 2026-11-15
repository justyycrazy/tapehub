"""TickFlow 完整 HTTP 链路契约测试（SDK 用桩 Mock，无真实网络）。"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tapehub.api.app import create_app
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
    with TestClient(
        create_app(settings=settings, registry=build_default_registry(settings))
    ) as client:
        yield client


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
    # bars 七块（CN stock/index/etf + HK/US stock + US etf 逐块声明：history
    # 依据见 20261107T145801 探针报告与 2026-11-15 两轮扩容注释；分钟族免费
    # 档无分钟权限（403 三轮实证）无法核实，保持 null）+ quotes 快照能力
    # 三市场合并
    assert [(cap["data_type"], cap["markets"]) for cap in caps] == [
        ("bars", ["CN"]),
        ("bars", ["CN"]),
        ("bars", ["CN"]),
        ("bars", ["HK"]),
        ("bars", ["US"]),
        ("bars", ["US"]),
        ("quotes", ["CN", "HK", "US"]),
    ]
    expected_starts = {
        "CN": "1991-01-18T00:00:00+08:00",
        "HK": "1980-02-15T00:00:00+08:00",
        "US": "1962-02-17T00:00:00+08:00",
    }
    assert [cap["asset_types"] for cap in caps[:3]] == [["stock"], ["index"], ["etf"]]
    # CN stock history 逐市场回填；指数/ETF history 未核实保持 null（chain 二分后回填）
    starts = {i["code"]: i["history"]["start"] for i in caps[0]["intervals"]}
    assert starts["P1D"] == starts["P1W"] == starts["P1M"] == expected_starts["CN"]
    assert all(i["history"]["start"] is None for i in caps[1]["intervals"])
    assert all(i["history"]["start"] is None for i in caps[2]["intervals"])
    assert caps[1]["adjustments"] == ["none"]  # 指数无复权
    assert caps[2]["adjustments"] == ["none", "qfq", "hfq"]  # ETF 复权生效（诊断实证）
    for cap in caps[3:5]:
        assert cap["asset_types"] == ["stock"]
        assert cap["variants"] == ["tickflow"]
        # 分钟族不声明（免费档无权限，与 alphafeed 同口径）
        assert [i["code"] for i in cap["intervals"]] == ["P1D", "P1W", "P1M"]
        assert cap["adjustments"] == ["none", "qfq", "hfq"]
        cap_starts = {i["code"]: i["history"]["start"] for i in cap["intervals"]}
        expected = expected_starts[cap["markets"][0]]
        assert cap_starts["P1D"] == cap_starts["P1W"] == cap_starts["P1M"] == expected
    # US etf（2026-11-15 扩容）：三复权（目录外诊断实证），history 未核实保持 null
    assert caps[5]["asset_types"] == ["etf"]
    assert caps[5]["variants"] == ["tickflow"]
    assert [i["code"] for i in caps[5]["intervals"]] == ["P1D", "P1W", "P1M"]
    assert all(i["history"]["start"] is None for i in caps[5]["intervals"])
    assert caps[5]["adjustments"] == ["none", "qfq", "hfq"]
    # quotes 快照能力：无 interval/adjust 维度，三市场 stock
    quotes_cap = caps[6]
    assert quotes_cap["data_type"] == "quotes"
    assert quotes_cap["markets"] == ["CN", "HK", "US"]
    assert quotes_cap["intervals"] == []
    assert quotes_cap["adjustments"] == []


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
    tickflow_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CN 指数/ETF 贯穿全链路：指数无复权（显式传 none）、ETF 默认 qfq。"""
    preset_payload(monkeypatch, klines_payload(["2026-01-02"]))

    params = {
        **{k: v for k, v in BASE_PARAMS.items() if k != "variant"},
        "asset_type": asset_type,
        "instrument": instrument,
    }
    response = tickflow_client.get("/v1/market/bars", params=params)
    assert response.status_code == 200
    body = response.json()

    kwargs = StubTickFlow.instances[-1].calls[0]
    assert kwargs["symbol"] == expected_symbol
    # 缺省 adjust：stock/etf 为 qfq→forward，index 为 none（docs/api.md 默认值约定）
    expected_adjust = "none" if asset_type == "index" else "forward"
    assert kwargs["adjust"] == expected_adjust
    assert kwargs["period"] == "1d"
    assert body["data"]["instrument"] == instrument
    assert body["meta"]["variant"] == "tickflow"


@pytest.mark.parametrize(
    ("instrument", "expected_symbol"),
    [
        # US ETF 与 stock 共用统一 klines 端点与后缀表（目录外诊断 2026-11-15
        # 第 9 轮实证：SPY.US/QQQ.US 直测放行、三复权生效、映射层零分支）
        ("US-NYSE-SPY", "SPY.US"),
        ("US-NASDAQ-QQQ", "QQQ.US"),
    ],
)
def test_us_etf_chain(
    instrument: str,
    expected_symbol: str,
    tickflow_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """US ETF 贯穿全链路：ETF 缺省 qfq（docs/api.md 默认值约定）。"""
    preset_payload(monkeypatch, klines_payload(["2026-01-02"]))

    params = {
        **{k: v for k, v in BASE_PARAMS.items() if k != "variant"},
        "asset_type": "etf",
        "instrument": instrument,
    }
    response = tickflow_client.get("/v1/market/bars", params=params)
    assert response.status_code == 200
    body = response.json()

    kwargs = StubTickFlow.instances[-1].calls[0]
    assert kwargs["symbol"] == expected_symbol
    assert kwargs["adjust"] == "forward"
    assert kwargs["period"] == "1d"
    assert body["data"]["instrument"] == instrument
    assert body["meta"]["variant"] == "tickflow"


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
# 港股 / 美股 K 线链路与实时快照链路（GET /v1/market/quotes）
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


# 注：auto 路由优先级（重叠能力归谁、停用者跳过）由回退链序裁决——顺序是可调
# 配置，契约面不断言具体胜者；机制测试在 tests/unit/test_registry_routing.py
# （合成置换配置验证「按链序裁决、停用者跳过」）。
