"""基于 config.yaml 中关键词列表的论文过滤。

规则（均可在 config.yaml 的 filter 段配置）：
- keywords：统计 fields 中命中的不同关键词个数，>= min_hits 才保留（默认 1，即命中任一即可）。
- exclude_keywords：任一排除词命中即丢弃。
- case_sensitive：是否区分大小写，默认 false。
关键词匹配细节：空格与连字符等价（"tool use" 匹配 "tool-use"），允许 s/es 复数后缀，
并要求前后为单词边界，因此 "agent" 不会命中 "reagent"。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from src.models import Paper


@dataclass(frozen=True)
class KeywordPattern:
    keyword: str
    regex: re.Pattern[str]


def compile_keyword(keyword: str, case_sensitive: bool = False) -> KeywordPattern:
    parts = [re.escape(p) for p in keyword.strip().split()]
    body = r"[\s\-]+".join(parts) if parts else ""
    # 单词边界 + 可选复数后缀：agent 命中 agents，不命中 reagent
    pattern = rf"(?<![A-Za-z0-9]){body}(?:e?s)?(?![A-Za-z0-9])"
    flags = 0 if case_sensitive else re.IGNORECASE
    return KeywordPattern(keyword=keyword, regex=re.compile(pattern, flags))


def match_keywords(text: str, patterns: Iterable[KeywordPattern]) -> list[str]:
    """返回 text 中命中的关键词（保持配置中的顺序，去重）。"""
    return [p.keyword for p in patterns if p.regex.search(text)]


def _field_text(paper: Paper, field_name: str) -> str:
    value = getattr(paper, field_name, "")
    return value if isinstance(value, str) else ""


def filter_papers(papers: list[Paper], cfg: dict) -> list[Paper]:
    case_sensitive = bool(cfg.get("case_sensitive", False))
    include = [compile_keyword(k, case_sensitive) for k in cfg.get("keywords", [])]
    exclude = [compile_keyword(k, case_sensitive) for k in cfg.get("exclude_keywords", []) or []]
    fields = list(cfg.get("fields", ["title", "abstract"]))
    min_hits = int(cfg.get("min_hits", 1))

    kept = []
    for paper in papers:
        text = "\n".join(_field_text(paper, f) for f in fields)
        if match_keywords(text, exclude):
            continue
        if len(match_keywords(text, include)) >= min_hits:
            kept.append(paper)
    return kept
