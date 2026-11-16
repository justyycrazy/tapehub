"""ths 完整 HTTP 链路契约测试（HTTP 用桩 Mock，无真实网络）。

auto 路由说明：bars/quotes CN stock/index/etf/contract 的回退链中 baostock、
akshare 等免凭据 Provider 会截胡 auto 请求，故显式传 variant=ths 定向路由；
CN 期权 bars（asset_type 三分：commodity-option / index-option / etf-option，
八所归属见 docs/api.md）组合 ths 为缺省首位（回退链 ths→tushare，测试环境
tushare 凭据缺失自动跳过）、quotes 组合 ths 独占，auto（无 provider/variant
参数）直达。
"""

import pytest
from fastapi.testclient import TestClient

from tapehub.api.app import create_app
from tapehub.providers.registry import build_default_registry
from tests.conftest import make_settings
from tests.fixtures.providers.stub_ths import (
    bar_rows,
    envelope,
    install,
    intraday_data,
    intraday_points,
    preset_payload,
    snapshot_data,
    stock_snapshot_item,
)

STOCK_PARAMS = {
    "provider": "ths",
    "asset_type": "stock",
    "instrument": "CN-SSE-600519",
    "interval": "P1D",
    "start": "2026-01-01T00:00:00+08:00",
    "end": "2026-01-10T00:00:00+08:00",
    "adjust": "qfq",
    # 显式传 variant=ths（auto 链中免凭据 Provider 会截胡，见模块 docstring）
    "variant": "ths",
}

STOCK_QUOTE_PARAMS = {
    "provider": "ths",
    "asset_type": "stock",
    "instrument": "CN-SSE-600519",
    "variant": "ths",
}


@pytest.fixture()
def ths_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    install(monkeypatch)  # REST 桩（bars + quotes）
    settings = make_settings(max_limit=1000, ths_api_key="test-key")
    with TestClient(
        create_app(settings=settings, registry=build_default_registry(settings))
    ) as client:
        yield client


def test_stock_bars_chain(ths_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    preset_payload(
        monkeypatch,
        envelope({"item": bar_rows("date_ms", [1767196800000, 1767283200000])}),
    )

    response = ths_client.get("/v1/market/bars", params=STOCK_PARAMS)
    assert response.status_code == 200
    body = response.json()

    data = body["data"]
    assert data["instrument"] == "CN-SSE-600519"
    assert data["asset_type"] == "stock"
    assert data["interval"] == "P1D"
    assert len(data["bars"]) == 2
    timestamps = [bar["timestamp"] for bar in data["bars"]]
    assert timestamps == sorted(timestamps)  # 统一升序
    # date_ms 为北京零点毫秒戳 → 全市场统一输出北京时间
    assert data["bars"][0]["timestamp"] == "2026-01-01T00:00:00+08:00"
    assert data["bars"][0]["close"] == 10.5
    assert data["bars"][0]["volume"] == 1000.0  # A 股档官方口径：股
    assert data["bars"][0]["amount"] == 10500.0  # turnover → amount（元）

    meta = body["meta"]
    assert meta["provider"] == "ths"
    assert meta["variant"] == "ths"
    assert meta["count"] == 2
    assert meta["partial"] is False  # 上游无文档化行数上限，不编造截断结论
    assert body["warnings"] == []


@pytest.mark.parametrize(
    ("asset_type", "instrument"),
    [
        ("index-option", "CN-CFFEX-IO2601-C-4000"),
        ("etf-option", "CN-SSE-10012437"),
        ("commodity-option", "CN-SHFE-CU2701C102000"),
    ],
)
def test_option_bars_auto_chain(
    ths_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    asset_type: str,
    instrument: str,
) -> None:
    """CN 期权 bars（asset_type 三分，三子类同链）组合 ths 缺省首位（tushare
    凭据型兜底居链尾，测试环境凭据缺失自动跳过）：无 provider/variant 参数
    auto 直达。"""
    preset_payload(monkeypatch, envelope({"item": bar_rows("timestamp", [1767196800000])}))

    params = {
        "asset_type": asset_type,
        "instrument": instrument,
        "interval": "P1D",
        "start": "2026-01-01T00:00:00+08:00",
        "end": "2026-01-10T00:00:00+08:00",
    }
    response = ths_client.get("/v1/market/bars", params=params)
    assert response.status_code == 200

    meta = response.json()["meta"]
    assert meta["provider"] == "ths"
    assert meta["variant"] == "ths"  # default_variant 具体化回显
    assert response.json()["data"]["bars"][0]["close"] == 10.5


def test_capability_catalog(ths_client: TestClient) -> None:
    response = ths_client.get("/v1/capabilities?provider=ths")
    assert response.status_code == 200
    entry = response.json()["data"][0]
    assert entry["provider"] == "ths"
    assert entry["enabled"] is True
    assert len(entry["capabilities"]) == 14  # bars 7 块 + quotes 7 块（期权三子类拆三）

    bars_caps = [c for c in entry["capabilities"] if c["data_type"] == "bars"]
    assert {tuple(cap["asset_types"])[0] for cap in bars_caps} == {
        "stock",
        "index",
        "etf",
        "contract",
        "commodity-option",
        "index-option",
        "etf-option",
    }
    for cap in bars_caps:
        assert cap["variants"] == ["ths"]
        assert cap["markets"] == ["CN"]
        assert [i["code"] for i in cap["intervals"]] == ["P1D"]
        assert cap["fields"] == ["open", "high", "low", "close", "volume", "amount"]

    # 历史覆盖（探针实证回填）：stock 固定起点 ≈2005-01-15（聚焦二分
    # ±30 天粒度 approx）；
    # index/etf 为滚动窗口语义（retention_days，指数端点实测
    # 'recent five years only'）
    hist_by_asset = {
        tuple(cap["asset_types"])[0]: cap["intervals"][0]["history"] for cap in bars_caps
    }
    assert hist_by_asset["stock"]["start"] == "2005-01-15T00:00:00+08:00"
    assert hist_by_asset["stock"]["end"] is None
    assert hist_by_asset["stock"]["retention_days"] is None
    for asset in ("index", "etf"):
        assert hist_by_asset[asset]["start"] is None
        assert hist_by_asset[asset]["retention_days"] == 1825

    adjustments = {tuple(cap["asset_types"])[0]: cap["adjustments"] for cap in bars_caps}
    assert adjustments["stock"] == ["none", "qfq", "hfq"]  # 唯一三值复权资产
    # ETF 上游固定前复权（受控对比实证：无 adjust 参数、数据固定 qfq 口径）
    assert adjustments["etf"] == ["qfq"]
    for asset in ("index", "contract", "commodity-option", "index-option", "etf-option"):
        assert adjustments[asset] == ["none"]

    # 实时快照：无周期/复权维度（显式空）；期货/期权量额=分时全点求和
    # （逐分钟增量口径探针实证回填）
    quote_caps = [c for c in entry["capabilities"] if c["data_type"] == "quotes"]
    assert {tuple(cap["asset_types"])[0] for cap in quote_caps} == {
        "stock",
        "index",
        "etf",
        "contract",
        "commodity-option",
        "index-option",
        "etf-option",
    }
    for cap in quote_caps:
        assert cap["intervals"] == []
        assert cap["adjustments"] == []
    fields_by_asset = {tuple(cap["asset_types"])[0]: cap["fields"] for cap in quote_caps}
    for asset in (
        "stock", "index", "etf", "contract",
        "commodity-option", "index-option", "etf-option",
    ):
        assert fields_by_asset[asset] == ["timestamp", "last_price", "volume", "amount"]


def test_unsupported_interval_p1w_422(ths_client: TestClient) -> None:
    """上游历史 K 线端外仅日线：能力目录即不声明，请求返回 422。"""
    params = {**STOCK_PARAMS, "interval": "P1W"}
    response = ths_client.get("/v1/market/bars", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_CAPABILITY"


def test_bse_exchange_namespace_mapped(
    ths_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BSE→BJ 收编（探针价格端点实证）：北交所个股请求正常路由
    （原「未实证 422 拒绝」断言随收编反转）。"""
    preset_payload(
        monkeypatch,
        envelope({"item": bar_rows("date_ms", [1767196800000, 1767283200000])}),
    )
    params = {**STOCK_PARAMS, "instrument": "CN-BSE-430047"}
    response = ths_client.get("/v1/market/bars", params=params)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["instrument"] == "CN-BSE-430047"
    assert len(data["bars"]) == 2


def test_upstream_business_error_wrapped(
    ths_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """信封非 0 业务码（3001 标的不存在）→ UPSTREAM_ERROR 502，SDK 异常不穿透。"""
    preset_payload(monkeypatch, envelope(None, code=3001, message="标的不存在"))
    response = ths_client.get("/v1/market/bars", params=STOCK_PARAMS)
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "UPSTREAM_ERROR"


def test_missing_api_key_returns_clear_error(default_client) -> None:  # type: ignore[no-untyped-def]
    """未配置 API Key 时：Provider 不启用（能力目录可见），显式请求返回明确错误。"""
    caps = default_client.get("/v1/providers/ths/capabilities")
    assert caps.status_code == 200
    assert caps.json()["data"]["enabled"] is False

    response = default_client.get(
        "/v1/market/bars", params={**STOCK_PARAMS, "provider": "ths"}
    )
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "PROVIDER_DISABLED"
    assert "TAPEHUB_THS_API_KEY" in error["details"]["disabled_reason"]


# ---------------------------------------------------------------------------
# 实时快照链路（REST 单次调用语义）
# ---------------------------------------------------------------------------


def test_stock_quote_chain(ths_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    preset_payload(monkeypatch, envelope(snapshot_data(1767225600000, [stock_snapshot_item()])))

    response = ths_client.get("/v1/market/quotes", params=STOCK_QUOTE_PARAMS)
    assert response.status_code == 200
    body = response.json()

    data = body["data"]
    assert data["instrument"] == "CN-SSE-600519"
    assert data["asset_type"] == "stock"
    quote = data["quote"]
    assert quote["last_price"] == 1277.8
    assert quote["volume"] == 3098875.0
    assert quote["amount"] == 3937375200.0
    assert quote["extensions"]["prev_price"] == 1256.0  # 白名单透出
    assert quote["bid_price1"] is None  # 上游无盘口字段
    # data.timestamp 毫秒戳 → 北京时间
    assert quote["timestamp"] == "2026-01-01T08:00:00+08:00"

    meta = body["meta"]
    assert meta["provider"] == "ths"
    assert meta["variant"] == "ths"
    assert meta["received_at"].endswith("+08:00")


@pytest.mark.parametrize(
    ("asset_type", "instrument"),
    [
        ("index-option", "CN-CFFEX-IO2601-C-4000"),
        ("etf-option", "CN-SSE-10012437"),
        ("commodity-option", "CN-SHFE-CU2701C102000"),
    ],
)
def test_option_quote_auto_chain(
    ths_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    asset_type: str,
    instrument: str,
) -> None:
    """期权快照（分时末点，asset_type 三分后三子类同链 ths 独占）：auto 直达。"""
    preset_payload(
        monkeypatch,
        envelope(
            intraday_data(1767225600000, intraday_points([1767225600000], prices=[35.2]))
        ),
    )

    params = {
        "asset_type": asset_type,
        "instrument": instrument,
    }
    response = ths_client.get("/v1/market/quotes", params=params)
    assert response.status_code == 200

    quote = response.json()["data"]["quote"]
    assert quote["last_price"] == 35.2
    assert quote["volume"] == 12.0  # 单点序列求和 = 该点值（逐分钟增量口径）
    assert quote["amount"] == 4692000.0
    assert quote["extensions"] is None
    assert quote["timestamp"] == "2026-01-01T08:00:00+08:00"
    assert response.json()["meta"]["provider"] == "ths"


def test_quote_missing_api_key_returns_clear_error(default_client) -> None:  # type: ignore[no-untyped-def]
    """未配置 API Key：快照请求与 bars 同样返回 PROVIDER_DISABLED。"""
    response = default_client.get("/v1/market/quotes", params=STOCK_QUOTE_PARAMS)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "PROVIDER_DISABLED"
    assert "TAPEHUB_THS_API_KEY" in error["details"]["disabled_reason"]
