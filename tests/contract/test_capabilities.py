"""能力目录契约测试。"""

from tests.conftest import BUILTIN_PROVIDER_NAMES


def test_default_catalog_lists_all_builtin_providers(default_client) -> None:  # type: ignore[no-untyped-def]
    response = default_client.get("/v1/capabilities")
    assert response.status_code == 200
    body = response.json()
    names = [entry["provider"] for entry in body["data"]]
    assert names == BUILTIN_PROVIDER_NAMES
    for entry in body["data"]:
        assert entry["runtime_status"] == "unknown"
    # akshare 已接入：启用且能力目录非空
    akshare = body["data"][0]
    assert akshare["provider"] == "akshare"
    assert akshare["enabled"] is True
    assert len(akshare["capabilities"]) >= 7
    # 个股按变体拆分为三条能力声明（东财/新浪/腾讯）
    stock_caps = [cap for cap in akshare["capabilities"] if cap["asset_types"] == ["stock"]]
    assert {cap["variants"][0] for cap in stock_caps} == {"eastmoney", "sina", "tencent"}
    em_stock = next(cap for cap in stock_caps if cap["variants"] == ["eastmoney"])
    assert [i["code"] for i in em_stock["intervals"]] == ["P1D", "P1W", "P1M"]
    assert em_stock["adjustments"] == ["none", "qfq", "hfq"]
    # tickflow 已接入：启用且能力目录非空
    tickflow = next(entry for entry in body["data"] if entry["provider"] == "tickflow")
    assert tickflow["enabled"] is True
    assert len(tickflow["capabilities"]) == 1
    assert [i["code"] for i in tickflow["capabilities"][0]["intervals"]][0] == "PT1M"
    # 其余两个 Provider 尚未接入
    for entry in body["data"]:
        if entry["provider"] in ("akshare", "tickflow", "alphafeed"):
            continue
        assert entry["enabled"] is False
        assert entry["capabilities"] == []
    meta = body["meta"]
    assert meta["request_id"].startswith("req_")
    assert meta["generated_at"].endswith("Z")


def test_fake_provider_entry(fake_client) -> None:  # type: ignore[no-untyped-def]
    body = fake_client.get("/v1/capabilities").json()
    assert len(body["data"]) == 1
    entry = body["data"][0]
    assert entry["provider"] == "fake-provider"
    assert entry["enabled"] is True
    assert len(entry["capabilities"]) == 1
    cap = entry["capabilities"][0]
    assert cap["data_type"] == "bars"
    assert cap["markets"] == ["CN"]
    assert cap["asset_types"] == ["stock", "etf", "index", "contract"]
    assert cap["variants"] == ["eastmoney"]
    assert [i["code"] for i in cap["intervals"]] == ["P1D", "PT1M"]
    assert all(i["history"] == {"start": None, "end": None} for i in cap["intervals"])
    assert cap["adjustments"] == ["none"]


def test_filters(fake_client) -> None:  # type: ignore[no-untyped-def]
    def names(query: str) -> list[str]:
        body = fake_client.get(f"/v1/capabilities{query}").json()
        return [entry["provider"] for entry in body["data"]]

    assert names("?provider=fake-provider") == ["fake-provider"]
    assert names("?provider=nope") == []
    assert names("?asset_type=stock") == ["fake-provider"]
    assert names("?data_type=bars") == ["fake-provider"]
    assert names("?market=CN") == ["fake-provider"]
    assert names("?market=US") == []
    assert names("?interval=PT1M") == ["fake-provider"]
    assert names("?interval=P1W") == []  # fake 不支持周周期
    assert names("?variant=eastmoney") == ["fake-provider"]
    assert names("?variant=sina") == []  # fake 仅声明 eastmoney 变体


def test_invalid_interval_filter_returns_unified_error(fake_client) -> None:  # type: ignore[no-untyped-def]
    response = fake_client.get("/v1/capabilities", params={"interval": "1d"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_single_provider_capabilities(fake_client) -> None:  # type: ignore[no-untyped-def]
    response = fake_client.get("/v1/providers/fake-provider/capabilities")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["provider"] == "fake-provider"
    assert body["data"]["enabled"] is True

    missing = fake_client.get("/v1/providers/nope/capabilities")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "PROVIDER_NOT_FOUND"


def test_single_provider_on_default_catalog(default_client) -> None:  # type: ignore[no-untyped-def]
    response = default_client.get("/v1/providers/tushare/capabilities")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["provider"] == "tushare"
    assert body["data"]["enabled"] is False
    assert body["data"]["capabilities"] == []
