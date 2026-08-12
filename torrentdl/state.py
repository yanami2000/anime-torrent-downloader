"""done.json：已下载 info_hash + 已处理 RSS 条目。替代原 SQLite 历史。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .logger import get_logger

_log = get_logger(__name__)


class DoneStore:
    """原子写入的 JSON 状态：done（info_hash -> 元信息）+ seen（RSS 条目去重）。"""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._done: dict[str, dict[str, Any]] = {}
        self._seen: set[str] = set()
        self._load()

    def _load(self) -> None:
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._done = data.get("done", {}) or {}
                self._seen = set(data.get("seen", []) or [])
        except Exception as exc:
            _log.warning("Failed to load state, starting fresh: %s", exc)
            self._done, self._seen = {}, set()

    def is_done(self, info_hash: str) -> bool:
        return info_hash in self._done

    def mark_done(self, info_hash: str, name: str) -> None:
        self._done[info_hash] = {"name": name, "time": time.time()}
        self.save()

    def is_seen(self, key: str) -> bool:
        return key in self._seen

    def mark_seen(self, key: str) -> None:
        self._seen.add(key)
        self.save()

    def save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = {"done": self._done, "seen": sorted(self._seen)}
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, self._path)
        except Exception as exc:
            _log.error("Failed to save state: %s", exc)
