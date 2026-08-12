"""默认配置与配置文件读写。用户日常只需编辑 feeds.txt 和 config.json。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    # 下载
    "save_path": "./downloads",
    "temp_suffix": ".part",          # 未完成文件后缀，空字符串 = 关闭
    "video_only": True,
    "download_rate_limit": 0,        # bytes/s；0 = 不限
    "upload_rate_limit": 0,
    # 并发队列（libtorrent 原生控制）
    "max_active_downloads": 3,
    "max_active_seeds": 2,
    "max_connections": 500,
    # 做种（0 = 下载完即完成；也可配 seed_time_limit 限量做种）
    "seed_ratio": 0.0,
    "seed_time_limit": 0,            # 秒；0 = 不限
    # 组织
    "organize_by_feed": True,        # 按番剧分目录
    "sort_by_episode": True,         # 同番内按集数先下旧集
    # RSS
    "stop_at_first_seen": True,      # 遇已处理条目即停（feed 按时间倒序）
    "rss_include": [],               # 标题包含正则（可留空）
    "rss_exclude": [],               # 标题排除正则（可留空）
    # 调度
    "resident": False,               # True = 常驻轮询；False = 跑完退出
    "poll_interval": 1800,           # 秒
    "max_run_seconds": 0,            # 单次下载阶段上限；0 = 不限
    # 容错
    "stall_skip_hours": 6,           # 死种清理阈值；0 = 关闭
    "progress_log_interval": 15,     # 进度日志间隔（秒）
    # 内部文件（一般不用改）
    "state_file": "./done.json",
    "resume_file": "./resume_data.pkl",
    "log_file": "./download.log",
}

_CONFIG_KEYS = frozenset(DEFAULT_CONFIG)


def load_config(path: str | Path) -> dict[str, Any]:
    """读 config.json；不存在则生成默认模板。未知键忽略。"""
    path = Path(path)
    if not path.exists():
        path.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[提示] 已生成 {path.name}，默认设置可直接使用；需要调整再编辑它。")
    try:
        user = json.loads(path.read_text(encoding="utf-8"))
        return {**DEFAULT_CONFIG, **{k: v for k, v in user.items() if k in _CONFIG_KEYS}}
    except Exception as exc:
        print(f"[警告] 读取 {path.name} 失败（{exc}），使用默认配置。")
        return dict(DEFAULT_CONFIG)


def load_feeds(path: str | Path) -> list[str]:
    """读 feeds.txt（每行一个 RSS 链接，# 为注释）；缺失则生成模板。"""
    path = Path(path)
    if not path.exists():
        path.write_text(
            "# 每行一个 RSS 链接（mikanani 番剧页的 RSS 订阅地址），# 开头为注释\n"
            "# https://mikanani.me/RSS/Bangumi?bangumiId=3985&subgroupid=583\n",
            encoding="utf-8",
        )
        print(f"[提示] 已生成 {path.name}，请把 RSS 链接粘贴进去后重新运行。")
        return []
    urls: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)
    return urls


def rebase_paths(cfg: dict[str, Any], root: Path) -> dict[str, Any]:
    """把相对路径统一为项目根目录下的绝对路径（任何 cwd 运行都正确）。"""
    for key in ("save_path", "state_file", "resume_file", "log_file"):
        p = Path(cfg[key])
        if not p.is_absolute():
            cfg[key] = str((root / p).resolve())
    return cfg
