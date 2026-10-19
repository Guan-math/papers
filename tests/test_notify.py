import json

import pytest

from src import notify
from src.models import DigestItem
from tests.conftest import make_paper


class _FakeResponse:
    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _item(read: bool = True) -> DigestItem:
    item = DigestItem(
        paper=make_paper("2410.00001", title="Agent [Bench] for Tools"),
        score=9,
        reason="直接相关",
        read_status="done" if read else "skipped_budget",
    )
    if read:
        item.analysis = {
            "one_liner": "提出工具调用的可执行评测",
            "core_problem": "如何评测工具调用",
            "method": "构造可执行环境",
            "key_results": "提升 12%",
            "limitations": "只覆盖英文",
            "takeaways": ["先写评测", "记录失败"],
        }
        item.github_links = ["https://github.com/org/repo"]
    return item


def test_sign_matches_documented_algorithm():
    # 与飞书文档一致：key = "{timestamp}\n{secret}"，message 为空，HMAC-SHA256 后 base64
    import base64
    import hashlib
    import hmac

    expected = base64.b64encode(
        hmac.new(b"1700000000\nmysecret", digestmod=hashlib.sha256).digest()
    ).decode()
    assert notify.sign("mysecret", 1700000000) == expected


def test_with_signature_adds_fields_only_when_secret_set():
    payload = {"msg_type": "text"}
    assert notify.with_signature(payload, None) == payload
    signed = notify.with_signature(payload, "s", now=42)
    assert signed["timestamp"] == "42"
    assert signed["sign"] == notify.sign("s", 42)


def test_build_card_structure_and_links():
    card = notify.build_card("AI Agent 论文日报", "2026-10-19", [_item()], footer="Token 1/2")
    assert card["msg_type"] == "interactive"
    assert card["card"]["header"]["title"]["content"] == "AI Agent 论文日报 · 2026-10-19"
    text = card["card"]["elements"][0]["text"]["content"]
    # 标题中的方括号被替换，避免破坏 lark_md 链接语法
    assert "[Agent （Bench） for Tools](http://arxiv.org/abs/2410.00001v1)" in text
    assert "**一句话摘要**：提出工具调用的可执行评测" in text
    assert "**主要方法**：构造可执行环境" in text
    assert "https://github.com/org/repo" in text
    assert card["card"]["elements"][-1]["tag"] == "note"


def test_build_card_title_only_when_not_read():
    card = notify.build_card("T", "2026-10-19", [_item(read=False)])
    text = card["card"]["elements"][0]["text"]["content"]
    assert "未精读：skipped_budget" in text
    assert "**主要方法**" not in text


def test_build_card_empty_day():
    card = notify.build_card("T", "2026-10-19", [])
    assert "没有符合条件的新论文" in card["card"]["elements"][0]["text"]["content"]


def test_send_posts_json_and_accepts_success(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["headers"] = dict(req.headers)
        return _FakeResponse('{"StatusCode":0,"StatusMessage":"success","code":0,"data":{}}')

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    notify.send("https://hook.example/abc", {"msg_type": "text", "content": {"text": "hi"}})
    assert captured["url"] == "https://hook.example/abc"
    assert captured["body"]["msg_type"] == "text"


def test_send_raises_on_business_error_with_http_200(monkeypatch):
    monkeypatch.setattr(
        notify.urllib.request,
        "urlopen",
        lambda req, timeout: _FakeResponse('{"code":19021,"msg":"sign match fail"}'),
    )
    with pytest.raises(notify.NotifyError, match="19021"):
        notify.send("https://hook.example/abc", {"msg_type": "text"})


def test_send_raises_on_non_json(monkeypatch):
    monkeypatch.setattr(notify.urllib.request, "urlopen", lambda req, timeout: _FakeResponse("<html>"))
    with pytest.raises(notify.NotifyError):
        notify.send("https://hook.example/abc", {"msg_type": "text"})
