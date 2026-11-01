"""通知渠道：企业微信群机器人（免费）与通用 Webhook。"""
from __future__ import annotations

import json
from datetime import datetime

import httpx

from . import db
from .security import scrub

LEVEL_ICON = {
    "success": "✅",
    "skipped": "☑️",
    "pending": "⏳",
    "failed": "❌",
    "need_login": "🔑",
    "retryable": "⚠️",
    "info": "ℹ️",
}


def _client() -> httpx.AsyncClient:
    from . import config

    kwargs = {"timeout": 10.0, "trust_env": False}
    if config.HTTP_PROXY:
        try:
            return httpx.AsyncClient(proxy=config.HTTP_PROXY, **kwargs)
        except TypeError:
            return httpx.AsyncClient(proxies=config.HTTP_PROXY, **kwargs)
    return httpx.AsyncClient(**kwargs)


def wecom_enabled() -> bool:
    return bool(db.get_setting("wecom_webhook", "").strip())


async def send(title: str, body: str, level: str = "info", force: bool = False) -> tuple[bool, str]:
    """按配置推送。未配置渠道则静默跳过。"""
    if not db.get_setting("notify_enabled", "1") == "1" and not force:
        return False, "通知已关闭"

    icon = LEVEL_ICON.get(level, "•")
    text = f"{icon} **{title}**\n{scrub(body)}\n> {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    sent: list[str] = []

    hook = db.get_setting("wecom_webhook", "").strip()
    if hook:
        payload = {"msgtype": "markdown", "markdown": {"content": text}}
        try:
            async with _client() as client:
                resp = await client.post(hook, json=payload)
            data = {}
            try:
                data = resp.json()
            except Exception:
                pass
            if resp.status_code == 200 and data.get("errcode", 0) == 0:
                sent.append("企业微信")
            else:
                return False, f"企业微信推送失败：{resp.status_code} {resp.text[:120]}"
        except Exception as exc:
            return False, f"企业微信推送异常：{exc}"

    generic = db.get_setting("generic_webhook", "").strip()
    if generic:
        payload = {"title": title, "content": text, "level": level}
        try:
            async with _client() as client:
                resp = await client.post(generic, json=payload)
            if resp.status_code < 300:
                sent.append("Webhook")
        except Exception as exc:
            return False, f"Webhook 推送异常：{exc}"

    if not sent:
        return False, "未配置任何通知渠道"
    return True, "已推送到：" + "、".join(sent)


async def test() -> tuple[bool, str]:
    return await send("连通性测试", "这是一条来自 CheckinHub 的测试消息。", "info", force=True)


def dumps(detail: dict) -> str:
    try:
        return scrub(json.dumps(detail, ensure_ascii=False))[:4000]
    except Exception:
        return "{}"
