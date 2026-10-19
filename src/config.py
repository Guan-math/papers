"""加载 config.yaml，并用环境变量覆盖少量运行参数。"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config.yaml"


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _env(name: str) -> str | None:
    """空字符串视为未设置（GitHub Actions 中未配置的变量会是空串）。"""
    value = os.environ.get(name, "").strip()
    return value or None


def score_model(cfg: dict) -> str:
    return _env("SCORE_MODEL") or cfg["scoring"]["model"]


def read_model(cfg: dict) -> str:
    return _env("READ_MODEL") or cfg["reading"]["model"]


def daily_token_budget(cfg: dict) -> int:
    value = _env("DAILY_TOKEN_BUDGET")
    return int(value) if value else int(cfg["budget"]["daily_tokens"])
