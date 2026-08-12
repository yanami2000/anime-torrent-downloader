"""文件日志（可选控制台到 stderr）。无头管道默认只写文件。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_ROOT = "torrentdl"
_CONFIGURED = False
_CONSOLE_HANDLER: logging.Handler | None = None


def get_logger(name: str) -> logging.Logger:
    if not name.startswith(_ROOT):
        name = f"{_ROOT}.{name}"
    return logging.getLogger(name)


def set_console_level(level: int) -> None:
    """临时调整控制台输出级别（用于进度显示期间静音 INFO 日志）。"""
    if _CONSOLE_HANDLER is not None:
        _CONSOLE_HANDLER.setLevel(level)


def setup_logging(log_file: str, console: bool = True,
                  level: int = logging.INFO) -> None:
    """幂等地挂接文件 handler（以及可选的 stderr handler）。"""
    global _CONFIGURED
    if _CONFIGURED:
        return

    root = logging.getLogger(_ROOT)
    root.setLevel(level)
    root.propagate = False
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    if console:
        global _CONSOLE_HANDLER
        _CONSOLE_HANDLER = logging.StreamHandler(sys.stderr)
        _CONSOLE_HANDLER.setFormatter(fmt)
        root.addHandler(_CONSOLE_HANDLER)

    _CONFIGURED = True
