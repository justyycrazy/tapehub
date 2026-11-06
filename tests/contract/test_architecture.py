"""架构边界测试：依赖方向与职责分离由源码扫描静态保证。

- Application 层不得出现具体 Provider 名称的条件判断；
- Domain 层不得依赖 FastAPI、PyYAML 或 Provider 适配器；
- Provider 适配器不得依赖 FastAPI 请求上下文；
- 上游 SDK 函数名不得出现在 Application/API 层。
"""

import re
from pathlib import Path
from typing import Any

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


def _beyond_monthly(interval: Any) -> bool:
    """周期是否超出能力目录的月线上界（P1M）。

    判定规则（避免项目禁止的「月↔固定天数」日历换算）：
    - **组合周期一律拒绝**：七个分量中恰一个非零——交易所标准周期均为
      单分量（PT1M…PT1H、P1D、P1W、P1M），P1M2W / P1W2D / PT1H30M 之类
      组合不进能力目录（逐案判断组合长短只会引出跨单位换算）；
    - 年分量 > 0 拒绝（P1Y 等）；
    - 月分量 > 1 拒绝（P3M 季线映射、P2M 等）；
    - 周/日/时/分/秒分量换算为天属恒等算术（周=7 天、时=1/24 天），
      超过 31 天（公历最长月）即不可能短于等于任何一个月：P13W（季线的
      周表达）、P5W、P32D 等由此拒绝。
    """
    components = (
        interval.years,
        interval.months,
        interval.weeks,
        interval.days,
        interval.hours,
        interval.minutes,
        interval.seconds,
    )
    if sum(1 for value in components if value) != 1:
        return True
    if interval.years:
        return True
    if interval.months:
        return interval.months > 1
    days = (
        interval.weeks * 7
        + interval.days
        + interval.hours / 24
        + interval.minutes / 1440
        + interval.seconds / 86400
    )
    return days > 31


def test_manifests_do_not_declare_intervals_beyond_monthly() -> None:
    """manifest 周期声明上界为月线（P1M）：年线、季线（含 P3M/P13W 映射）
    与一切组合周期均不得进入能力目录（判定规则见 ``_beyond_monthly``）。

    上游存在年线/季线（如 tickflow/alphafeed 的 1Q/1Y）属于上游能力面，
    不映射进标准周期目录。
    """
    from tapehub.providers.manifest import load_builtin_manifest
    from tapehub.providers.registry import BUILTIN_PROVIDERS

    for name in BUILTIN_PROVIDERS:
        manifest = load_builtin_manifest(name)
        for capability in manifest.capabilities:
            for entry in capability.intervals:
                interval = entry.code
                assert not _beyond_monthly(interval), (
                    f"{name} declares interval {interval.code!r} beyond the "
                    "monthly cap (P1M); only standard single-component periods "
                    "up to P1M belong in the capability catalog"
                )
