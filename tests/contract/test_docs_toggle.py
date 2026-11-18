"""交互式文档开关契约测试（审查项 L3）。

- 默认开启：/openapi.json 可用（锚定现状）；
- TAPEHUB_DOCS_ENABLED=false：/docs、/redoc、/openapi.json 整体下线
  （统一 404 NOT_FOUND 结构）——对外暴露时不向非可信主体披露 API 结构。
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


def test_docs_enabled_by_default() -> None:
    app = create_app(settings=make_settings(), registry=_registry())
    with TestClient(app) as client:
        assert client.get("/openapi.json").status_code == 200


def test_docs_disabled_returns_unified_404() -> None:
    app = create_app(settings=make_settings(docs_enabled=False), registry=_registry())
    with TestClient(app) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            resp = client.get(path)
            assert resp.status_code == 404
            assert resp.json()["error"]["code"] == "NOT_FOUND"
