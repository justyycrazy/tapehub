"""架构边界测试：依赖方向与职责分离由源码扫描静态保证。

- Application 层不得出现具体 Provider 名称的条件判断；
- Domain 层不得依赖 FastAPI、PyYAML 或 Provider 适配器；
- Provider 适配器不得依赖 FastAPI 请求上下文；
- 上游 SDK 函数名不得出现在 Application/API 层。
"""

import re
from pathlib import Path

SRC_DIR = Path(__file__).parents[2] / "src" / "tapehub"

PROVIDER_NAMES = ("akshare", "tushare", "tickflow", "alphafeed", "tqsdk", "twelvedata")


def _read(rel: str) -> str:
    return (SRC_DIR / rel).read_text(encoding="utf-8")


def _package_files(rel: str) -> list[Path]:
    return sorted((SRC_DIR / rel).glob("*.py"))


def test_application_has_no_provider_specific_branching() -> None:
    for path in _package_files("application"):
        text = path.read_text(encoding="utf-8")
        for name in PROVIDER_NAMES:
            assert name not in text.lower(), f"{path.name} must not mention provider {name}"


def test_application_does_not_import_fastapi() -> None:
    for path in _package_files("application"):
        text = path.read_text(encoding="utf-8")
        assert "fastapi" not in text.lower(), f"{path.name} must not depend on FastAPI"


def test_domain_has_no_infrastructure_imports() -> None:
    forbidden = ("fastapi", "yaml", "uvicorn", "from tapehub.providers", "from tapehub.api")
    for path in _package_files("domain"):
        text = path.read_text(encoding="utf-8")
        for needle in forbidden:
            assert needle not in text, f"domain/{path.name} must not reference {needle!r}"


def test_adapters_do_not_import_fastapi_or_sdk() -> None:
    for name in PROVIDER_NAMES:
        adapter = SRC_DIR / "providers" / name / "adapter.py"
        text = adapter.read_text(encoding="utf-8")
        assert "fastapi" not in text, f"{name} adapter must not depend on FastAPI"
        # SDK 必须延迟导入：模块顶层不得出现 SDK import（允许函数体内缩进的延迟导入）
        assert not re.search(rf"^import {name}\b", text, re.MULTILINE), (
            f"{name} adapter must lazily import its SDK"
        )
        assert not re.search(rf"^from {name}\b", text, re.MULTILINE), (
            f"{name} adapter must lazily import its SDK"
        )


def test_api_layer_does_not_import_provider_sdk_modules() -> None:
    api_files = _package_files("api")
    api_files += sorted((SRC_DIR / "api" / "routes").glob("*.py"))
    api_files += sorted((SRC_DIR / "api" / "schemas").glob("*.py"))
    for path in api_files:
        text = path.read_text(encoding="utf-8")
        for name in PROVIDER_NAMES:
            assert f"import {name}" not in text, f"{path.name} must not import provider SDK"
            assert f"from tapehub.providers.{name}" not in text
