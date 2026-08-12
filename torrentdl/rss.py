"""mikanani RSS：拉取、解析、标题分析、目录名生成。"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.request import Request, urlopen

from .logger import get_logger

_log = get_logger(__name__)

_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


@dataclass
class Item:
    title: str
    link: str
    guid: str = ""


@dataclass
class Feed:
    url: str
    title: str
    items: list[Item] = field(default_factory=list)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(entry: ET.Element, name: str) -> str | None:
    for child in entry:
        if _local(child.tag) == name:
            return (child.text or "").strip() or None
    return None


def _extract_link(entry: ET.Element) -> str:
    """兼容 Atom（link[rel=enclosure]）与 RSS 2.0（enclosure/link）。"""
    links = [c for c in entry if _local(c.tag) == "link"]
    for c in links:
        href = (c.get("href") or "").strip()
        if href and c.get("rel") == "enclosure":
            return href
    for c in entry:
        if _local(c.tag) == "enclosure":
            url = (c.get("url") or "").strip()
            if url:
                return url
    for c in links:
        href = (c.get("href") or c.text or "").strip()
        if href:
            return href
    return ""


def fetch(url: str, timeout: int = 20) -> Feed:
    if not url:
        raise ValueError("empty RSS url")
    req = Request(url, headers={"User-Agent": "torrent-dl/0.3"})
    with urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    feed = parse(data, url)
    _log.info("RSS %s: %d item(s).", url, len(feed.items))
    return feed


def parse(data: bytes, url: str = "") -> Feed:
    """解析 Atom / RSS 2.0，返回 Feed（含 feed 标题与条目）。"""
    root = ET.fromstring(data)
    title = ""
    items: list[Item] = []
    for entry in root.iter():
        tag = _local(entry.tag)
        if tag == "title" and not title:
            title = (entry.text or "").strip()
            continue
        if tag not in ("item", "entry"):
            continue
        item_title = _child_text(entry, "title") or ""
        link = _extract_link(entry)
        guid = _child_text(entry, "guid") or _child_text(entry, "id") or link
        if link:
            items.append(Item(title=item_title.strip(), link=link,
                              guid=guid or link))
    return Feed(url=url, title=title, items=items)


_EPISODE_RE = re.compile(
    r"(?:第\s*(\d{1,3})\s*[话集]"
    r"|EP?\s*(\d{1,3})"
    r"|[-\s_](\d{1,3})(?=\s*(?:\[|$))"
    r"|\[(\d{1,3})\]"
    r"|E(\d{1,3}))",
    re.IGNORECASE,
)


def episode_key(title: str) -> str | None:
    """从标题提取集数（标准化为 3 位字符串）；提取不到返回 None。"""
    m = _EPISODE_RE.search(title or "")
    if not m:
        return None
    num = next((g for g in m.groups() if g), None)
    return f"{int(num):03d}" if num else None


def strip_episode_markers(title: str) -> str:
    t = re.sub(r"\[[^\]]*\]", " ", title)
    t = _EPISODE_RE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def make_folder_name(feed: Feed) -> str:
    """目录名：feed 标题 -> 首条标题兜底 -> bangumiId 兜底 -> 清洗非法字符。"""
    title = (feed.title or "").strip()
    title = re.sub(r"^Mikan Project\s*-\s*", "", title)   # 去掉站点前缀
    if not title and feed.items:
        title = strip_episode_markers(feed.items[0].title)
    if not title:
        m = re.search(r"bangumiId=(\d+)", feed.url)
        title = f"bangumi_{m.group(1)}" if m else "anime"
    title = _ILLEGAL.sub("_", title).strip(" .")[:80]
    return title or "anime"


def make_matcher(include=(), exclude=()):
    """预编译标题过滤规则：exclude 命中任一跳过；include 非空时至少命中一个。"""
    inc = [re.compile(p, re.IGNORECASE) for p in include]
    exc = [re.compile(p, re.IGNORECASE) for p in exclude]

    def match(title: str) -> bool:
        if any(p.search(title) for p in exc):
            return False
        if inc and not any(p.search(title) for p in inc):
            return False
        return True

    return match
