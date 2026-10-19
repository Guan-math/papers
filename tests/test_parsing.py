"""抓取、打分、精读中的纯解析逻辑（不联网）。"""

from datetime import datetime, timezone

from src import fetch, read, score

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2410.12345v2</id>
    <updated>2026-10-18T10:00:00Z</updated>
    <published>2026-10-18T08:00:00Z</published>
    <title>Agent   Memory
      for LLMs</title>
    <summary>  We study agent memory.
    </summary>
    <author><name>Alice Liu</name></author>
    <author><name>Bob</name></author>
    <category term="cs.AI" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2410.99999v1</id>
    <updated>2026-10-01T00:00:00Z</updated>
    <published>2026-10-01T00:00:00Z</published>
    <title>Old paper</title>
    <summary>old</summary>
  </entry>
</feed>
"""


def test_parse_feed_extracts_metadata_and_strips_version():
    papers = fetch.parse_feed(FEED)
    assert len(papers) == 2
    first = papers[0]
    assert first.arxiv_id == "2410.12345"
    assert first.title == "Agent Memory for LLMs"
    assert first.abstract == "We study agent memory."
    assert first.authors == ("Alice Liu", "Bob")
    assert first.categories == ("cs.AI", "cs.CL")
    assert first.published == datetime(2026, 10, 18, 8, 0, tzinfo=timezone.utc)


def test_fetch_recent_filters_window_and_paginates():
    now = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
    calls = []

    def fake_get(url):
        calls.append(url)
        # 第一页包含窗口内论文与一篇窗口外论文，且 updated 已早于窗口起点 -> 停止翻页
        return FEED

    papers = fetch.fetch_recent(
        ["cs.AI"], 24, "http://api.test/query", page_size=100, now=now, get=fake_get, sleep=lambda s: None
    )
    assert [p.arxiv_id for p in papers] == ["2410.12345"]
    assert len(calls) == 1
    assert "cat%3Acs.AI" in calls[0] and "sortBy=submittedDate" in calls[0]


def test_parse_scores_clamps_and_filters_ids():
    text = 'Here you go:\n[{"id": "a", "score": 12, "reason": "x"}, {"id": "b", "score": "7", "reason": "y"}, {"id": "zzz", "score": 5}]'
    result = score.parse_scores(text, {"a", "b"})
    assert result == {
        "a": score.ScoreResult(score=10, reason="x"),
        "b": score.ScoreResult(score=7, reason="y"),
    }


def test_parse_scores_rejects_missing_array():
    try:
        score.parse_scores("no json here", {"a"})
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_extract_sections_skips_references_and_scripts():
    html = """
    <html><body>
    <h2>1 Introduction</h2><p>Agents need memory.</p>
    <p>See <a href="https://github.com/acme/agentmem">code</a>.</p>
    <h2>2 Method</h2><p>We propose <annotation>x^2</annotation> a planner.</p>
    <h2>References</h2><p>Ref one https://github.com/other/paper</p>
    </body></html>
    """
    sections, links = read.extract_sections(html)
    headings = [s.heading for s in sections]
    assert "References" not in headings
    assert "https://github.com/acme/agentmem" in links
    assert "https://github.com/other/paper" not in " ".join(s.body for s in sections)
    method = next(s for s in sections if s.heading == "2 Method")
    assert "x^2" not in method.body and "We propose" in method.body


def test_build_excerpt_respects_limit_and_order():
    sections = [
        read.Section("1 Introduction", "intro " * 200),
        read.Section("2 Related Work", "should be skipped"),
        read.Section("3 Method", "method " * 200),
        read.Section("4 Experiments", "exp " * 200),
        read.Section("5 Conclusion", "concl " * 200),
    ]
    shares = {"introduction": 0.2, "method": 0.35, "experiments": 0.3, "conclusion": 0.15}
    excerpt = read.build_excerpt(sections, shares, max_chars=1000)
    assert len(excerpt) <= 1000
    assert "Related Work" not in excerpt
    assert excerpt.index("Introduction") < excerpt.index("Method") < excerpt.index("Conclusion")


def test_github_links_dedup_and_strip_trailing_dot():
    links = read.github_links(["see github.com/a/b.", "https://github.com/a/b and https://github.com/c/d"])
    assert links == ["https://github.com/a/b", "https://github.com/c/d"]


def test_github_links_skip_arxiv_page_chrome():
    texts = [
        "https://github.com/arXiv/html_feedback https://github.com/Gen-Verse/DiscoveryRSI",
        "https://github.com/brucemiller/LaTeXML",
    ]
    assert read.github_links(texts) == ["https://github.com/Gen-Verse/DiscoveryRSI"]


def test_parse_analysis_normalizes_fields():
    text = '```json\n{"one_liner": "O", "core_problem": "P", "method": "M", "key_results": "R", "limitations": "L", "takeaways": "one"}\n```'
    analysis = read.parse_analysis(text)
    assert analysis["takeaways"] == ["one"]
    assert analysis["core_problem"] == "P"
    assert analysis["one_liner"] == "O"
