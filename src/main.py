"""主流程：抓取 -> 关键词过滤 -> 打分 -> 精读 Top-K -> 存档 Markdown -> 飞书推送。

用法（在仓库根目录执行）：
    python -m src.main                  # 完整运行
    python -m src.main --dry-run        # 完整运行，但不推送飞书
    python -m src.main --max-papers 5   # 只对前 5 篇候选打分（调试/省钱）
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic

from src.config import DEFAULT_CONFIG_PATH, ROOT, daily_token_budget, load_config, read_model, score_model
from src.fetch import fetch_recent
from src.filter import filter_papers
from src.models import DigestItem, DigestStats, Paper
from src.notify import build_card, send, with_signature
from src.read import read_paper, worst_case_read_tokens
from src.score import ScoreResult, score_papers
from src.usage import UsageTracker

log = logging.getLogger("main")

BEIJING = timezone(timedelta(hours=8))
TABLE_ROWS = 20  # 存档中展示的打分候选条数


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI Agent 论文每日摘要")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="配置文件路径")
    parser.add_argument("--max-papers", type=int, default=None, help="最多打分的候选篇数，覆盖 config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="不推送飞书，其余步骤照常执行")
    parser.add_argument("--date", default=None, help="存档日期 YYYY-MM-DD，默认为北京时间今天")
    return parser.parse_args(argv)


def render_markdown(
    title: str,
    date_str: str,
    items: list[DigestItem],
    ranked: list[tuple[Paper, ScoreResult]],
    stats: DigestStats,
    usage_summary: str,
) -> str:
    lines = [
        f"# {title} · {date_str}",
        "",
        f"- 抓取 {stats.fetched} 篇 → 关键词过滤 {stats.filtered} 篇 → 打分 {stats.scored} 篇 → 精读 {len(items)} 篇",
        "",
        "## 用量与成本",
        "",
        "```",
        usage_summary,
        "```",
        "",
        "## 今日精选",
        "",
    ]
    if not items:
        lines += ["今日没有符合条件的新论文。", ""]

    for i, item in enumerate(items, start=1):
        p = item.paper
        lines += [
            f"### {i}. {p.title}",
            "",
            f"- 链接：{p.url}",
            f"- 作者：{', '.join(p.authors)}",
            f"- 分类：{', '.join(p.categories)}　提交：{p.published:%Y-%m-%d %H:%M} UTC",
            f"- 评分：{item.score}/10　{item.reason}",
        ]
        if item.github_links:
            lines.append("- 代码：" + " ".join(item.github_links))
        if item.analysis:
            a = item.analysis
            lines += [
                f"- 精读来源：{'arXiv HTML 正文' if item.source == 'html' else '仅摘要（正文不可用）'}",
                "",
                f"**一句话摘要**：{a['one_liner']}",
                "",
                f"**核心问题**：{a['core_problem']}",
                "",
                f"**主要方法**：{a['method']}",
                "",
                f"**关键结果**：{a['key_results']}",
                "",
                f"**局限性**：{a['limitations']}",
                "",
                "**对实践者的启发**：",
                "",
            ]
            lines += [f"- {t}" for t in a["takeaways"]]
            lines.append("")
        else:
            lines += [f"- 精读状态：{item.read_status}（仅保留标题与链接）", ""]

    if ranked:
        lines += [
            "## 打分候选（前 {} 篇）".format(min(TABLE_ROWS, len(ranked))),
            "",
            "| 分数 | 标题 | 理由 |",
            "| --- | --- | --- |",
        ]
        for paper, result in ranked[:TABLE_ROWS]:
            title_cell = paper.title.replace("|", "\\|")
            reason_cell = result.reason.replace("|", "\\|")
            lines.append(f"| {result.score} | [{title_cell}]({paper.url}) | {reason_cell} |")
        lines.append("")
    return "\n".join(lines)


def run(
    cfg: dict,
    *,
    max_papers: int | None,
    dry_run: bool,
    date_str: str | None,
    now: datetime | None = None,
    client=None,
) -> Path:
    now = now or datetime.now(timezone.utc)
    date_str = date_str or now.astimezone(BEIJING).strftime("%Y-%m-%d")
    arxiv = cfg["arxiv"]
    scoring = cfg["scoring"]
    reading = cfg["reading"]
    budget = daily_token_budget(cfg)
    tracker = UsageTracker(pricing=cfg["pricing_usd_per_mtok"])
    stats = DigestStats()

    # 1. 抓取
    papers = fetch_recent(
        arxiv["categories"],
        arxiv["lookback_hours"],
        arxiv["api_url"],
        page_size=arxiv["page_size"],
        max_pages=arxiv["max_pages"],
        request_interval_sec=arxiv["request_interval_sec"],
        timeout_sec=arxiv["timeout_sec"],
        now=now,
    )
    stats.fetched = len(papers)

    # 2. 关键词过滤
    candidates = filter_papers(papers, cfg["filter"])
    stats.filtered = len(candidates)
    limit = max_papers if max_papers is not None else scoring.get("max_papers", 0)
    if limit:
        candidates = candidates[:limit]
    log.info("候选 %d 篇（上限 %s）", len(candidates), limit or "不限")

    # 3. 打分
    client = client or (anthropic.Anthropic() if candidates else None)
    scores: dict[str, ScoreResult] = {}
    if candidates:
        scores = score_papers(
            client,
            candidates,
            model=score_model(cfg),
            batch_size=scoring["batch_size"],
            abstract_max_chars=scoring["abstract_max_chars"],
            max_output_tokens=scoring["max_output_tokens"],
            effort=scoring["effort"],
            tracker=tracker,
        )
    stats.scored = len(scores)
    ranked_pairs = sorted(
        ((p, scores[p.arxiv_id]) for p in candidates if p.arxiv_id in scores),
        key=lambda pair: (pair[1].score, pair[0].published),
        reverse=True,
    )

    # 4. 精读 Top-K（超出预算则跳过，只保留标题与链接）
    top_k = reading["top_k"]
    items = [
        DigestItem(paper=p, score=r.score, reason=r.reason)
        for p, r in ranked_pairs[:top_k]
    ]
    worst = worst_case_read_tokens(reading["max_input_chars"], reading["max_output_tokens"])
    for item in items:
        if tracker.total_tokens + worst > budget:
            item.read_status = "skipped_budget"
            log.warning("token 预算不足（已用 %d / %d），跳过精读：%s", tracker.total_tokens, budget, item.paper.arxiv_id)
            continue
        try:
            reading_result = read_paper(
                client,
                item.paper,
                model=read_model(cfg),
                html_base=arxiv["html_base"],
                max_input_chars=reading["max_input_chars"],
                section_shares=reading["section_shares"],
                max_output_tokens=reading["max_output_tokens"],
                effort=reading["effort"],
                tracker=tracker,
            )
        except Exception:  # noqa: BLE001 - 单篇失败不影响其余论文与推送
            log.exception("精读失败：%s", item.paper.arxiv_id)
            item.read_status = "failed"
            continue
        item.analysis = reading_result.analysis
        item.github_links = reading_result.github_links
        item.source = reading_result.source
        item.read_status = "done"

    usage_summary = tracker.summary(budget)
    log.info("用量汇总:\n%s", usage_summary)

    # 5. 存档
    title = cfg["digest"]["title"]
    digest_dir = ROOT / cfg["digest"]["dir"]
    digest_dir.mkdir(parents=True, exist_ok=True)
    out_path = digest_dir / f"{date_str}.md"
    out_path.write_text(
        render_markdown(title, date_str, items, ranked_pairs, stats, usage_summary),
        encoding="utf-8",
    )
    log.info("已写入存档 %s", out_path)

    # 6. 推送
    footer = f"Token {tracker.total_tokens}/{budget}，估算成本约 ${tracker.total_cost:.4f}"
    payload = build_card(title, date_str, items, footer=footer)
    if dry_run:
        log.info("dry-run：跳过飞书推送，卡片内容如下")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        url = os.environ.get("FEISHU_WEBHOOK_URL", "").strip()
        if not url:
            raise RuntimeError("未设置 FEISHU_WEBHOOK_URL（如只想本地验证请加 --dry-run）")
        secret = os.environ.get("FEISHU_WEBHOOK_SECRET", "").strip() or None
        send(url, with_signature(payload, secret))
        log.info("已推送到飞书")
    return out_path


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)
    try:
        cfg = load_config(args.config)
        run(cfg, max_papers=args.max_papers, dry_run=args.dry_run, date_str=args.date)
    except Exception:  # noqa: BLE001 - 顶层统一记录并以非零码退出，便于 CI 报红
        log.exception("运行失败")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
