"""探针日志配置测试（离线：capsys / tmp_path 断言 handler 行为）。"""

import datetime as dt
import logging
from pathlib import Path

import pytest
from probes import logging_setup
from probes.logging_setup import setup_logging

from tapehub.domain.models import CN_TIMEZONE


@pytest.fixture(autouse=True)
def _reset_logging():
    """每条测试后移除本模块添加的 handler，不污染其它测试的 root logger。"""
    yield
    root = logging.getLogger()
    for name in ("_console_handler", "_file_handler"):
        handler = getattr(logging_setup, name)
        if handler is not None:
            root.removeHandler(handler)
            setattr(logging_setup, name, None)
    root.setLevel(logging.WARNING)
    logging.getLogger("probes").setLevel(logging.NOTSET)


def test_console_default_info_hides_debug(capsys: pytest.CaptureFixture[str]) -> None:
    """默认 INFO：case 进度与子步骤行可见，DEBUG 细节被 handler 过滤。"""
    setup_logging()
    lg = logging.getLogger("probes.test")
    lg.debug("debug-detail")
    lg.info("info-line")
    out = capsys.readouterr().out
    assert "info-line" in out
    assert "debug-detail" not in out


def test_verbose_console_shows_debug(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging(verbose=True)
    logging.getLogger("probes.test").debug("debug-detail")
    assert "debug-detail" in capsys.readouterr().out


def test_log_file_always_debug(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--log-file 恒 DEBUG 全量：非 verbose 运行也留下完整时间线。"""
    log_file = tmp_path / "run.log"
    setup_logging(log_file=log_file)
    lg = logging.getLogger("probes.test")
    lg.debug("file-only-debug")
    lg.info("info-line")
    content = log_file.read_text(encoding="utf-8")
    assert "file-only-debug" in content
    assert "info-line" in content
    assert "probes.test" in content  # 文件行带 logger 名
    # 控制台仍按默认 INFO 过滤 DEBUG
    assert "file-only-debug" not in capsys.readouterr().out


def test_log_file_timestamps_beijing(tmp_path: Path) -> None:
    """文件日志时间戳统一北京时间：与当前北京墙钟偏差在分钟级内。"""
    log_file = tmp_path / "run.log"
    setup_logging(log_file=log_file)
    logging.getLogger("probes.test").info("ts-line")
    line = log_file.read_text(encoding="utf-8").strip()
    stamp_text = line.split(" INFO")[0]
    stamp = dt.datetime.strptime(stamp_text, "%Y-%m-%d %H:%M:%S,%f").replace(
        tzinfo=CN_TIMEZONE
    )
    assert abs((dt.datetime.now(CN_TIMEZONE) - stamp).total_seconds()) < 120


def test_setup_logging_is_idempotent(capsys: pytest.CaptureFixture[str]) -> None:
    """重复调用（如测试反复 setup）不叠加 handler，每条记录只输出一次。"""
    setup_logging()
    setup_logging()
    logging.getLogger("probes.test").info("once-line")
    assert capsys.readouterr().out.count("once-line") == 1


def test_setup_replaces_file_handler(tmp_path: Path) -> None:
    """再次 setup 不带 log_file 时移除旧文件 handler（先删后建，可换路径）。"""
    first = tmp_path / "a.log"
    setup_logging(log_file=first)
    setup_logging()
    logging.getLogger("probes.test").info("after-reset")
    assert "after-reset" not in first.read_text(encoding="utf-8")
