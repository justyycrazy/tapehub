"""可选入站 API Key 认证契约测试。

覆盖（docs/api.md「认证」）：

- 未配置（默认）：全部端点行为与历史版本一致（回归由既有契约测试承载，
  本文件仅显式锚定一条）；
- 已配置：无/错头 401（统一错误结构、状态码 401 而非查表改写）、正确头
  放行、``/health`` 保持无鉴权（连接测试用途）。
"""

import pytest
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


def _registry_with_api_keys():

    settings = make_settings(api_keys=["secret-key-1", "secret-key-2"])
    return create_app(settings=settings, registry=_registry())


def test_no_keys_configured_unauthenticated_by_default() -> None:
    """未配置 Key：行为与历史版本一致（无鉴权，业务端点直接可用）。"""
    settings = make_settings()
    app = create_app(settings=settings, registry=_registry())
    with TestClient(app) as client:
        resp = client.get("/v1/capabilities")
        assert resp.status_code == 200


def test_missing_header_rejected_401() -> None:
    with TestClient(_registry_with_api_keys()) as client:
        resp = client.get("/v1/capabilities")
        assert resp.status_code == 401
        body = resp.json()
        assert body["error"]["code"] == "INVALID_REQUEST"
        assert body["error"]["request_id"].startswith("req_")


def test_wrong_key_rejected_401() -> None:
    with TestClient(_registry_with_api_keys()) as client:
        resp = client.get(
            "/v1/capabilities", headers={"X-Api-Key": "wrong-key"}
        )
        assert resp.status_code == 401


def test_valid_key_accepted() -> None:
    with TestClient(_registry_with_api_keys()) as client:
        resp = client.get("/v1/capabilities", headers={"X-Api-Key": "secret-key-2"})
        assert resp.status_code == 200
        assert resp.json()["meta"]["request_id"].startswith("req_")


def test_second_key_also_accepted() -> None:
    """白名单多 Key 语义：任一匹配放行。"""
    with TestClient(_registry_with_api_keys()) as client:
        resp = client.get("/v1/capabilities", headers={"X-Api-Key": "secret-key-1"})
        assert resp.status_code == 200


def test_health_stays_unauthenticated() -> None:
    """/health 保持无鉴权（连接测试用途，docs/api.md 明示）。"""
    with TestClient(_registry_with_api_keys()) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["service"] == "tapehub"


@pytest.mark.parametrize(
    "path",
    [
        "/v1/capabilities",
        "/v1/providers/fake-provider/capabilities",
        "/v1/instruments/resolve?q=600000",
        "/v1/market/bars?provider=fake-provider&asset_type=stock&instrument=CN-SSE-600000"
        "&interval=P1D&start=2026-01-01T00:00:00%2B08:00&end=2026-01-02T00:00:00%2B08:00",
        "/v1/market/quotes?provider=fake-provider&asset_type=stock&instrument=CN-SSE-600000",
        # SSE 流受理：认证依赖在 service.open() 之前执行，401 于响应头前返回
        "/v1/market/quotes/stream?provider=fake-provider&asset_type=stock"
        "&instrument=CN-SSE-600000",
    ],
)
def test_all_business_endpoints_require_key(path: str) -> None:
    """全部业务端点（bars/quotes/流受理/capabilities/instruments）统一挂认证。"""
    with TestClient(_registry_with_api_keys()) as client:
        resp = client.get(path)
        assert resp.status_code == 401


def test_non_ascii_key_rejected_401_not_500() -> None:
    """非 ASCII 头值收敛为 401（compare_digest 的 str 形态要求 ASCII，
    str 直比会抛 TypeError 被兜底处理器误转 500——见 dependencies.py）。

    输入经 bytes 头值携带原始高位字节（\\xe9）：真实客户端可在 wire 上
    发送任意头字节，服务端 Starlette 按 latin-1 解码为非 ASCII str；测试
    客户端的 str 头值会被 httpx2 以 ASCII 编码拒绝（到不了服务端），
    bytes 是抵达服务端非 ASCII 解码结果的唯一通路。"""
    with TestClient(_registry_with_api_keys()) as client:
        resp = client.get("/v1/capabilities", headers={"X-Api-Key": b"k\xe9y"})
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] != "INTERNAL_ERROR"
