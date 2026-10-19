"""跨模块共享的数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Paper:
    arxiv_id: str  # 不带版本号，例如 "2410.12345"
    title: str
    authors: tuple[str, ...]
    abstract: str
    url: str  # arXiv 摘要页链接
    published: datetime  # v1 提交时间（UTC）
    updated: datetime  # 最新版本时间（UTC）
    categories: tuple[str, ...]


@dataclass
class DigestItem:
    paper: Paper
    score: int
    reason: str = ""  # 打分理由（一句话）
    analysis: dict | None = None  # 精读结果：core_problem / method / key_results / limitations / takeaways
    github_links: list[str] = field(default_factory=list)
    source: str = "abstract"  # 精读输入来源："html" 或 "abstract"
    read_status: str = "not_selected"  # done / skipped_budget / failed / not_selected


@dataclass
class DigestStats:
    fetched: int = 0
    filtered: int = 0
    scored: int = 0
