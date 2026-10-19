"""记录每次 API 调用的 token 用量，并估算成本。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class CallRecord:
    stage: str
    model: str
    input_tokens: int
    output_tokens: int


@dataclass
class UsageTracker:
    pricing: dict[str, dict[str, float]]  # model -> {"input": $/MTok, "output": $/MTok}
    records: list[CallRecord] = field(default_factory=list)

    def record(self, stage: str, model: str, usage) -> CallRecord:
        """usage 为 SDK 返回的 response.usage（需有 input_tokens / output_tokens）。"""
        rec = CallRecord(
            stage=stage,
            model=model,
            input_tokens=int(usage.input_tokens or 0),
            output_tokens=int(usage.output_tokens or 0),
        )
        self.records.append(rec)
        log.info(
            "[%s] %s 调用: input=%d output=%d tokens, 本次成本约 $%.4f",
            stage,
            model,
            rec.input_tokens,
            rec.output_tokens,
            self.cost_of(rec),
        )
        return rec

    @property
    def total_tokens(self) -> int:
        return sum(r.input_tokens + r.output_tokens for r in self.records)

    def cost_of(self, rec: CallRecord) -> float:
        price = self.pricing.get(rec.model)
        if price is None:
            log.warning("config.yaml 中缺少 %s 的单价，成本按 0 计算", rec.model)
            return 0.0
        return (rec.input_tokens * price["input"] + rec.output_tokens * price["output"]) / 1_000_000

    @property
    def total_cost(self) -> float:
        return sum(self.cost_of(r) for r in self.records)

    def summary(self, budget: int) -> str:
        lines = []
        for stage in ("score", "read"):
            recs = [r for r in self.records if r.stage == stage]
            if not recs:
                continue
            inp = sum(r.input_tokens for r in recs)
            out = sum(r.output_tokens for r in recs)
            cost = sum(self.cost_of(r) for r in recs)
            lines.append(f"{stage}: {len(recs)} 次调用, input={inp}, output={out}, 成本约 ${cost:.4f}")
        lines.append(
            f"合计: {self.total_tokens} / 预算 {budget} tokens（{self.total_tokens / budget:.1%}）, "
            f"估算成本约 ${self.total_cost:.4f}"
        )
        return "\n".join(lines)
