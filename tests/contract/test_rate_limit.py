"""入站客户端限流契约测试（审查项 M3）。

固定窗口（60 秒）语义：身份为命中白名单的 API Key，未命中/未配置一律
归 anonymous（桶数上界 = len(api_keys)+1）；超限 429 CLIENT_RATE_LIMITED
+ Retry-After；/health 豁免；默认 0 = 关闭（行为与历史版本一致）。

测试全部在同一 60 秒窗口内连发完成（用例秒级），不 mock 时钟。
"""

from fastapi.testclient import TestClient

from tapehub.api.app import create_app
from tests.conftest import fake_routing, make_settings


def _registry():
    from pathlib import Path

    from tapehub.providers.manifest import load_manifest_file
    from tapehub.providers.registry import ProviderRegistry, build_provider_record
    from tests.fixtures.providers.fake_provider import FakeBarProvider

    manifest_path = Path(__file__).parents[1] / "fixtures" / "manifests" / "fake_provider.yaml"
    manifest = load_manifest_file(manifest_path)
    record = build_provider_record(manifest.provider, manifest, FakeBarProvider)
    return ProviderRegistry([record], fake_routing())


def _client(**settings_kwargs: object) -> TestClient:
    app = create_app(settings=make_settings(**settings_kwargs), registry=_registry())
    return TestClient(app)


def test_limit_exceeded_returns_429_with_retry_after() -> None:
    with _client(api_keys=["k1"], client_rate_limit_per_minute=2) as client:
        headers = {"X-Api-Key": "k1"}
        assert client.get("/v1/capabilities", headers=headers).status_code == 200
        assert client.get("/v1/capabilities", headers=headers).status_code == 200
        third = client.get("/v1/capabilities", headers=headers)
        assert third.status_code == 429
        body = third.json()
        assert body["error"]["code"] == "CLIENT_RATE_LIMITED"
        assert body["error"]["details"]["limit"] == 2
        # 内层中间件语义：限流 429 仍携带 request_id（外层 RequestContext）
        assert third.headers.get("x-request-id", "").startswith("req_")
        retry_after = int(third.headers["Retry-After"])
        assert 1 <= retry_after <= 60


def test_identities_count_independently() -> None:
    with _client(api_keys=["k1", "k2"], client_rate_limit_per_minute=2) as client:
        for _ in range(2):
            assert client.get("/v1/capabilities", headers={"X-Api-Key": "k1"}).status_code == 200
        # k1 已满，k2 独立计数仍放行
        assert client.get("/v1/capabilities", headers={"X-Api-Key": "k2"}).status_code == 200
        # 未命中白名单的 Key：先计入 anonymous 桶（限流层在认证层之前），
        # 再被认证拒绝为 401
        resp = client.get("/v1/capabilities", headers={"X-Api-Key": "random-key"})
        assert resp.status_code == 401


def test_anonymous_global_bucket_without_keys() -> None:
    with _client(client_rate_limit_per_minute=2) as client:
        assert client.get("/v1/capabilities").status_code == 200
        assert client.get("/v1/capabilities").status_code == 200
        assert client.get("/v1/capabilities").status_code == 429


def test_disabled_by_default() -> None:
    with _client() as client:
        for _ in range(5):
            assert client.get("/v1/capabilities").status_code == 200


def test_health_exempt_from_rate_limit() -> None:
    with _client(api_keys=["k1"], client_rate_limit_per_minute=1) as client:
        headers = {"X-Api-Key": "k1"}
        assert client.get("/v1/capabilities", headers=headers).status_code == 200
        assert client.get("/v1/capabilities", headers=headers).status_code == 429
        # 业务端点已满限，/health 豁免仍可用（连接测试语义）
        assert client.get("/health").status_code == 200


def test_auth_failed_requests_consume_anonymous_quota() -> None:
    """限流先于认证执行：认证失败（401）的请求同样计入 anonymous 桶——
    凭据爆破/滥刷语义。随机 Key 不产生新桶（防基数爆炸）。"""
    with _client(api_keys=["k1"], client_rate_limit_per_minute=2) as client:
        # 两个不同的非法 Key（401），共用 anonymous 桶并各计一次
        assert client.get("/v1/capabilities", headers={"X-Api-Key": "bad1"}).status_code == 401
        assert client.get("/v1/capabilities", headers={"X-Api-Key": "bad2"}).status_code == 401
        # 第三个非法 Key：anonymous 桶已满 → 限流 429（先于认证的 401）
        third = client.get("/v1/capabilities", headers={"X-Api-Key": "bad3"})
        assert third.status_code == 429
        assert third.json()["error"]["code"] == "CLIENT_RATE_LIMITED"
