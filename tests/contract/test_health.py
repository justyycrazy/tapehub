"""健康检查契约测试。"""

from tapehub import __version__


def test_health_returns_ok(default_client) -> None:  # type: ignore[no-untyped-def]
    response = default_client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "tapehub"
    assert body["version"] == __version__
    assert body["api_version"] == "v1"
    assert body["timestamp"].endswith("Z")


def test_health_has_request_id_header(default_client) -> None:  # type: ignore[no-untyped-def]
    response = default_client.get("/health")
    assert response.headers["x-request-id"].startswith("req_")


def test_health_and_capabilities_do_not_call_providers(counting_client, counting_registry) -> None:  # type: ignore[no-untyped-def]
    _, counting = counting_registry
    assert counting_client.get("/health").status_code == 200
    assert counting_client.get("/v1/capabilities").status_code == 200
    assert (
        counting_client.get("/v1/providers/fake-provider/capabilities").status_code == 200
    )
    # 健康检查与能力查询都不访问任何 Provider
    assert counting.calls == 0


def test_app_boots_without_any_provider_sdk(default_client) -> None:  # type: ignore[no-untyped-def]
    """未安装任何 Provider SDK 时，应用仍能启动并响应。"""
    assert default_client.get("/health").status_code == 200
