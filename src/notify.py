"""通过飞书自定义机器人 Webhook 推送每日摘要（交互式卡片）。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.request

from src.models import DigestItem

TIMEOUT_SEC = 10


class NotifyError(RuntimeError):
    pass


def _clip(text: str, limit: int = 400) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _md_title(title: str) -> str:
    # 方括号会打断 lark_md 的链接语法，替换为全角
    return _clip(title, 200).replace("[", "（").replace("]", "）")


def sign(secret: str, timestamp: int) -> str:
    """飞书自定义机器人“签名校验”算法：HMAC-SHA256，密钥为 "{timestamp}\\n{secret}"，消息为空。"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _item_markdown(index: int, item: DigestItem) -> str:
    paper = item.paper
    lines = [f"**{index}. [{_md_title(paper.title)}]({paper.url})**", f"评分 {item.score}/10"]
    if item.reason:
        lines[-1] += f" · {_clip(item.reason, 100)}"

    if item.analysis:
        a = item.analysis
        one_liner = a.get("one_liner") or a.get("core_problem", "")
        if one_liner:
            lines.append(f"**一句话摘要**：{_clip(one_liner, 300)}")
        if a.get("method"):
            lines.append(f"**主要方法**：{_clip(a['method'])}")
        if a.get("key_results"):
            lines.append(f"**关键结果**：{_clip(a['key_results'])}")
        if a.get("limitations"):
            lines.append(f"**局限性**：{_clip(a['limitations'])}")
        takeaways = [f"- {_clip(t, 200)}" for t in a.get("takeaways", [])[:3]]
        if takeaways:
            lines.append("**对实践者的启发**：\n" + "\n".join(takeaways))
    else:
        lines.append(f"（未精读：{item.read_status}，仅提供标题与链接）")

    if item.github_links:
        lines.append("**代码**：" + " ".join(item.github_links[:3]))
    return "\n".join(lines)


def build_card(title: str, date: str, items: list[DigestItem], footer: str = "") -> dict:
    elements: list[dict] = []
    for i, item in enumerate(items, start=1):
        if i > 1:
            elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": _item_markdown(i, item)}})
    if not items:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "今日没有符合条件的新论文。"}})
    if footer:
        elements.append({"tag": "hr"})
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": footer}]})

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"{title} · {date}"},
                "template": "blue",
            },
            "elements": elements,
        },
    }


def with_signature(payload: dict, secret: str | None, now: int | None = None) -> dict:
    if not secret:
        return payload
    ts = int(time.time()) if now is None else now
    return {**payload, "timestamp": str(ts), "sign": sign(secret, ts)}


def send(webhook_url: str, payload: dict, timeout: float = TIMEOUT_SEC) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=body, headers={"Content-Type": "application/json; charset=utf-8"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    # 飞书业务错误也可能以 HTTP 200 返回，必须检查 code
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise NotifyError(f"飞书返回非 JSON 内容: {raw[:200]}") from exc
    if result.get("code", 0) != 0:
        raise NotifyError(f"飞书推送失败 code={result.get('code')} msg={result.get('msg')}")
