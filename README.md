# anime-torrent-downloader

「RSS -> 自动下载」管道：把 mikanani 的 RSS 链接粘进 `feeds.txt`，运行
`uv run main.py`，自动扫描、分类、下载，完成后按规则收尾。

## 三步用法

1. 装 uv（若没有）：`pip install uv`，或见 <https://docs.astral.sh/uv/>。

2. 编辑 `feeds.txt`，每行一个 RSS 链接（mikanani 番剧页的 RSS 订阅地址）：

   ```
   https://mikanani.me/RSS/Bangumi?bangumiId=3985&subgroupid=583
   ```

3. 安装依赖并运行（uv 自动建 `.venv` + 装 libtorrent，一键跑）：

   ```powershell
   uv sync        # 首次运行一次即可
   uv run main.py # 等价于双击 start.bat
   ```

## 文件说明

用户日常只需要碰三个文件：

| 文件 | 作用 |
|------|------|
| `start.bat` | 一键运行（Windows） |
| `main.py` | 运行入口，不用改 |
| `feeds.txt` | 粘贴 RSS 链接（每行一个，`#` 为注释） |
| `config.json` | 可选设置（首次运行自动生成） |

`torrentdl/` 是内部库代码；`pyproject.toml` + `uv.lock` 是依赖声明（uv 管理），
一般不用动。

## config.json 关键项

| 配置 | 默认 | 说明 |
|------|------|------|
| `save_path` | `./downloads` | 下载根目录 |
| `temp_suffix` | `.part` | 未完成文件后缀，空字符串 = 关闭 |
| `organize_by_feed` | `true` | 按番剧分目录 |
| `max_active_downloads` | `3` | 同时下载的任务数 |
| `max_active_seeds` | `2` | 同时做种的任务数 |
| `seed_ratio` | `0.0` | 0 = 下载完即完成；1.0 = 做种到 1:1 |
| `seed_time_limit` | `0` | 做种秒数上限，0 = 不限（建议配 43200 限量做种） |
| `stall_skip_hours` | `6` | 死种（0 进度）超过该小时数则跳过 |
| `progress_log_interval` | `15` | 进度日志间隔（秒） |
| `resident` | `false` | true = 常驻轮询；false = 跑完退出 |
| `poll_interval` | `1800` | 常驻轮询间隔（秒） |
| `max_run_seconds` | `0` | 单次下载上限，配 Windows 计划任务用 |
| `sort_by_episode` | `true` | 同番内先下旧集 |

## 运行模式

- 默认跑完退出；配 Windows 计划任务（如每 2 小时 `python main.py --max-run-seconds 7200` 不需要，改 config.json 的 `max_run_seconds`）可分段续跑；
- `resident: true` 常驻轮询；
- 崩溃/断电后重新运行会从断点继续（resume 每 30 秒落盘一次）。

## 测试（无需网络）

```powershell
uv run test_smoke.py
```
