"""统一错误结构契约测试。"""


def test_error_shape_unified(fake_client) -> None:  # type: ignore[no-untyped-def]
    response = fake_client.get(
        "/v1/market/bars",
        params={
            "provider": "fake-provider",
            "asset_type": "stock",
            "instrument": "bad-instrument",
            "interval": "P1D",
            "start": "2026-01-01",
            "end": "2026-01-31",
        },
    )
    assert response.status_code == 422
    body = response.json()
    assert set(body.keys()) == {"error"}
    error = body["error"]
    assert set(error.keys()) == {"code", "message", "request_id", "details"}
    assert error["code"] == "INVALID_REQUEST"
    assert isinstance(error["message"], str) and error["message"]
    assert error["request_id"].startswith("req_")
    assert isinstance(error["details"], dict)


def test_request_id_echoed_in_header(fake_client) -> None:  # type: ignore[no-untyped-def]
    response = fake_client.get(
        "/v1/market/bars",
        params={
            "provider": "fake-provider",
            "asset_type": "stock",
            "instrument": "CN-SSE-600000",
            "interval": "P1D",
            "start": "2026-01-01",
            "end": "2026-01-31",
            "limit": 0,
        },
    )
    assert response.status_code == 422
    assert response.headers["x-request-id"] == response.json()["error"]["request_id"]


def test_unknown_route_unified(default_client) -> None:  # type: ignore[no-untyped-def]
    response = default_client.get("/definitely/not/here")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "NOT_FOUND"
    assert error["request_id"].startswith("req_")


def test_method_not_allowed_unified(default_client) -> None:  # type: ignore[no-untyped-def]
    response = default_client.post("/health")
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_no_internal_details_leaked(fake_client) -> None:  # type: ignore[no-untyped-def]
    response = fake_client.get("/v1/market/bars", params={"asset_type": "stock"})
    text = response.text.lower()
    assert "traceback" not in text
    assert "exception" not in text
    assert "token" not in text
