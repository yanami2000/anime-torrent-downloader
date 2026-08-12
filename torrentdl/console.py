"""控制台进度：单行刷新（\\r 覆盖），不依赖光标移动/行数，任何终端都不会累积。"""

from __future__ import annotations

import shutil
import sys
import time

from .engine import fmt_speed


def render_line(rows: list[dict], now: str, width: int = 8) -> str:
    """一行总览：总速度 + 各任务迷你进度 + 排队数（纯 ASCII，集数排序）。"""
    total_dl = sum(r.get("download_rate", 0) for r in rows)
    total_ul = sum(r.get("upload_rate", 0) for r in rows)
    parts = [f"[{now}] \u2193{fmt_speed(total_dl)}/s \u2191{fmt_speed(total_ul)}/s"]

    active = [r for r in rows if int(r.get("state", 0)) in (1, 2, 3, 4, 5)]
    shown = sorted(active, key=lambda r: r.get("episode") or "zzz")
    overflow = len(shown) > 4
    for r in shown[:4]:
        ep = r.get("episode") or "?"
        pct = int(float(r.get("progress", 0)) * 100)
        if overflow:
            parts.append(f"EP{ep} {pct}%")
        else:
            filled = min(pct * width // 100, width)
            parts.append(f"EP{ep} {'#' * filled + '-' * (width - filled)} {pct}%")
    if overflow:
        parts.append(f"+{len(shown) - 4} more")

    queued = sum(1 for r in rows if int(r.get("state", 0)) == 0)
    if queued:
        parts.append(f"{queued} queued")
    return " | ".join(parts)


class ProgressDisplay:
    """用 \\r 覆盖当前行，任何终端（含管道外的交互终端）都稳定。"""

    def __init__(self) -> None:
        self._tty = sys.stdout.isatty()

    def render(self, rows: list[dict]) -> None:
        if not self._tty:
            return
        line = render_line(rows, time.strftime("%H:%M:%S"))
        cols = shutil.get_terminal_size((100, 20)).columns
        line = line[:cols - 1]
        sys.stdout.write("\r" + line.ljust(cols - 1))
        sys.stdout.flush()

    def clear(self) -> None:
        if self._tty:
            cols = shutil.get_terminal_size((100, 20)).columns
            sys.stdout.write("\r" + " " * (cols - 1) + "\r")
            sys.stdout.flush()
