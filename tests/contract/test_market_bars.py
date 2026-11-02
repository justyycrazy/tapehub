"""历史 K 线完整链路契约测试。

时间范围：2026-01-01 至 2026-01-31（闭区间），Fake Provider 日线共 31 根。
Fake Provider 仅声明 adjust=none，因此测试显式传参；
「未传递 adjust」的默认值行为（个股 qfq）由 akshare 链路测试覆盖。
"""

BAR_PARAMS = {
    "asset_type": "stock",
    "instrument": "CN-SSE-600000",
    "interval": "P1D",
    "start": "2026-01-01",
    "end": "2026-01-31",
    "adjust": "none",
}


def test_full_chain_with_fake_provider(fake_client) -> None:  # type: ignore[no-untyped-def]
    response = fake_client.get("/v1/market/bars", params=BAR_PARAMS)
    assert response.status_code == 200
    body = response.json()
    assert body["warnings"] == []

    data = body["data"]
    assert data["instrument"] == "CN-SSE-600000"
    assert data["asset_type"] == "stock"
    assert data["interval"] == "P1D"
    assert len(data["bars"]) == 31
    # 闭区间边界：第一根与最后一根都在请求范围内
    assert data["bars"][0]["timestamp"] == "2026-01-01T00:00:00Z"
    assert data["bars"][-1]["timestamp"] == "2026-01-31T00:00:00Z"
    timestamps = [bar["timestamp"] for bar in data["bars"]]
    assert timestamps == sorted(timestamps)

    meta = body["meta"]
    assert meta["provider"] == "fake-provider"  # 响应 provider 是实际执行的数据源
    assert meta["offset"] == 0
    assert meta["limit"] == 500
    assert meta["count"] == 31
    assert meta["partial"] is False
    assert meta["request_id"].startswith("req_")
    assert meta["received_at"].endswith("Z")


def test_auto_resolves_fake_provider(fake_client) -> None:  # type: ignore[no-untyped-def]
    response = fake_client.get("/v1/market/bars", params={**BAR_PARAMS, "provider": "auto"})
    assert response.status_code == 200
    assert response.json()["meta"]["provider"] == "fake-provider"


def test_pagination_window(fake_client) -> None:  # type: ignore[no-untyped-def]
    response = fake_client.get(
        "/v1/market/bars", params={**BAR_PARAMS, "offset": 2, "limit": 3}
    )
    assert response.status_code == 200
    body = response.json()
    timestamps = [bar["timestamp"] for bar in body["data"]["bars"]]
    assert timestamps == ["2026-01-03T00:00:00Z", "2026-01-04T00:00:00Z", "2026-01-05T00:00:00Z"]
    # 适配器已完成分页，Application 不得再次切片：
    # 若 offset 被重复应用，count 将小于 3
    assert body["meta"]["count"] == 3
    assert body["meta"]["offset"] == 2
    assert body["meta"]["limit"] == 3


def test_offset_tail(fake_client) -> None:  # type: ignore[no-untyped-def]
    response = fake_client.get(
        "/v1/market/bars", params={**BAR_PARAMS, "offset": 30, "limit": 10}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["count"] == 1
    assert body["data"]["bars"][0]["timestamp"] == "2026-01-31T00:00:00Z"


def test_limit_capped_by_settings(fake_client) -> None:  # type: ignore[no-untyped-def]
    response = fake_client.get("/v1/market/bars", params={**BAR_PARAMS, "limit": 1000})
    assert response.status_code == 200
    assert response.json()["meta"]["count"] == 31
    assert response.json()["meta"]["limit"] == 1000


def test_minute_interval(fake_client) -> None:  # type: ignore[no-untyped-def]
    params = {
        "provider": "fake-provider",
        "asset_type": "stock",
        "instrument": "CN-SSE-600000",
        "interval": "PT1M",
        "start": "2026-01-01T00:00:00Z",
        "end": "2026-01-01T00:05:00Z",
        "adjust": "none",
    }
    response = fake_client.get("/v1/market/bars", params=params)
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["interval"] == "PT1M"
    assert body["meta"]["count"] == 6  # 闭区间 [00:00, 00:05]


def test_timezone_aware_bounds(fake_client) -> None:  # type: ignore[no-untyped-def]
    params = {
        "provider": "fake-provider",
        "asset_type": "stock",
        "instrument": "CN-SSE-600000",
        "interval": "P1D",
        "start": "2026-01-01T00:00:00+08:00",
        "end": "2026-01-03T00:00:00+08:00",
        "adjust": "none",
    }
    response = fake_client.get("/v1/market/bars", params=params)
    assert response.status_code == 200
    body = response.json()
    # +08:00 边界转换为 UTC 后闭区间为 [2025-12-31T16:00Z, 2026-01-02T16:00Z]，
    # 按日步进且端点包含，共 3 根
    timestamps = [bar["timestamp"] for bar in body["data"]["bars"]]
    assert timestamps == [
        "2025-12-31T16:00:00Z",
        "2026-01-01T16:00:00Z",
        "2026-01-02T16:00:00Z",
    ]


def test_other_asset_types(fake_client) -> None:  # type: ignore[no-untyped-def]
    for asset_type in ("etf", "index", "contract"):
        params = {**BAR_PARAMS, "asset_type": asset_type}
        response = fake_client.get("/v1/market/bars", params=params)
        assert response.status_code == 200
        body = response.json()
        assert body["data"]["asset_type"] == asset_type
        assert body["meta"]["provider"] == "fake-provider"


class TestRequestValidation:
    """统一参数错误：422 + INVALID_REQUEST。"""

    def _post(self, fake_client, **overrides):  # type: ignore[no-untyped-def]
        params = {**BAR_PARAMS, "provider": "fake-provider", **overrides}
        params = {k: v for k, v in params.items() if v is not None}
        return fake_client.get("/v1/market/bars", params=params)

    def test_missing_asset_type(self, fake_client) -> None:  # type: ignore[no-untyped-def]
        params = {k: v for k, v in BAR_PARAMS.items() if k != "asset_type"}
        response = fake_client.get("/v1/market/bars", params=params)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_REQUEST"

    def test_invalid_instrument(self, fake_client) -> None:  # type: ignore[no-untyped-def]
        for instrument in ("600000", "CN.SSE.600000", "cn-sse-600000"):
            response = self._post(fake_client, instrument=instrument)
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "INVALID_REQUEST"

    def test_start_not_earlier_than_end(self, fake_client) -> None:  # type: ignore[no-untyped-def]
        assert self._post(fake_client, start="2026-01-31", end="2026-01-01").status_code == 422
        assert self._post(fake_client, start="2026-01-01", end="2026-01-01").status_code == 422

    def test_naive_datetime_rejected(self, fake_client) -> None:  # type: ignore[no-untyped-def]
        response = self._post(fake_client, start="2026-01-01T00:00:00", end="2026-01-31T00:00:00")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_REQUEST"

    def test_invalid_interval(self, fake_client) -> None:  # type: ignore[no-untyped-def]
        for interval in ("1d", "tick", "PT"):
            assert self._post(fake_client, interval=interval).status_code == 422

    def test_offset_negative(self, fake_client) -> None:  # type: ignore[no-untyped-def]
        assert self._post(fake_client, offset=-1).status_code == 422

    def test_limit_zero_or_exceeds_max(self, fake_client) -> None:  # type: ignore[no-untyped-def]
        assert self._post(fake_client, limit=0).status_code == 422
        assert self._post(fake_client, limit=1001).status_code == 422

    def test_invalid_adjust(self, fake_client) -> None:  # type: ignore[no-untyped-def]
        assert self._post(fake_client, adjust="bogus").status_code == 422


class TestUnsupportedCapability:
    """显式指定 Provider 时不支持的能力返回 UNSUPPORTED_CAPABILITY。"""

    def test_unsupported_interval(self, fake_client) -> None:  # type: ignore[no-untyped-def]
        params = {**BAR_PARAMS, "provider": "fake-provider", "interval": "P1W"}
        response = fake_client.get("/v1/market/bars", params=params)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "UNSUPPORTED_CAPABILITY"

    def test_unsupported_adjust(self, fake_client) -> None:  # type: ignore[no-untyped-def]
        params = {**BAR_PARAMS, "provider": "fake-provider", "adjust": "qfq"}
        response = fake_client.get("/v1/market/bars", params=params)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "UNSUPPORTED_CAPABILITY"

    def test_operation_gap_surfaces_as_unsupported(self, fake_client) -> None:  # type: ignore[no-untyped-def]
        # etf + PT1M 在能力目录级别可匹配，但适配器没有对应内部 operation
        params = {
            **BAR_PARAMS,
            "provider": "fake-provider",
            "asset_type": "etf",
            "interval": "PT1M",
        }
        response = fake_client.get("/v1/market/bars", params=params)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "UNSUPPORTED_CAPABILITY"

    def test_auto_without_candidates_returns_no_provider(self, fake_client) -> None:  # type: ignore[no-untyped-def]
        # auto 路由只从完整匹配的候选中选择；无候选时返回 NO_PROVIDER_AVAILABLE
        response = fake_client.get("/v1/market/bars", params={**BAR_PARAMS, "interval": "P1W"})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "NO_PROVIDER_AVAILABLE"


class TestRoutingErrors:
    """默认应用：五个 Provider 全部接入，但凭据/SDK 均未配置。"""

    PARAMS = {**BAR_PARAMS, "instrument": "CN-SSE-600000"}

    def test_unknown_provider(self, default_client) -> None:  # type: ignore[no-untyped-def]
        response = default_client.get("/v1/market/bars", params={**self.PARAMS, "provider": "nope"})
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "PROVIDER_NOT_FOUND"

    def test_auto_without_candidates(self, default_client) -> None:  # type: ignore[no-untyped-def]
        # 没有任何 Provider 支持期权，auto 路由无候选
        params = {**self.PARAMS, "asset_type": "option", "instrument": "CN-SSE-10000001"}
        response = default_client.get("/v1/market/bars", params=params)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "NO_PROVIDER_AVAILABLE"

    def test_auto_selects_first_candidate_but_sdk_missing(self, default_client) -> None:  # type: ignore[no-untyped-def]
        """auto 选中 akshare（注册顺序第一）后，SDK 未安装必须返回明确错误。"""
        response = default_client.get("/v1/market/bars", params=self.PARAMS)
        assert response.status_code == 501
        assert response.json()["error"]["code"] == "PROVIDER_NOT_IMPLEMENTED"

    def test_tqsdk_explicit_missing_auth(self, default_client) -> None:  # type: ignore[no-untyped-def]
        """显式指定 tqsdk 时，凭据未配置返回带 setting 提示的明确错误。"""
        params = {
            **self.PARAMS,
            "provider": "tqsdk",
            "asset_type": "contract",
            "instrument": "CN-SHFE-cu2607",
        }
        response = default_client.get("/v1/market/bars", params=params)
        assert response.status_code == 501
        error = response.json()["error"]
        assert error["code"] == "PROVIDER_NOT_IMPLEMENTED"
        assert error["details"]["missing"] == ["user", "password"]

    def test_no_fake_bars_from_missing_sdk(self, default_client) -> None:  # type: ignore[no-untyped-def]
        """真实数据不可用时，绝不返回写死的模拟行情。"""
        params = {**self.PARAMS, "provider": "akshare"}
        response = default_client.get("/v1/market/bars", params=params)
        assert response.status_code == 501
        assert "bars" not in response.json().get("data", {})
