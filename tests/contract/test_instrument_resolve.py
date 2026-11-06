"""标的解析完整链路契约测试（API → Application → Domain 规则引擎）。

解析是纯本地规则：不访问 Provider、不访问上游；用 ``default_client``
（默认注册表即可，解析服务与 Provider 注册表无关）。
"""

from fastapi.testclient import TestClient


def test_resolve_600000(default_client: TestClient) -> None:
    response = default_client.get("/v1/instruments/resolve", params={"q": "600000"})
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["query"] == "600000"
    assert body["data"]["ambiguous"] is False
    assert body["data"]["candidates"] == [
        {
            "instrument": "CN-SSE-600000",
            "market": "CN",
            "exchange": "SSE",
            "symbol": "600000",
            "asset_type": "stock",
        }
    ]
    assert body["meta"]["request_id"].startswith("req_")


def test_resolve_ambiguous_000001(default_client: TestClient) -> None:
    """000001 双关：上证指数与平安银行双候选，ambiguous 标注。"""
    response = default_client.get("/v1/instruments/resolve", params={"q": "000001"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["ambiguous"] is True
    instruments = {item["instrument"] for item in data["candidates"]}
    assert instruments == {"CN-SSE-000001", "CN-SZSE-000001"}
    asset_types = {item["instrument"]: item["asset_type"] for item in data["candidates"]}
    assert asset_types == {"CN-SSE-000001": "index", "CN-SZSE-000001": "stock"}


def test_resolve_asset_type_hint_narrows(default_client: TestClient) -> None:
    response = default_client.get(
        "/v1/instruments/resolve", params={"q": "000001", "asset_type": "index"}
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["ambiguous"] is False
    assert [item["instrument"] for item in data["candidates"]] == ["CN-SSE-000001"]


def test_resolve_usdjpy(default_client: TestClient) -> None:
    response = default_client.get("/v1/instruments/resolve", params={"q": "USDJPY"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["candidates"] == [
        {
            "instrument": "GLOBAL-OTC-USD/JPY",
            "market": "GLOBAL",
            "exchange": "OTC",
            "symbol": "USD/JPY",
            "asset_type": "forex",
        }
    ]


def test_resolve_us_ticker_multi_candidates_and_exchange_hint(
    default_client: TestClient,
) -> None:
    multi = default_client.get("/v1/instruments/resolve", params={"q": "AAPL"})
    assert multi.status_code == 200
    data = multi.json()["data"]
    assert data["ambiguous"] is True
    assert {item["exchange"] for item in data["candidates"]} == {"NYSE", "NASDAQ", "CBOE"}

    narrowed = default_client.get(
        "/v1/instruments/resolve", params={"q": "AAPL", "exchange": "NASDAQ"}
    )
    assert narrowed.status_code == 200
    data = narrowed.json()["data"]
    assert data["ambiguous"] is False
    assert [item["instrument"] for item in data["candidates"]] == ["US-NASDAQ-AAPL"]


def test_resolve_unmatched_returns_empty_candidates(default_client: TestClient) -> None:
    """期货等不解析的输入：200 空候选，不报错、不猜测。"""
    response = default_client.get("/v1/instruments/resolve", params={"q": "cu2607"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["ambiguous"] is False
    assert data["candidates"] == []


def test_resolve_missing_query_is_invalid_request(default_client: TestClient) -> None:
    response = default_client.get("/v1/instruments/resolve")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_resolve_blank_query_returns_empty_candidates(default_client: TestClient) -> None:
    """空白输入与其它不可匹配输入一致：200 空候选（仅缺失 q 才 422）。"""
    response = default_client.get("/v1/instruments/resolve", params={"q": " "})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["ambiguous"] is False
    assert data["candidates"] == []


def test_resolve_works_without_provider_dependencies(fake_client: TestClient) -> None:
    """解析只读本地规则：与 Provider 注册表内容无关（Fake Provider 注册表）。"""
    response = fake_client.get("/v1/instruments/resolve", params={"q": "600000"})
    assert response.status_code == 200
    assert response.json()["data"]["candidates"][0]["instrument"] == "CN-SSE-600000"
