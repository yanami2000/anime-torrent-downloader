"""无头 BT 引擎：magnet/http/文件、仅视频过滤、.part 暂存、断点续传、做种控制。

设计要点：
- 单线程事件循环（run/stop 显式驱动），无后台线程、无锁；
- 直接下载到最终分类目录；未完成文件带 temp_suffix（如 .part），完成后原地改名；
- 用 libtorrent 原生队列控制并发（active_downloads / active_seeds）；
- 死种超时清理，保证大批次能收尾。
"""

from __future__ import annotations

import os
import pickle
import time
from pathlib import Path
from typing import Any
from urllib.request import urlopen

import libtorrent as lt

from . import rss
from .config import DEFAULT_CONFIG
from .logger import get_logger
from .state import DoneStore

_log = get_logger(__name__)

VIDEO_EXTENSIONS: frozenset[str] = frozenset({
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".m4v", ".mpg", ".mpeg", ".ts", ".m2ts", ".rmvb", ".rm",
    ".vob", ".ogv", ".3gp",
})

TRACKER_LIST: list[str] = [
    "udp://tracker.publictracker.xyz:6969/announce",
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker2.dler.org:80/announce",
    "udp://tracker.wildkat.net:6969/announce",
    "udp://tracker.tryhackx.org:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://tracker.qu.ax:6969/announce",
    "udp://tracker.opentorrent.top:6969/announce",
    "udp://tracker.bittor.pw:1337/announce",
    "udp://tracker.auctor.tv:6969/announce",
    "udp://tracker-udp.gbitt.info:80/announce",
    "udp://tr4ck3r.duckdns.org:6969/announce",
    "udp://torrentclub.online:54123/announce",
    "udp://t.overflow.biz:6969/announce",
    "udp://seedpeer.net:6969/announce",
    "udp://retracker01-msk-virt.corbina.net:80/announce",
    "udp://ns575949.ip-51-222-82.net:6969/announce",
]

_STATE_LABELS: dict[int, str] = {
    0: "queued", 1: "checking", 2: "dl-meta", 3: "downloading",
    4: "finished", 5: "seeding", 6: "allocating", 7: "checking",
}

_PROGRESS_LOG_INTERVAL = 60.0
_RESUME_FLUSH_INTERVAL = 30.0


class TorrentError(Exception):
    """所有可预期引擎错误的基类。"""


class DuplicateError(TorrentError):
    """种子已在本会话中。"""


class AlreadyDoneError(TorrentError):
    """该种子此前已完成下载。"""


class NoVideoError(TorrentError):
    """video_only 开启且种子不含视频。"""


class TorrentEngine:
    """单会话、单线程的 BT 引擎。"""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._cfg = {**DEFAULT_CONFIG, **(config or {})}
        self._session: lt.session | None = None
        self._torrents: dict[str, lt.torrent_handle] = {}
        self._paths: dict[str, str] = {}        # ih -> save_path（最终目录）
        self._resume: dict[str, bytes] = {}
        self._seed_targets: set[str] = set()
        self._state = DoneStore(self._cfg["state_file"])
        self._probed_dirs: set[str] = set()
        self._timers: dict[str, float] = {}
        self._resume_dirty = False

    # ------------------------------------------------------------------ #
    #  生命周期
    # ------------------------------------------------------------------ #

    def _ensure(self) -> None:
        if self._session is not None:
            return
        self._probe_dir(self._cfg["save_path"])

        cfg = self._cfg
        settings: dict[str, Any] = {
            "listen_interfaces": "0.0.0.0:0,[::]:0",
            "download_rate_limit": cfg.get("download_rate_limit", 0),
            "upload_rate_limit": cfg.get("upload_rate_limit", 0),
            "connections_limit": cfg.get("max_connections", 500),
            "active_downloads": cfg.get("max_active_downloads", 3),
            "active_seeds": cfg.get("max_active_seeds", 2),
            "enable_dht": True,
            "enable_lsd": True,
            "enable_natpmp": True,
            "enable_upnp": True,
            "announce_to_all_trackers": True,
            "announce_to_all_tiers": True,
            "connection_speed": 100,
            "out_enc_policy": 1,
            "in_enc_policy": 1,
            "allowed_enc_level": 2,
        }
        cat = lt.alert.category_t
        settings["alert_mask"] = (
            cat.error_notification | cat.status_notification
            | cat.storage_notification | cat.progress_notification
        )
        self._session = lt.session(settings)

        for router, port in [
            ("dht.libtorrent.org", 25401),
            ("router.bittorrent.com", 6881),
            ("dht.transmissionbt.com", 6881),
            ("dht.aelitis.com", 6881),
        ]:
            try:
                self._session.add_dht_router(router, port)
            except Exception:
                pass

        _log.info("libtorrent session started (port: %s, v%s).",
                  self._session.listen_port(), lt.version)

        self._load_resume()
        self._restore()
        _log.info("Engine ready: %d torrent(s) restored.", len(self._torrents))

    @property
    def active_count(self) -> int:
        self._ensure()
        return len(self._torrents)

    def has_pending(self) -> bool:
        """是否存在未完成任务（供 --once 模式判断是否需要启动 session）。"""
        return Path(self._cfg["resume_file"]).exists()

    def run(self, max_seconds: int | None = None, display: Any = None) -> None:
        """阻塞直到所有种子完成/移除，或达到 max_seconds。"""
        self._ensure()
        if max_seconds is None:
            max_seconds = int(self._cfg.get("max_run_seconds", 0))
        deadline = time.time() + max_seconds if max_seconds else None
        last_display = 0.0
        try:
            while self._torrents:
                if deadline and time.time() >= deadline:
                    _log.info("Run limit reached with %d torrent(s) active.",
                              len(self._torrents))
                    break
                self.tick()
                self._flush_resume_if_dirty()
                self._log_progress(
                    float(self._cfg.get("progress_log_interval", 15)))
                if display is not None:
                    now = time.time()
                    if now - last_display >= 1.0:
                        display.render(self.status_snapshot())
                        last_display = now
                time.sleep(0.5)
        finally:
            if display is not None:
                display.clear()

    def status_snapshot(self) -> list[dict]:
        """每个任务的紧凑状态（供控制台/日志展示）。"""
        rows: list[dict] = []
        for info_hash, handle in list(self._torrents.items()):
            try:
                s = handle.status()
                name = self._safe_str(s.name, info_hash[:12])
                rows.append({
                    "episode": rss.episode_key(name) or "?",
                    "name": name,
                    "state": int(s.state),
                    "progress": float(s.progress),
                    "download_rate": s.download_rate,
                    "upload_rate": s.upload_rate,
                    "num_seeds": s.num_seeds,
                    "num_peers": s.num_peers,
                })
            except Exception:
                continue
        return rows

    def tick(self) -> None:
        for alert in self._session.pop_alerts():
            try:
                self._handle_alert(alert)
            except Exception as exc:
                _log.debug("Alert error (%s): %s", type(alert).__name__, exc)
        self._check_seed_limits()
        self._check_stalled()

    def stop(self) -> None:
        """保存 resume 与状态后关闭。可重复调用。"""
        if self._session is None:
            self._state.save()
            return

        handles = list(self._torrents.values())
        for handle in handles:
            self._request_resume(handle)

        pending = len(handles)
        saved: set[str] = set()
        deadline = time.time() + 5.0
        while time.time() < deadline and len(saved) < pending:
            alerts = self._session.pop_alerts()
            if alerts:
                for alert in alerts:
                    if isinstance(alert, lt.save_resume_data_alert):
                        self._on_resume_saved(alert)
                        saved.add(self._handle_ih(alert.handle))
            else:
                time.sleep(0.1)

        self._flush_resume()
        self._state.save()

        for handle in handles:
            try:
                self._session.remove_torrent(handle)
            except Exception:
                pass
        self._torrents.clear()
        self._seed_targets.clear()
        _log.info("Engine stopped. Resume saved %d/%d.", len(saved), pending)

    # ------------------------------------------------------------------ #
    #  添加种子
    # ------------------------------------------------------------------ #

    def add_torrent(self, source: str, save_path: str | None = None) -> str:
        """添加 magnet / http(s) 链接 / 本地 .torrent 路径。"""
        self._ensure()
        source = source.strip()
        if source.startswith("magnet:"):
            params = lt.parse_magnet_uri(source)
            info_hash = self._ih_str(params.info_hash)
            return self._add(self._apply_resume(params, info_hash),
                             info_hash, info_hash, source, save_path)
        return self.add_torrent_data(source, self._read_torrent(source),
                                     save_path)

    def add_torrent_data(self, source: str, data: bytes,
                         save_path: str | None = None) -> str:
        """直接用已抓取的 .torrent 字节添加（配合并行预取）。"""
        self._ensure()
        ti = self._make_torrent_info(data)
        info_hash = self._ih_str(ti.info_hash())
        name = self._safe_str(ti.name(), info_hash)
        if self._cfg.get("video_only", True) and not self._has_videos(ti):
            raise NoVideoError(f"'{name}' contains no video files.")
        params = lt.add_torrent_params()
        params.ti = ti
        params = self._apply_resume(params, info_hash)
        return self._add(params, info_hash, name, source, save_path)

    def _add(self, params: Any, info_hash: str, name: str, source: str,
             save_path: str | None) -> str:
        if self._state.is_done(info_hash):
            raise AlreadyDoneError(f"'{name}' already downloaded.")
        if info_hash in self._torrents:
            raise DuplicateError(f"'{name}' already in session.")

        target = save_path or self._cfg["save_path"]
        if save_path is not None:
            self._probe_dir(target)
        if not params.save_path:
            params.save_path = target
        self._apply_ti_suffix(params.ti)

        handle = self._session.add_torrent(params)
        self._attach_trackers(handle)
        self._torrents[info_hash] = handle
        self._paths[info_hash] = params.save_path or target
        self._request_resume(handle)
        _log.info("Added '%s' [%s] -> %s", name, info_hash, params.save_path)
        return info_hash

    # ------------------------------------------------------------------ #
    #  RSS 状态透传（去重）
    # ------------------------------------------------------------------ #

    def is_seen(self, key: str) -> bool:
        return self._state.is_seen(key)

    def mark_seen(self, key: str) -> None:
        self._state.mark_seen(key)

    # ------------------------------------------------------------------ #
    #  事件处理
    # ------------------------------------------------------------------ #

    def _handle_alert(self, alert: Any) -> None:
        if isinstance(alert, lt.metadata_received_alert):
            self._on_metadata(alert)
        elif isinstance(alert, lt.torrent_finished_alert):
            self._on_finished(alert.handle)
        elif isinstance(alert, lt.save_resume_data_alert):
            self._on_resume_saved(alert)
        elif isinstance(alert, lt.torrent_error_alert):
            _log.error("Torrent error: %s", self._safe_alert_msg(alert))
        elif isinstance(alert, lt.add_torrent_alert):
            err = getattr(alert, "error", None)
            if err is not None:
                try:
                    if err.value() != 0:
                        _log.error("Add torrent error: %s",
                                   self._safe_str(err.message()))
                except Exception:
                    pass
        elif isinstance(alert, lt.torrent_removed_alert):
            ih = self._ih_str(alert.info_hash)
            self._torrents.pop(ih, None)
            self._paths.pop(ih, None)
        elif isinstance(alert, lt.fastresume_rejected_alert):
            _log.warning("Resume rejected: %s", self._safe_alert_msg(alert))

    def _on_metadata(self, alert: Any) -> None:
        handle = alert.handle
        info_hash = self._handle_ih(handle)
        ti = handle.torrent_file()
        if ti is None:
            return
        name = self._safe_str(ti.name(), info_hash)
        if self._cfg.get("video_only", True) and not self._has_videos(ti):
            _log.info("Removing '%s' - no video files.", name)
            try:
                self._session.remove_torrent(handle)
            except Exception:
                pass
            self._torrents.pop(info_hash, None)
            self._paths.pop(info_hash, None)
            self._resume.pop(info_hash, None)
            return
        # 磁力种子：在下载数据前应用 .part 后缀（空文件 recheck 零代价）
        suffix = self._cfg.get("temp_suffix", "")
        if suffix:
            files = ti.files()
            for i in range(files.num_files()):
                rel = files.file_path(i)
                if not rel.endswith(suffix):
                    try:
                        handle.rename_file(i, rel + suffix)
                    except Exception:
                        pass
        _log.info("Metadata received: '%s' [%s].", name, info_hash)

    def _on_finished(self, handle: lt.torrent_handle) -> None:
        info_hash = self._handle_ih(handle)
        self._seed_targets.add(info_hash)
        self._request_resume(handle)
        try:
            name = self._safe_str(handle.status().name, info_hash)
        except Exception:
            name = info_hash
        _log.info("Download completed: '%s' [%s].", name, info_hash)

    def _check_seed_limits(self) -> None:
        now = time.time()
        if not self._due("seed", 3.0):
            return

        target = self._cfg.get("seed_ratio", 0.0)
        time_limit = self._cfg.get("seed_time_limit", 0)

        for info_hash in list(self._seed_targets):
            handle = self._torrents.get(info_hash)
            if handle is None:
                self._seed_targets.discard(info_hash)
                continue
            try:
                status = handle.status()
                dl = status.all_time_download
                if dl <= 0:
                    continue
                ratio = status.all_time_upload / dl
                reached = ratio >= target
                if time_limit and now - status.added_time > time_limit:
                    reached = True
                if not reached:
                    continue
                try:
                    name = self._safe_str(status.name, info_hash)
                except Exception:
                    name = info_hash
                _log.info("Seed target reached (%.2f >= %.2f): '%s'. Done.",
                          ratio, target, name)
                self._finalize(info_hash, handle, name)
            except Exception as exc:
                _log.debug("Seed check error for %s: %s", info_hash, exc)

    def _finalize(self, info_hash: str, handle: lt.torrent_handle,
                  name: str) -> None:
        """移除种子，原地去掉 .part 后缀，标记完成。"""
        save_dir = self._paths.get(info_hash, self._cfg["save_path"])
        rels: list[str] = []
        try:
            ti = handle.torrent_file()
            files = ti.files()
            rels = [files.file_path(i) for i in range(files.num_files())]
        except Exception:
            pass

        try:
            self._session.remove_torrent(handle)
        except Exception:
            pass
        self._torrents.pop(info_hash, None)
        self._paths.pop(info_hash, None)
        self._seed_targets.discard(info_hash)

        suffix = self._cfg.get("temp_suffix", "")
        renamed_ok = True
        if suffix and rels:
            for rel in rels:
                if not rel.endswith(suffix):
                    continue
                src = Path(save_dir) / rel
                dst = Path(save_dir) / rel[:-len(suffix)]
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(src, dst)
                except Exception as exc:
                    renamed_ok = False
                    _log.warning("Rename failed for %s: %s", rel, exc)

        if renamed_ok:
            self._resume.pop(info_hash, None)
            self._state.mark_done(info_hash, name)
        else:
            # 保留 resume，下次运行 restore 后重试改名
            _log.warning("File rename incomplete, will retry next run: %s", name)

    def _check_stalled(self) -> None:
        """死种清理：0 进度且超时，移出会话（保留 resume 供下轮重试）。"""
        now = time.time()
        if not self._due("stall", 60.0):
            return
        limit = self._cfg.get("stall_skip_hours", 0)
        if not limit:
            return
        for info_hash, handle in list(self._torrents.items()):
            if info_hash in self._seed_targets:
                continue
            try:
                status = handle.status()
                if status.progress > 0:
                    continue
                if now - status.added_time <= limit * 3600:
                    continue
                try:
                    name = self._safe_str(status.name, info_hash)
                except Exception:
                    name = info_hash
                _log.warning("Stalled >%dh (0%%), skipping: %s", limit, name)
                try:
                    self._session.remove_torrent(handle)
                except Exception:
                    pass
                self._torrents.pop(info_hash, None)
                self._paths.pop(info_hash, None)
            except Exception as exc:
                _log.debug("Stall check error for %s: %s", info_hash, exc)

    def _log_progress(self, interval: float = _PROGRESS_LOG_INTERVAL) -> None:
        now = time.time()
        if not self._due("progress", interval):
            return
        total_dl = total_ul = 0
        for info_hash, handle in list(self._torrents.items()):
            try:
                s = handle.status()
                name = self._safe_str(s.name, info_hash[:12])
                ep = rss.episode_key(name) or "?"
                _log.info(
                    "  EP%s %s %5.1f%%  %s/s\u2193 %s/s\u2191  S:%d P:%d  %s",
                    ep, _STATE_LABELS.get(int(s.state), "?"),
                    float(s.progress) * 100,
                    fmt_speed(s.download_rate), fmt_speed(s.upload_rate),
                    s.num_seeds, s.num_peers, name[:22])
                total_dl += s.download_rate
                total_ul += s.upload_rate
            except Exception:
                pass
        _log.info("  aggregate: %s/s\u2193 %s/s\u2191 (%d active)",
                  fmt_speed(total_dl), fmt_speed(total_ul),
                  len(self._torrents))

    # ------------------------------------------------------------------ #
    #  Resume
    # ------------------------------------------------------------------ #

    def _request_resume(self, handle: lt.torrent_handle) -> None:
        handle.save_resume_data(lt.torrent_handle.save_info_dict)

    def _on_resume_saved(self, alert: Any) -> None:
        info_hash = self._handle_ih(alert.handle)
        try:
            self._resume[info_hash] = lt.bencode(
                lt.write_resume_data(alert.params))
            self._resume_dirty = True
        except Exception as exc:
            _log.warning("Failed to save resume data for %s: %s",
                         info_hash, exc)

    def _flush_resume_if_dirty(self) -> None:
        if not self._resume_dirty or not self._due("flush",
                                                    _RESUME_FLUSH_INTERVAL):
            return
        self._flush_resume()

    def _flush_resume(self) -> None:
        try:
            path = Path(self._cfg["resume_file"])
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "wb") as fh:
                pickle.dump(self._resume, fh)
            tmp.replace(path)
            self._resume_dirty = False
        except Exception as exc:
            _log.error("Failed to flush resume: %s", exc)

    def _load_resume(self) -> None:
        try:
            path = Path(self._cfg["resume_file"])
            if path.exists():
                with open(path, "rb") as fh:
                    self._resume = pickle.load(fh)
                self._resume = {
                    ih: data for ih, data in self._resume.items()
                    if not self._state.is_done(ih)
                }
                _log.info("Loaded %d resume entries.", len(self._resume))
        except Exception as exc:
            _log.warning("Failed to load resume: %s", exc)
            self._resume = {}

    def _restore(self) -> None:
        """把 resume 里的未完成任务重新加入会话，实现启动即续传。"""
        suffix = self._cfg.get("temp_suffix", "")
        for info_hash, encoded in list(self._resume.items()):
            try:
                params = lt.read_resume_data(encoded)
                if not params.save_path:
                    params.save_path = self._cfg["save_path"]
                if params.ti is not None and suffix:
                    self._apply_ti_suffix(params.ti)   # 幂等兜底
                handle = self._session.add_torrent(params)
                self._attach_trackers(handle)
                self._torrents[info_hash] = handle
                self._paths[info_hash] = params.save_path or self._cfg["save_path"]
            except Exception as exc:
                _log.warning("Failed to restore %s: %s", info_hash, exc)

    # ------------------------------------------------------------------ #
    #  辅助
    # ------------------------------------------------------------------ #

    def _due(self, key: str, interval: float) -> bool:
        """节流：距上次同一 key 执行不足 interval 秒则返回 False。"""
        now = time.time()
        if now - self._timers.get(key, 0.0) < interval:
            return False
        self._timers[key] = now
        return True

    def _apply_resume(self, params: Any, info_hash: str) -> Any:
        if info_hash not in self._resume:
            return params
        try:
            return lt.read_resume_data(self._resume[info_hash])
        except Exception as exc:
            _log.warning("Bad resume data for %s: %s", info_hash, exc)
            return params

    def _apply_ti_suffix(self, ti: Any) -> None:
        """给 torrent_info 的所有文件加 temp_suffix（幂等，不改 info_hash）。"""
        suffix = self._cfg.get("temp_suffix", "")
        if not suffix or ti is None:
            return
        files = ti.files()
        for i in range(files.num_files()):
            rel = files.file_path(i)
            if not rel.endswith(suffix):
                ti.rename_file(i, rel + suffix)

    def _attach_trackers(self, handle: lt.torrent_handle) -> None:
        for url in TRACKER_LIST:
            try:
                handle.add_tracker({"url": url, "tier": 0})
            except Exception:
                pass

    def _probe_dir(self, path: str) -> None:
        """下载前校验目标目录可写（错误前置）。"""
        p = Path(path)
        p.mkdir(parents=True, exist_ok=True)
        key = str(p)
        if key in self._probed_dirs:
            return
        probe = p / ".write_test"
        try:
            probe.write_bytes(b"")
            probe.unlink()
        except Exception as exc:
            raise ValueError(f"目标目录不可写: {path} ({exc})")
        self._probed_dirs.add(key)

    def _read_torrent(self, source: str) -> bytes:
        if source.startswith(("http://", "https://")):
            with urlopen(source, timeout=30) as resp:
                return resp.read()
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"Torrent file not found: {source}")
        return path.read_bytes()

    def _make_torrent_info(self, data: bytes) -> lt.torrent_info:
        return lt.torrent_info(lt.bdecode(data))

    @staticmethod
    def _ih_str(ih: Any) -> str:
        if hasattr(ih, "v1"):
            v1 = ih.v1()
            return str(v1) if not v1.is_all_zeros() else str(ih.v2())
        return str(ih)

    def _handle_ih(self, handle: lt.torrent_handle) -> str:
        try:
            return self._ih_str(handle.info_hash())
        except Exception:
            return ""

    @staticmethod
    def _safe_str(val: Any, default: str = "") -> str:
        if val is None:
            return default
        if isinstance(val, bytes):
            for enc in ("utf-8", "gbk", "gb2312", "latin-1"):
                try:
                    return val.decode(enc)
                except (UnicodeDecodeError, ValueError):
                    continue
            return val.decode("utf-8", errors="replace")
        try:
            return str(val)
        except Exception:
            return default

    def _safe_alert_msg(self, alert: Any) -> str:
        try:
            return self._safe_str(alert.message())
        except Exception:
            return type(alert).__name__

    @staticmethod
    def _is_video_file(filename: str) -> bool:
        return Path(filename).suffix.lower() in VIDEO_EXTENSIONS

    def _has_videos(self, ti: Any) -> bool:
        try:
            files = ti.files()
        except Exception:
            return False
        for i in range(files.num_files()):
            try:
                if self._is_video_file(self._safe_str(files.file_path(i))):
                    return True
            except Exception:
                continue
        return False


def fmt_speed(bps: float) -> str:
    if bps >= 1_048_576:
        return f"{bps / 1_048_576:.1f}MB"
    if bps >= 1_024:
        return f"{bps / 1_024:.1f}KB"
    return f"{bps:.0f}B"
