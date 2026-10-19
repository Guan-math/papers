"""从 arXiv 官方 API 抓取最近 N 小时内提交的论文元数据（不下载 PDF）。"""

from __future__ import annotations

import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from src.models import Paper

log = logging.getLogger(__name__)

USER_AGENT = "ai-agent-paper-digest/1.0 (+https://github.com/guan-math/papers)"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
_VERSION_RE = re.compile(r"v\d+$")


def http_get(url: str, timeout: float = 30, retries: int = 3, backoff: float = 2.0) -> str:
    """GET 请求，对 429 / 5xx / 网络错误做指数退避重试。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            retryable = e.code == 429 or e.code >= 500
            if not retryable or attempt == retries:
                raise
            log.warning("GET %s 返回 %s，%.0fs 后重试", url, e.code, backoff * 2**attempt)
        except urllib.error.URLError:
            if attempt == retries:
                raise
            log.warning("GET %s 网络错误，%.0fs 后重试", url, backoff * 2**attempt)
        time.sleep(backoff * 2**attempt)
    raise RuntimeError("unreachable")


def build_search_query(categories: list[str]) -> str:
    return "(" + " OR ".join(f"cat:{c}" for c in categories) + ")"


def _parse_time(text: str) -> datetime:
    # arXiv 时间格式形如 2026-10-18T12:34:56Z
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _arxiv_id(entry_id_url: str) -> str:
    # http://arxiv.org/abs/2410.12345v2 -> 2410.12345
    raw = entry_id_url.rsplit("/abs/", 1)[-1]
    return _VERSION_RE.sub("", raw)


def parse_feed(xml_text: str) -> list[Paper]:
    root = ET.fromstring(xml_text)
    papers = []
    for entry in root.findall(f"{ATOM}entry"):
        title = " ".join((entry.findtext(f"{ATOM}title") or "").split())
        if title.lower() == "error":  # arXiv 查询出错时会返回一条 title 为 Error 的 entry
            continue
        url = (entry.findtext(f"{ATOM}id") or "").strip()
        papers.append(
            Paper(
                arxiv_id=_arxiv_id(url),
                title=title,
                authors=tuple(
                    " ".join((a.findtext(f"{ATOM}name") or "").split())
                    for a in entry.findall(f"{ATOM}author")
                ),
                abstract=" ".join((entry.findtext(f"{ATOM}summary") or "").split()),
                url=url,
                published=_parse_time(entry.findtext(f"{ATOM}published")),
                updated=_parse_time(entry.findtext(f"{ATOM}updated")),
                categories=tuple(c.get("term", "") for c in entry.findall(f"{ATOM}category")),
            )
        )
    return papers


def fetch_recent(
    categories: list[str],
    lookback_hours: int,
    api_url: str,
    *,
    page_size: int = 100,
    max_pages: int = 10,
    request_interval_sec: float = 3.0,
    timeout_sec: float = 30,
    now: datetime | None = None,
    get: Callable[[str], str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Paper]:
    """返回 published 在 lookback 窗口内的论文，按提交时间倒序。

    按 submittedDate 倒序翻页。翻页停止条件基于 updated（最新版本时间）：
    排序键与 updated 一致，因此一旦 updated 早于窗口起点，后续页都不会再有新论文。
    """
    get = get or (lambda url: http_get(url, timeout=timeout_sec))
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=lookback_hours)
    query = build_search_query(categories)

    found: dict[str, Paper] = {}
    for page in range(max_pages):
        params = {
            "search_query": query,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "start": page * page_size,
            "max_results": page_size,
        }
        url = f"{api_url}?{urllib.parse.urlencode(params)}"
        log.info("抓取 arXiv 第 %d 页（start=%d）", page + 1, params["start"])
        entries = parse_feed(get(url))
        if not entries:
            break

        reached_cutoff = False
        for paper in entries:
            if paper.updated < cutoff:
                reached_cutoff = True
            elif paper.published >= cutoff:
                found[paper.arxiv_id] = paper
        if reached_cutoff or len(entries) < page_size:
            break
        sleep(request_interval_sec)

    papers = sorted(found.values(), key=lambda p: p.published, reverse=True)
    log.info("窗口 %d 小时内共 %d 篇新论文", lookback_hours, len(papers))
    return papers
