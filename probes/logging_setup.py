"""探针运行日志的集中配置。

探针 CLI 专用（``probes.cli`` 在 ``run`` 子命令入口调用一次）：

- 控制台 handler 挂 stdout，``%(message)s`` 裸格式——进度/子步骤行自描述，
  保留「stdout 重定向到文件仍实时可见」的既有工作流（StreamHandler 每次
  emit 后自动 flush）；
- ``--log-file`` 追加文件 handler，**级别恒为 DEBUG**（无视控制台开关），
  供事后取证；覆盖已有文件时为追加而非截断；
- 时间戳统一北京时间（与项目「服务级时间戳一律北京时间」口径一致）；
- ``probes`` 命名空间恒设 DEBUG（子步骤细节全部发出，交给各 handler 过滤），
  root 级别跟随控制台开关，使 ``tapehub.*``（registry 告警等）可见、
  第三方 SDK 的 DEBUG 噪声默认不穿透。
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from tapehub.domain.models import CN_TIMEZONE

CONSOLE_FORMAT = "%(message)s"
FILE_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"

# 本次 setup 添加的 handler；重复调用（如测试反复 setup）先移除，避免叠加
_console_handler: logging.Handler | None = None
_file_handler: logging.Handler | None = None


def _cn_time_converter(seconds: float) -> time.struct_time:
    """logging.Formatter 时间戳转北京时间 wall clock（struct_time 不携带 tz）。"""
    return datetime.fromtimestamp(seconds, CN_TIMEZONE).timetuple()


def setup_logging(*, verbose: bool = False, log_file: Path | None = None) -> None:
    """配置探针运行日志：控制台（INFO/DEBUG）+ 可选全量 DEBUG 文件。

    ``verbose=False`` 时控制台为 INFO（case 生命周期 + 每次上游调用的
    子步骤行）；``--log-file`` 的文件 handler 恒为 DEBUG，保证非 verbose
    运行也能留下完整时间线。
    """
    global _console_handler, _file_handler
    console_level = logging.DEBUG if verbose else logging.INFO

    root = logging.getLogger()
    if _console_handler is not None:
        root.removeHandler(_console_handler)
        _console_handler = None
    if _file_handler is not None:
        root.removeHandler(_file_handler)
        _file_handler = None

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(CONSOLE_FORMAT))
    console.setLevel(console_level)
    root.addHandler(console)
    _console_handler = console

    if log_file is not None:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(FILE_FORMAT))
        file_handler.setLevel(logging.DEBUG)
        file_handler.formatter.converter = _cn_time_converter
        root.addHandler(file_handler)
        _file_handler = file_handler

    # 子步骤 DEBUG 行要从 logger 层放行（effective level 沿袭 root），
    # probes 命名空间显式设 DEBUG；root 跟随控制台级别，第三方库 DEBUG
    # 默认被 logger 层过滤
    logging.getLogger("probes").setLevel(logging.DEBUG)
    root.setLevel(console_level)
