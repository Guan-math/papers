from src.filter import compile_keyword, filter_papers, match_keywords
from tests.conftest import make_paper

BASE_CFG = {
    "keywords": ["agent", "tool use", "MCP", "prompt injection"],
    "exclude_keywords": [],
    "fields": ["title", "abstract"],
    "min_hits": 1,
    "case_sensitive": False,
}


def test_keyword_matches_plural_and_case_insensitively():
    pattern = compile_keyword("agent")
    assert match_keywords("Agents for science", [pattern]) == ["agent"]
    assert match_keywords("LLM AGENT", [pattern]) == ["agent"]


def test_keyword_respects_word_boundaries():
    pattern = compile_keyword("agent")
    assert match_keywords("A reagent for chemistry", [pattern]) == []
    assert match_keywords("user-agent strings", [pattern]) == ["agent"]


def test_space_in_keyword_matches_hyphen_and_space():
    pattern = compile_keyword("tool use")
    assert match_keywords("Improving Tool-Use in LLMs", [pattern]) == ["tool use"]
    assert match_keywords("Tool use benchmarks", [pattern]) == ["tool use"]
    assert match_keywords("tooluse", [pattern]) == []


def test_acronym_matches_plural():
    pattern = compile_keyword("MCP")
    assert match_keywords("Servers exposing MCPs", [pattern]) == ["MCP"]
    assert match_keywords("mcp servers", [pattern]) == ["MCP"]


def test_case_sensitive_option():
    pattern = compile_keyword("MCP", case_sensitive=True)
    assert match_keywords("mcp servers", [pattern]) == []
    assert match_keywords("MCP servers", [pattern]) == ["MCP"]


def test_filter_keeps_papers_with_any_keyword_in_title_or_abstract():
    hit_title = make_paper("1", title="Agent Memory Systems", abstract="Nothing relevant.")
    hit_abstract = make_paper("2", title="Something", abstract="We study prompt injection attacks.")
    miss = make_paper("3", title="Image classification", abstract="A CNN for images.")

    kept = filter_papers([hit_title, hit_abstract, miss], BASE_CFG)
    assert [p.arxiv_id for p in kept] == ["1", "2"]


def test_filter_ignores_fields_not_configured():
    paper = make_paper("1", title="Plain title", abstract="Mentions agent here.")
    cfg = {**BASE_CFG, "fields": ["title"]}
    assert filter_papers([paper], cfg) == []


def test_exclude_keywords_drop_papers():
    paper = make_paper("1", title="Agent for reinforcement learning", abstract="")
    cfg = {**BASE_CFG, "exclude_keywords": ["reinforcement learning"]}
    assert filter_papers([paper], cfg) == []
    assert len(filter_papers([paper], BASE_CFG)) == 1


def test_min_hits_requires_multiple_distinct_keywords():
    one = make_paper("1", title="Agent", abstract="")
    two = make_paper("2", title="Agent with tool use", abstract="")
    cfg = {**BASE_CFG, "min_hits": 2}
    assert [p.arxiv_id for p in filter_papers([one, two], cfg)] == ["2"]
