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
