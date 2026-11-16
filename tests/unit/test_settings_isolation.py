"""测试环境解耦机制测试：make_settings 不读 .env，autouse 隔离 TAPEHUB_* 环境变量。"""

import os
from pathlib import Path

import pytest

from tapehub.settings import Settings
from tests.conftest import make_settings


def test_make_settings_ignores_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """临时 .env 只影响服务路径的 Settings()，不影响测试路径的 make_settings()。"""
    (tmp_path / ".env").write_text("TAPEHUB_MAX_LIMIT=777\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert Settings().max_limit == 777  # 服务路径：读 .env（运行时语义不变）
    assert make_settings().max_limit == 5000  # 测试路径：忽略 .env，回到默认值


def test_autouse_fixture_clears_tapehub_env() -> None:
    """测试体开始时进程环境中不应存在任何 TAPEHUB_* 变量。

    在干净外壳下本断言为真；若外壳配置了真实凭据（TAPEHUB_*），
    autouse fixture 已在 setup 阶段将其清空，本断言同样是解耦的守卫。
    """
    assert not [name for name in os.environ if name.startswith("TAPEHUB_")]


def test_deployment_mode_defaults_to_single() -> None:
    """守门字段缺省为 single（单进程、单用户最小运行模式）。"""
    assert make_settings().deployment_mode == "single"


def test_deployment_mode_rejects_unknown_values() -> None:
    """非 single 值启动即失败：守门字段不承诺未实现的能力。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        make_settings(deployment_mode="cluster")
    with pytest.raises(ValidationError):
        make_settings(deployment_mode="")


def test_tqsdk_conn_mode_default_and_validation() -> None:
    """tqsdk 连接模式缺省 per_request；非法值启动失败。"""
    from pydantic import ValidationError

    assert make_settings().tqsdk_conn_mode == "shared"
    assert make_settings(tqsdk_conn_mode="per_request").tqsdk_conn_mode == "per_request"
    with pytest.raises(ValidationError):
        make_settings(tqsdk_conn_mode="pool")


def test_routing_config_env_var_matches_docs(monkeypatch: pytest.MonkeyPatch) -> None:
    """文档环境变量名与 Settings 字段名一致（契约测试）。

    ``routing_config_path`` 由 pydantic-settings 推导的环境变量是
    ``TAPEHUB_ROUTING_CONFIG_PATH``；文档（README/operations/.env.example/
    provider_routing.yaml 头注释）必须与之一致，否则外部路由配置会被
    ``extra="ignore"`` 静默丢弃且无任何提示。
    """
    monkeypatch.setenv("TAPEHUB_ROUTING_CONFIG_PATH", "routing.yaml")
    assert make_settings().routing_config_path == "routing.yaml"
    monkeypatch.delenv("TAPEHUB_ROUTING_CONFIG_PATH")
    assert make_settings().routing_config_path is None


def test_api_keys_env_json_array(monkeypatch: pytest.MonkeyPatch) -> None:
    """TAPEHUB_API_KEYS 为 JSON 数组（认证节契约）：空数组 = 不启用。"""
    monkeypatch.setenv("TAPEHUB_API_KEYS", '["k1","k2"]')
    assert make_settings().api_keys == ["k1", "k2"]
    monkeypatch.setenv("TAPEHUB_API_KEYS", "[]")
    assert make_settings().api_keys == []
    monkeypatch.delenv("TAPEHUB_API_KEYS")
    assert make_settings().api_keys == []
