"""用 Anthropic API 按 1–10 分对候选论文打分（分批调用，输出结构化 JSON）。"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from src.models import Paper
from src.usage import UsageTracker

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是 AI Agent 领域的论文筛选员。根据每篇论文的标题和摘要，评估它与 AI Agent 研究的相关性（LLM 智能体、工具使用、规划、记忆、多智能体、计算机使用、提示注入、智能体评测、MCP 等）以及方法的创新性，给出 1 到 10 的整数分。

评分参考：
- 9-10：直接提出或显著推进 AI Agent 的方法、系统或评测，且有明显新意
- 6-8：与 Agent 密切相关，有一定新意或实用价值
- 3-5：只是沾边（例如仅顺带提到 agent 或 evaluation）
- 1-2：基本无关

只输出一个 JSON 数组，不要任何其他文字，格式如下：
[{"id": "<论文 id>", "score": <1-10 的整数>, "reason": "<不超过 40 字的中文理由>"}]
输入中的每个 id 都必须且只出现一次。"""


@dataclass(frozen=True)
class ScoreResult:
    score: int
    reason: str


def build_user_message(papers: list[Paper], abstract_max_chars: int) -> str:
    blocks = []
    for p in papers:
        abstract = p.abstract[:abstract_max_chars]
        blocks.append(f"[id: {p.arxiv_id}]\n标题: {p.title}\n摘要: {abstract}")
    return f"请为以下 {len(papers)} 篇论文打分。\n\n" + "\n\n".join(blocks)


def parse_scores(text: str, expected_ids: set[str]) -> dict[str, ScoreResult]:
    """从模型输出中解析打分结果。只保留 expected_ids 中的条目；分数截断到 1-10。"""
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        raise ValueError("输出中找不到 JSON 数组")
    items = json.loads(match.group(0))
    if not isinstance(items, list):
        raise ValueError("JSON 顶层不是数组")

    results: dict[str, ScoreResult] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        paper_id = str(item.get("id", "")).strip()
        if paper_id not in expected_ids:
            continue
        try:
            score = int(round(float(item["score"])))
        except (KeyError, TypeError, ValueError):
            continue
        results[paper_id] = ScoreResult(
            score=min(10, max(1, score)),
            reason=str(item.get("reason", "")).strip(),
        )
    return results


def _call_model(client, model: str, user_text: str, max_output_tokens: int, effort: str, tracker: UsageTracker):
    response = client.messages.create(
        model=model,
        max_tokens=max_output_tokens,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_text}],
        # 打分不需要长推理：关闭 thinking 并使用低 effort，避免思考 token 计费
        thinking={"type": "disabled"},
        output_config={"effort": effort},
    )
    tracker.record("score", model, response.usage)
    if response.stop_reason == "max_tokens":
        raise ValueError("输出被 max_tokens 截断")
    return "".join(block.text for block in response.content if block.type == "text")


def score_papers(
    client,
    papers: list[Paper],
    *,
    model: str,
    batch_size: int,
    abstract_max_chars: int,
    max_output_tokens: int,
    effort: str,
    tracker: UsageTracker,
) -> dict[str, ScoreResult]:
    """分批打分。单批失败会重试一次，仍失败则该批论文不参与排序（记录告警）。"""
    scores: dict[str, ScoreResult] = {}
    for start in range(0, len(papers), batch_size):
        batch = papers[start : start + batch_size]
        expected = {p.arxiv_id for p in batch}
        user_text = build_user_message(batch, abstract_max_chars)
        # 输出 token 上限随批大小线性增长，给每篇留足空间
        out_limit = max(max_output_tokens, 80 * len(batch))

        batch_scores: dict[str, ScoreResult] = {}
        for attempt in (1, 2):
            try:
                text = _call_model(client, model, user_text, out_limit, effort, tracker)
                batch_scores = parse_scores(text, expected)
                break
            except Exception as exc:  # noqa: BLE001 - 单批失败不应中断整体流程
                log.warning("打分第 %d 批第 %d 次尝试失败: %s", start // batch_size + 1, attempt, exc)

        missing = expected - batch_scores.keys()
        if missing:
            log.warning("本批有 %d 篇未得到有效分数，已跳过", len(missing))
        scores.update(batch_scores)
    return scores
