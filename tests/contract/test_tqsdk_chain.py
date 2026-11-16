"""TqSdk 完整 HTTP 链路契约测试（SDK 用桩 Mock，无真实网络）。

覆盖历史 K 线（bars）与实时快照（quotes）两条链路；
sync 桩仅覆盖 per_request 路径，shared 语义由
tests/unit/test_tqsdk_runtime.py 的假 SDK（tick 驱动模型）覆盖。
"""


import pytest
from fastapi.testclient import TestClient

from tapehub.api.app import create_app
from tapehub.providers.registry import build_default_registry
from tests.conftest import make_settings
from tests.fixtures.providers.stub_tqsdk import (
    DEFAULT_QUOTE_FIELDS,
    StubTqApi,
    install,
    klines_rows,
    preset_quote,
    preset_rows,
)

BASE_PARAMS = {
    "provider": "tqsdk",
    "asset_type": "contract",
    "instrument": "CN-SHFE-CU2607",
    "interval": "P1D",
    # 时间边界必须带时区偏移；CN 标的统一用北京时间表达
    "start": "2026-01-01T00:00:00+08:00",
    "end": "2026-01-10T00:00:00+08:00",
    # 显式传 variant=tqsdk（缺省时按 default_variant 解析为同名变体）
    "variant": "tqsdk",
}


@pytest.fixture()
def tqsdk_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    install(monkeypatch)
    # sync stub 仅覆盖 per_request 路径；shared 语义由 tests/unit/test_tqsdk_runtime.py 覆盖
    settings = make_settings(
        max_limit=1000,
        tqsdk_conn_mode="per_request",
        tqsdk_auth_user="user@qq.com",
        tqsdk_auth_password="secret",
    )
    with TestClient(
        create_app(settings=settings, registry=build_default_registry(settings))
    ) as client:
        yield client


def test_full_chain_continuous(
    tqsdk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """主力连续链路：KQ.m@ 符号 + 无成交额字段。"""
    preset_rows(monkeypatch, klines_rows(["2026-01-02", "2026-01-05"]))

    params = {
        **BASE_PARAMS,
        "asset_type": "continuous",
        "instrument": "CN-SHFE-CU",
    }
    response = tqsdk_client.get("/v1/market/bars", params=params)
    assert response.status_code == 200
    body = response.json()

    api = StubTqApi.instances[-1]
    assert api.auth is not None
    assert api.auth.user == "user@qq.com"
    assert api.calls[0]["symbol"] == "KQ.m@SHFE.cu"

    data = body["data"]
    assert data["instrument"] == "CN-SHFE-CU"
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
        "commodity-option": "tqsdk",
        "index-option": "tqsdk",
        "etf-option": "tqsdk",
    }
    # 期货三类拆分 + 股票分钟族/股票 PT30M+（复权收窄拆分）+ 指数 +
    # ETF 分钟族/ETF PT30M+ + 期权三子类（asset_type 三分，bars 拆三块）+ quotes
    assert len(entry["capabilities"]) == 12
    cap = entry["capabilities"][0]
    assert cap["data_type"] == "bars"
    assert cap["asset_types"] == ["contract"]
    assert cap["variants"] == ["tqsdk"]
    assert [i["code"] for i in cap["intervals"]] == [
        "PT1M", "PT5M", "PT15M", "PT30M", "PT1H", "P1D",
    ]
    assert cap["adjustments"] == ["none"]
    assert cap["fields"] == ["open", "high", "low", "close", "volume"]
    # quotes 能力：无周期/复权维度（两维度为空），覆盖九类资产（含期权三子类）
    quotes_cap = next(c for c in entry["capabilities"] if c["data_type"] == "quotes")
    assert quotes_cap["intervals"] == []
    assert quotes_cap["adjustments"] == []
    assert set(quotes_cap["asset_types"]) == {
        "contract", "continuous", "weighted-index", "stock", "index", "etf",
        "commodity-option", "index-option", "etf-option",
    }
    assert quotes_cap["variants"] == ["tqsdk"]
    assert "last_price" in quotes_cap["fields"]
    assert "ask_price5" in quotes_cap["fields"]
    # 探针回填的 history.start 在能力目录可见（continuous 日线）
    continuous_cap = next(
        c for c in entry["capabilities"] if c["asset_types"] == ["continuous"]
    )
    p1d = next(i for i in continuous_cap["intervals"] if i["code"] == "P1D")
    assert p1d["history"]["start"] == "2016-01-05T00:00:00+08:00"
    # 分钟级滚动保留以 retention_days 声明（探针实测向下取整）
    stock_cap = next(c for c in entry["capabilities"] if c["asset_types"] == ["stock"])
    pt1m = next(i for i in stock_cap["intervals"] if i["code"] == "PT1M")
    assert pt1m["history"] == {
        "start": None,
        "end": None,
        "retention_days": 35,
    }


def test_missing_auth_returns_clear_error(default_client) -> None:  # type: ignore[no-untyped-def]
    """未配置快期账户时：Provider 不启用（能力目录可见），显式请求返回明确错误。"""
    caps = default_client.get("/v1/providers/tqsdk/capabilities")
    assert caps.status_code == 200
    assert caps.json()["data"]["enabled"] is False

    response = default_client.get("/v1/market/bars", params=BASE_PARAMS)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "PROVIDER_DISABLED"
    assert "TAPEHUB_TQSDK_AUTH_USER" in error["details"]["disabled_reason"]
    assert "TAPEHUB_TQSDK_AUTH_PASSWORD" in error["details"]["disabled_reason"]


def test_option_bars_chain(
    tqsdk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """期权链路（asset_type 三分，2026-11-16 八所探针实证收编）：品种字母段
    小写还原（commodity-option 五所）。"""
    preset_rows(monkeypatch, klines_rows(["2026-01-02", "2026-01-05"]))

    params = {
        **BASE_PARAMS,
        "asset_type": "commodity-option",
        "instrument": "CN-SHFE-CU2701C102000",
    }
    response = tqsdk_client.get("/v1/market/bars", params=params)
    assert response.status_code == 200
    body = response.json()

    api = StubTqApi.instances[-1]
    assert api.calls[0]["symbol"] == "SHFE.cu2701C102000"

    data = body["data"]
    assert data["instrument"] == "CN-SHFE-CU2701C102000"
    assert data["asset_type"] == "commodity-option"
    assert len(data["bars"]) == 2
    assert data["bars"][0]["volume"] == 500.0


def test_weighted_index_chain(
    tqsdk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    preset_rows(monkeypatch, klines_rows(["2026-01-02"]))
    params = {
        **BASE_PARAMS,
        "asset_type": "weighted-index",
        "instrument": "CN-SHFE-BU",
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


def test_etf_explicit_provider_chain(
    tqsdk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ETF 已移出 CN auto 回退链（重排后 baostock 链首承接）：
    tqsdk ETF 映射经显式 provider 覆盖。"""
    preset_rows(monkeypatch, klines_rows(["2026-01-02"]))

    params = {
        **BASE_PARAMS,
        "asset_type": "etf",
        "instrument": "CN-SSE-510050",
        # tqsdk ETF 仅声明 none；缺省 qfq（2026-11-15 起 ETF 与 stock 同缺省）
        # 会被能力过滤，显式传 none 走通
        "adjust": "none",
    }
    response = tqsdk_client.get("/v1/market/bars", params=params)
    assert response.status_code == 200
    meta = response.json()["meta"]
    assert meta["provider"] == "tqsdk"
    assert meta["variant"] == "tqsdk"
    assert StubTqApi.instances[-1].calls[0]["symbol"] == "SSE.510050"


# ---------------------------------------------------------------------------
# 实时快照链路（GET /v1/market/quotes；per_request 桩路径）
# ---------------------------------------------------------------------------

QUOTE_PARAMS = {
    "provider": "tqsdk",
    "asset_type": "contract",
    "instrument": "CN-SHFE-AU2612",
    "variant": "tqsdk",
}


def test_quote_full_chain(
    tqsdk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """合约快照链路：SHFE.au2612 映射 + P5 实测字段白名单。"""
    preset_quote(monkeypatch, DEFAULT_QUOTE_FIELDS)

    response = tqsdk_client.get("/v1/market/quotes", params=QUOTE_PARAMS)
    assert response.status_code == 200
    body = response.json()

    api = StubTqApi.instances[-1]
    assert api.quote_calls == ["SHFE.au2612"]

    data = body["data"]
    assert data["instrument"] == "CN-SHFE-AU2612"
    assert data["asset_type"] == "contract"
    quote = data["quote"]
    # 行情时间统一北京时间（上游为北京时间墙钟字符串）
    assert quote["timestamp"] == "2026-11-04T10:14:46.500000+08:00"
    assert quote["last_price"] == 848.24
    assert quote["bid_price1"] == 848.26
    assert quote["ask_price5"] == 848.6
    assert quote["bid_volume5"] == 1
    assert quote["volume"] == 200250.0
    assert quote["amount"] == 169906871060.0
    # extensions 白名单：P5 实证字段；close/settlement 等未核实字段不透出
    assert quote["extensions"] == {
        "open": 846.0,
        "highest": 853.64,
        "lowest": 843.14,
        "average": 848.47,
        "pre_settlement": 846.12,
        "pre_close": 841.8,
        "open_interest": 160306.0,
        "pre_open_interest": 160467.0,
        "upper_limit": 964.56,
        "lower_limit": 727.66,
    }

    meta = body["meta"]
    assert meta["provider"] == "tqsdk"
    assert meta["variant"] == "tqsdk"
    assert meta["received_at"].endswith("+08:00")


def test_quote_index_without_order_book(
    tqsdk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """指数快照：订阅成功但无盘口（P5 实测），bid/ask 价格 null、量 0 如实透出。"""
    nan = float("nan")
    preset_quote(
        monkeypatch,
        {
            **DEFAULT_QUOTE_FIELDS,
            "last_price": 2752.93,
            **{f"{side}_price{i}": nan for side in ("bid", "ask") for i in range(1, 6)},
            **{f"{side}_volume{i}": 0 for side in ("bid", "ask") for i in range(1, 6)},
            "open_interest": 0,
            "pre_open_interest": 0,
            "upper_limit": nan,
            "lower_limit": nan,
            "pre_settlement": nan,
            "volume": 17607537,
            "amount": 44721284722.9,
        },
    )
    params = {**QUOTE_PARAMS, "asset_type": "index", "instrument": "CN-SSE-000016"}
    response = tqsdk_client.get("/v1/market/quotes", params=params)
    assert response.status_code == 200
    quote = response.json()["data"]["quote"]
    assert quote["last_price"] == 2752.93
    assert quote["bid_price1"] is None  # NaN → null，不用 0 冒充
    assert quote["ask_price5"] is None
    assert quote["bid_volume1"] == 0.0  # 量侧真实 0 保留
    # 恒为 0/NaN 的期货语义字段按值透出；NaN（upper/lower/pre_settlement）剔除
    assert quote["extensions"]["open_interest"] == 0.0
    assert "upper_limit" not in quote["extensions"]
    assert "pre_settlement" not in quote["extensions"]


def test_quote_auto_routes_and_symbol_mapping(
    tqsdk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """auto 路由命中 quotes 能力；主连映射 KQ.m@ 前缀。"""
    preset_quote(monkeypatch, DEFAULT_QUOTE_FIELDS)
    params = {
        "asset_type": "continuous",
        "instrument": "CN-SHFE-CU",
        # provider/variant 缺省：auto + default_variant（tqsdk）具体化
    }
    response = tqsdk_client.get("/v1/market/quotes", params=params)
    assert response.status_code == 200
    meta = response.json()["meta"]
    assert meta["provider"] == "tqsdk"
    assert meta["variant"] == "tqsdk"
    assert StubTqApi.instances[-1].quote_calls == ["KQ.m@SHFE.cu"]


def test_quote_option_chain(
    tqsdk_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """期权快照链路（asset_type 三分，2026-11-16 八所探针实证收编）：数字
    合约码直拼（etf-option 沪深两所）。"""
    preset_quote(monkeypatch, DEFAULT_QUOTE_FIELDS)

    params = {**QUOTE_PARAMS, "asset_type": "etf-option", "instrument": "CN-SSE-10012437"}
    response = tqsdk_client.get("/v1/market/quotes", params=params)
    assert response.status_code == 200
    body = response.json()

    api = StubTqApi.instances[-1]
    assert api.quote_calls == ["SSE.10012437"]
    data = body["data"]
    assert data["instrument"] == "CN-SSE-10012437"
    assert data["asset_type"] == "etf-option"
    assert data["quote"]["last_price"] is not None


def test_quote_missing_auth_returns_clear_error(default_client) -> None:  # type: ignore[no-untyped-def]
    """未配置快期账户：快照请求与 bars 同样返回 PROVIDER_DISABLED。"""
    response = default_client.get("/v1/market/quotes", params=QUOTE_PARAMS)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "PROVIDER_DISABLED"
    assert "TAPEHUB_TQSDK_AUTH_USER" in error["details"]["disabled_reason"]
    assert "TAPEHUB_TQSDK_AUTH_PASSWORD" in error["details"]["disabled_reason"]
