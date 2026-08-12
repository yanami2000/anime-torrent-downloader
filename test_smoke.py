"""离线冒烟测试：RSS 解析、标题分析、目录名、配置模板、done.json。"""

import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=DeprecationWarning)


def test_rss_atom():
    from torrentdl import rss
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <title>地。-关于地球的运动-</title>
      <entry>
        <title>[LoliHouse] Some Anime - 01 [WebRip 1080p HEVC-10bit AAC]</title>
        <id>tag:mikanani,2026:123</id>
        <link rel="alternate" href="https://mikanani.me/Home/20260719/123"/>
        <link rel="enclosure" href="https://mikanani.me/Download/20260719/abc.torrent"
              type="application/x-bittorrent"/>
      </entry>
      <entry>
        <title>[GroupB] Other Show - 02 [720p]</title>
        <id>tag:mikanani,2026:456</id>
        <link rel="enclosure" href="https://mikanani.me/Download/20260719/def.torrent"/>
      </entry>
    </feed>""".encode("utf-8")
    feed = rss.parse(xml, "https://mikanani.me/RSS/Bangumi?bangumiId=3985")
    assert feed.title == "地。-关于地球的运动-", feed.title
    assert len(feed.items) == 2
    assert feed.items[0].link.endswith("abc.torrent")
    assert feed.items[0].guid == "tag:mikanani,2026:123"

    folder = rss.make_folder_name(feed)
    assert folder == "地。-关于地球的运动-", folder


def test_rss2():
    from torrentdl import rss
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <title>Show 01 RSS</title>
      <item>
        <title>Show 01 [1080p]</title>
        <guid>g1</guid>
        <link>https://example.com/1.torrent</link>
      </item>
      <item>
        <title>Show 02</title>
        <guid>g2</guid>
        <enclosure url="https://example.com/2.torrent"
                   type="application/x-bittorrent"/>
      </item>
    </channel></rss>""".encode("utf-8")
    feed = rss.parse(xml)
    assert feed.title == "Show 01 RSS"
    assert len(feed.items) == 2
    assert feed.items[1].link.endswith("2.torrent")


def test_episode_and_folder():
    from torrentdl import rss
    assert rss.episode_key("Some Show - 12 [1080p]") == "012"
    assert rss.episode_key("Some Show 第03话") == "003"
    assert rss.episode_key("Some Show [05]") == "005"
    assert rss.episode_key("Some Show EP09") == "009"
    assert rss.episode_key("Movie Special") is None

    # 非法字符清洗 + 兜底目录名
    feed = rss.Feed(url="https://mikanani.me/RSS/Bangumi?bangumiId=42", title="")
    assert rss.make_folder_name(feed) == "bangumi_42"
    bad = rss.Feed(url="", title='A:B?C*D"E<F>G|H\\I/J')
    name = rss.make_folder_name(bad)
    assert "/" not in name and ":" not in name and "?" not in name
    site = rss.Feed(url="", title="Mikan Project - 才女的侍从")
    assert rss.make_folder_name(site) == "才女的侍从"


def test_temp_suffix():
    """libtorrent torrent_info 的 .part 改名（回归：file_path 取自 ti.files()）。"""
    import libtorrent as lt
    from torrentdl.engine import TorrentEngine
    info = {b"name": b"test-show", b"piece length": 16384,
            b"length": 1000, b"pieces": bytes(20)}
    ti = lt.torrent_info(lt.bdecode(lt.bencode(
        {b"announce": b"udp://tracker.opentrackr.org:1337/announce",
         b"info": info})))
    with tempfile.TemporaryDirectory() as tmp:
        engine = TorrentEngine({
            "save_path": tmp,
            "state_file": str(Path(tmp) / "done.json"),
            "resume_file": str(Path(tmp) / "resume.pkl"),
            "temp_suffix": ".part",
        })
        engine._apply_ti_suffix(ti)
        assert ti.files().file_path(0).endswith(".part")
        engine._apply_ti_suffix(ti)          # 幂等
        assert ti.files().file_path(0).endswith(".part")


def test_matcher():
    from torrentdl import rss
    m = rss.make_matcher(include=["1080p"], exclude=["720p"])
    assert m("[LoliHouse] Show - 01 [1080p]")
    assert not m("[LoliHouse] Show - 01 [720p]")
    m2 = rss.make_matcher(exclude=["GroupB"])
    assert m2("anything")
    assert not m2("[GroupB] x")


def test_episode_order():
    """decide() 按集数排序，_prefetch 保持输入顺序（回归）。"""
    from torrentdl import rss
    from torrentdl.pipeline import decide
    from torrentdl.engine import TorrentEngine
    import tempfile

    titles = [
        "[ANi] Show - 02 [1080P][MP4]",
        "[ANi] Show - 05 [1080P][MP4]",
        "[ANi] Show - 01 [1080P][MP4]",
        "[ANi] Show - 03 [1080P][MP4]",
        "[ANi] Show - 04 [1080P][MP4]",
    ]
    feed = rss.Feed(
        url="https://mikanani.me/RSS/Bangumi?bangumiId=1",
        title="Show",
        items=[rss.Item(title=t, link=f"https://x/{i}", guid=f"g{i}")
               for i, t in enumerate(titles)],
    )
    with tempfile.TemporaryDirectory() as tmp:
        engine = TorrentEngine({
            "save_path": tmp,
            "state_file": str(Path(tmp) / "done.json"),
            "resume_file": str(Path(tmp) / "resume.pkl"),
        })
        picked = decide(feed, engine, {"stop_at_first_seen": True,
                                       "sort_by_episode": True},
                        rss.make_matcher())
        eps = [rss.episode_key(i.title) for i in picked]
        assert eps == ["001", "002", "003", "004", "005"], eps


def test_console_render():
    from torrentdl.console import render_line
    rows = [
        {"episode": "002", "name": "Show - 02", "state": 3, "progress": 0.5,
         "download_rate": 1024, "upload_rate": 10,
         "num_seeds": 2, "num_peers": 3},
        {"episode": "001", "name": "Show - 01", "state": 3, "progress": 1.0,
         "download_rate": 0, "upload_rate": 0,
         "num_seeds": 0, "num_peers": 0},
        {"episode": "003", "name": "Show - 03", "state": 0, "progress": 0.0,
         "download_rate": 0, "upload_rate": 0,
         "num_seeds": 0, "num_peers": 0},
    ]
    line = render_line(rows, "12:00:00")
    assert "EP001" in line and "EP002" in line
    assert "100%" in line
    assert "1 queued" in line
    assert "\r" not in line and "\n" not in line


def test_config_templates():
    from torrentdl.config import load_config, load_feeds, rebase_paths
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cfg = load_config(root / "config.json")
        assert cfg["temp_suffix"] == ".part"
        assert cfg["progress_log_interval"] == 15
        assert (root / "config.json").exists()
        urls = load_feeds(root / "feeds.txt")
        assert urls == []
        assert (root / "feeds.txt").exists()
        (root / "feeds.txt").write_text(
            "# comment\nhttps://mikanani.me/RSS/Bangumi?bangumiId=1\n\n"
            "https://mikanani.me/RSS/Bangumi?bangumiId=2&subgroupid=5\n",
            encoding="utf-8")
        assert load_feeds(root / "feeds.txt") == [
            "https://mikanani.me/RSS/Bangumi?bangumiId=1",
            "https://mikanani.me/RSS/Bangumi?bangumiId=2&subgroupid=5",
        ]
        cfg = rebase_paths(cfg, root)
        assert Path(cfg["save_path"]).is_absolute()


def test_done_store():
    from torrentdl.state import DoneStore
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "done.json")
        store = DoneStore(path)
        store.mark_done("abc123", "Show 01")
        store.mark_seen("https://example.com/1.torrent")
        reloaded = DoneStore(path)
        assert reloaded.is_done("abc123")
        assert reloaded.is_seen("https://example.com/1.torrent")


def test_engine_import():
    from torrentdl.engine import TorrentEngine
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        e = TorrentEngine({
            "save_path": tmp,
            "state_file": str(Path(tmp) / "done.json"),
            "resume_file": str(Path(tmp) / "resume.pkl"),
        })
        assert e.has_pending() is False


if __name__ == "__main__":
    for fn in (test_rss_atom, test_rss2, test_episode_and_folder,
               test_temp_suffix, test_matcher, test_episode_order,
               test_console_render,
               test_config_templates, test_done_store,
               test_engine_import):
        fn()
        print(f"OK  {fn.__name__}")
    print("all smoke tests passed")
