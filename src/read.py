"""精读：拉取 arXiv HTML 版本，抽取引言/方法/实验/结论，调用模型生成中文讲解。"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
from collections.abc import Iterable
from dataclasses import dataclass, field
from html.parser import HTMLParser

from src.fetch import http_get
from src.models import Paper
from src.usage import UsageTracker

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是资深 AI 研究员，为工程师撰写中文论文精读笔记。
请基于给出的标题、摘要和正文摘录（正文可能不完整），输出一个 JSON 对象：
{
  "one_liner": "一句话概括论文的核心贡献（不超过 60 字）",
  "core_problem": "论文要解决的核心问题（2-3 句）",
  "method": "主要方法或系统设计，突出与 AI Agent 相关的关键机制（3-5 句）",
  "key_results": "关键结果：列出 2-4 个最重要的数字或对比，并说明基准与对照对象",
  "limitations": "局限性（2-3 条，包括作者自述的和你从设计中看出的）",
  "takeaways": ["对实践者的启发 1", "对实践者的启发 2", "对实践者的启发 3"]
}
要求：不要编造正文中没有的数字或结论；若正文缺失，请注明结论仅基于摘要。只输出 JSON，不要使用 Markdown 代码块。"""

HEADING_TAGS = {"h1", "h2", "h3", "h4"}
BLOCK_TAGS = {"p", "li", "figcaption", "tr", "div", "section"}
SKIP_TAGS = {"script", "style", "annotation"}  # annotation 是 LaTeXML 中公式的 TeX 源码，不放入正文

# 章节分桶：先匹配排除规则，再按桶匹配标题
EXCLUDE_HEADING = re.compile(
    r"related work|background|preliminar|acknowledg|appendi|ethic|impact statement|checklist|supplement",
    re.IGNORECASE,
)
REFERENCE_HEADING = re.compile(r"^\W*(references?|bibliography)\W*$", re.IGNORECASE)
BUCKETS = {
    "introduction": re.compile(r"introduction", re.IGNORECASE),
    "method": re.compile(r"method|approach|framework|architecture|design|proposed|our (model|system)", re.IGNORECASE),
    "experiments": re.compile(r"experiment|evaluation|result|benchmark|empirical|analysis", re.IGNORECASE),
    "conclusion": re.compile(r"conclusion|discussion|limitation|future work", re.IGNORECASE),
}
GITHUB_RE = re.compile(r"https?://github\.com/[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+")
# arXiv HTML 页面模板里的非论文仓库（页脚的反馈与渲染工具），不是论文代码
NON_PAPER_REPOS = {"arxiv/html_feedback", "brucemiller/latexml"}


@dataclass
class Section:
    heading: str
    body: str


@dataclass
class Reading:
    analysis: dict
    github_links: list[str] = field(default_factory=list)
    source: str = "abstract"  # "html" 或 "abstract"


class _SectionParser(HTMLParser):
    """把 arXiv HTML 拆成 (标题, 正文) 列表。参考文献部分不收集。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sections: list[Section] = [Section(heading="", body="")]
        self.links: list[str] = []
        self._skip = 0
        self._in_heading = False
        self._in_refs = False
        self._heading_buf: list[str] = []
        self._text_buf: list[str] = []

    def _flush_text(self) -> None:
        text = " ".join("".join(self._text_buf).split())
        self._text_buf = []
        if text and not self._in_refs:
            section = self.sections[-1]
            section.body = f"{section.body}\n{text}" if section.body else text

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if tag in HEADING_TAGS:
            self._flush_text()
            self._in_heading = True
            self._heading_buf = []
        elif tag == "a" and not self._in_refs:
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        elif tag in BLOCK_TAGS:
            self._flush_text()

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag in HEADING_TAGS and self._in_heading:
            self._in_heading = False
            heading = " ".join("".join(self._heading_buf).split())
            self._in_refs = bool(REFERENCE_HEADING.match(heading))
            if not self._in_refs:
                self.sections.append(Section(heading=heading, body=""))
        elif tag in BLOCK_TAGS:
            self._flush_text()

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_heading:
            self._heading_buf.append(data)
        elif not self._in_refs:
            self._text_buf.append(data)

    def close(self):
        super().close()
        self._flush_text()


def extract_sections(html: str) -> tuple[list[Section], list[str]]:
    parser = _SectionParser()
    parser.feed(html)
    parser.close()
    return [s for s in parser.sections if s.body], parser.links


def build_excerpt(sections: list[Section], shares: dict[str, float], max_chars: int) -> str:
    """按 引言 -> 方法 -> 实验 -> 结论 的顺序填充正文，各桶不超过其占比，未用完的预算顺延。"""
    parts: list[str] = []
    remaining = max_chars
    for bucket, pattern in BUCKETS.items():
        if remaining <= 0:
            break
        cap = min(remaining, int(max_chars * shares.get(bucket, 0)))
        used = 0
        for section in sections:
            if EXCLUDE_HEADING.search(section.heading) or not pattern.search(section.heading):
                continue
            room = cap - used
            if room <= 0:
                break
            block = f"## {section.heading}\n{section.body}\n"
            if len(block) > room:
                cut = block.rfind("\n", 0, room)
                block = block[: cut if cut > 0 else room]
            parts.append(block)
            used += len(block)
        remaining -= used
    return "\n".join(parts).strip()


def github_links(texts: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for text in texts:
        for match in GITHUB_RE.finditer(text):
            url = match.group(0).rstrip(".")
            owner, repo = url.split("github.com/", 1)[1].split("/", 1)
            if f"{owner}/{repo}".lower() in NON_PAPER_REPOS:
                continue
            seen.setdefault(url, None)
    return list(seen)


def build_user_message(paper: Paper, excerpt: str, source: str) -> str:
    note = "（正文不可用，仅基于摘要）" if source == "abstract" else ""
    body = excerpt if excerpt else "（无正文摘录）"
    return (
        f"标题: {paper.title}\n"
        f"作者: {', '.join(paper.authors)}\n"
        f"摘要: {paper.abstract}\n\n"
        f"正文摘录{note}:\n{body}"
    )


def parse_analysis(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("输出中找不到 JSON 对象")
    data = json.loads(match.group(0))
    takeaways = data.get("takeaways", [])
    if isinstance(takeaways, str):
        takeaways = [takeaways]
    return {
        "one_liner": str(data.get("one_liner", "")).strip(),
        "core_problem": str(data.get("core_problem", "")).strip(),
        "method": str(data.get("method", "")).strip(),
        "key_results": str(data.get("key_results", "")).strip(),
        "limitations": str(data.get("limitations", "")).strip(),
        "takeaways": [str(t).strip() for t in takeaways if str(t).strip()],
    }


def worst_case_read_tokens(max_input_chars: int, max_output_tokens: int) -> int:
    """预算检查用的上界估算：输入按 1 token ≈ 3 字符计，另加系统提示与输出上限。"""
    system_overhead = len(SYSTEM_PROMPT) // 2
    return max_input_chars // 3 + system_overhead + max_output_tokens


def fetch_html(html_base: str, arxiv_id: str, timeout: float) -> str | None:
    """返回 HTML 文本；该论文没有 HTML 版本（404）时返回 None。"""
    try:
        return http_get(f"{html_base}/{arxiv_id}", timeout=timeout, retries=1)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def read_paper(
    client,
    paper: Paper,
    *,
    model: str,
    html_base: str,
    max_input_chars: int,
    section_shares: dict[str, float],
    max_output_tokens: int,
    effort: str,
    tracker: UsageTracker,
    timeout: float = 30,
) -> Reading:
    sections: list[Section] = []
    links: list[str] = []
    source = "abstract"
    try:
        html = fetch_html(html_base, paper.arxiv_id, timeout)
        if html:
            sections, links = extract_sections(html)
    except Exception as exc:  # noqa: BLE001 - 正文获取失败时退回摘要
        log.warning("%s 的 HTML 获取失败，退回摘要: %s", paper.arxiv_id, exc)

    # 正文预算 = 总输入上限 - 固定部分（标题、作者、摘要、模板文字）
    fixed = len(build_user_message(paper, "", "abstract"))
    excerpt = build_excerpt(sections, section_shares, max(0, max_input_chars - fixed))
    if excerpt:
        source = "html"
    user_text = build_user_message(paper, excerpt, source)

    links_found = github_links([paper.abstract, *links, *(s.body for s in sections)])

    response = client.messages.create(
        model=model,
        max_tokens=max_output_tokens,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_text}],
        output_config={"effort": effort},
    )
    tracker.record("read", model, response.usage)
    text = "".join(block.text for block in response.content if block.type == "text")
    return Reading(analysis=parse_analysis(text), github_links=links_found, source=source)
