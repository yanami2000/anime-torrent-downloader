"""主流程：读配置 -> 逐 feed 拉取 -> 标题分析/分类 -> 并行预取 -> 添加 -> 下载。"""

from __future__ import annotations

import argparse
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import Request, urlopen

from . import rss
from .config import load_config, load_feeds, rebase_paths
from .console import ProgressDisplay
from .engine import (AlreadyDoneError, DuplicateError, NoVideoError,
                     TorrentEngine)
from .logger import get_logger, set_console_level, setup_logging

_log = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_PREFETCH_WORKERS = 8


def _fetch_bytes(url: str, timeout: int = 20) -> bytes:
    req = Request(url, headers={"User-Agent": "torrent-dl/0.3"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def decide(feed: rss.Feed, engine: TorrentEngine, cfg: dict,
           matcher) -> list[rss.Item]:
    """标题过滤 + 去重，返回本次要下载的新条目。"""
    out: list[rss.Item] = []
    for item in feed.items:
        if engine.is_seen(item.guid or item.link):
            if cfg.get("stop_at_first_seen", True):
                break                      # feed 按时间倒序，旧条目直接停
            continue
        if matcher(item.title):
            out.append(item)
    if cfg.get("sort_by_episode"):
        out.sort(key=lambda it: rss.episode_key(it.title) or "zzz")
    return out


def _prefetch(items: list[rss.Item]) -> list[tuple[rss.Item, bytes | None]]:
    """并行预取 .torrent 字节，返回顺序与 items 一致（保持集数排序）。

    magnet 链接不预取，返回 None（交给引擎处理）。
    """
    results: list[bytes | None] = [None] * len(items)
    with ThreadPoolExecutor(max_workers=_PREFETCH_WORKERS) as pool:
        pending: dict = {}
        for idx, item in enumerate(items):
            if not item.link.startswith("magnet:"):
                pending[pool.submit(_fetch_bytes, item.link)] = idx
        for fut in as_completed(pending):
            idx = pending[fut]
            try:
                results[idx] = fut.result()
            except Exception as exc:
                _log.warning("Prefetch failed (%s): %s", items[idx].title, exc)
    return [(items[i], results[i]) for i in range(len(items))]


def _try_add(engine: TorrentEngine, item: rss.Item,
             data: bytes | None, target: str) -> bool:
    key = item.guid or item.link
    try:
        if item.link.startswith("magnet:"):
            engine.add_torrent(item.link, save_path=target)
        elif data is not None:
            engine.add_torrent_data(item.link, data, save_path=target)
        else:
            _log.warning("Prefetch failed, will retry next run: %s", item.title)
            return False
        engine.mark_seen(key)
        _log.info("Queued: %s", item.title)
        return True
    except (AlreadyDoneError, NoVideoError, DuplicateError) as exc:
        engine.mark_seen(key)
        _log.info("Skip (%s): %s", type(exc).__name__, item.title)
        return False
    except Exception as exc:
        _log.warning("Skip (error): %s - %s", item.title, exc)
        return False


def run_once(engine: TorrentEngine, cfg: dict, urls: list[str], matcher,
             display: ProgressDisplay) -> None:
    added = 0
    for url in urls:
        try:
            feed = rss.fetch(url, timeout=20)
        except Exception as exc:
            _log.warning("Feed failed (%s): %s", url, exc)
            continue
        items = decide(feed, engine, cfg, matcher)
        if not items:
            continue
        if cfg.get("organize_by_feed"):
            folder = rss.make_folder_name(feed)
            target = str(Path(cfg["save_path"]) / folder)
        else:
            target = cfg["save_path"]
        _log.info("Feed '%s': %d new item(s) -> %s",
                  feed.title or url, len(items), target)
        for item, data in _prefetch(items):
            if _try_add(engine, item, data, target):
                added += 1

    if added or engine.has_pending():
        set_console_level(logging.WARNING)   # 进度显示期间控制台只留告警
        try:
            engine.run(display=display)
        finally:
            set_console_level(logging.INFO)
    else:
        _log.info("Nothing to download.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="torrent-dl",
        description="RSS -> 自动下载（默认零参数运行）",
    )
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config.json"),
                        help="配置文件路径（一般不用指定）")
    parser.add_argument("--feeds", default=str(PROJECT_ROOT / "feeds.txt"),
                        help="RSS 链接文件路径（一般不用指定）")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    cfg = rebase_paths(cfg, PROJECT_ROOT)
    urls = load_feeds(args.feeds)
    if not urls:
        print("feeds.txt 里还没有 RSS 链接：请把 mikanani 的 RSS 地址粘贴进去后重新运行。")
        return 1

    setup_logging(cfg["log_file"])
    matcher = rss.make_matcher(
        cfg.get("rss_include", ()), cfg.get("rss_exclude", ()))
    engine = TorrentEngine(cfg)
    display = ProgressDisplay()
    try:
        if cfg.get("resident"):
            _log.info("Resident mode: polling every %d s.",
                      cfg.get("poll_interval", 1800))
            while True:
                try:
                    run_once(engine, cfg, urls, matcher, display)
                except Exception as exc:
                    _log.error("Poll failed: %s", exc)
                time.sleep(cfg.get("poll_interval", 1800))
        else:
            run_once(engine, cfg, urls, matcher, display)
    except KeyboardInterrupt:
        _log.info("Interrupted.")
    finally:
        engine.stop()
    return 0
