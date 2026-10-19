from datetime import datetime, timezone

import pytest

from src.models import Paper


def make_paper(
    arxiv_id: str = "2410.00001",
    title: str = "A Paper",
    abstract: str = "An abstract.",
) -> Paper:
    now = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
    return Paper(
        arxiv_id=arxiv_id,
        title=title,
        authors=("Alice", "Bob"),
        abstract=abstract,
        url=f"http://arxiv.org/abs/{arxiv_id}v1",
        published=now,
        updated=now,
        categories=("cs.AI",),
    )


@pytest.fixture
def paper_factory():
    return make_paper
