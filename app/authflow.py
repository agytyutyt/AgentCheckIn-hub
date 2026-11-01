"""网页端扫码纳管：把「打开客户端 → 导出凭证 → 粘贴」压缩成「扫一扫」。

协议（口径见 ithtelab/workbuddy-manager 的 tencent.start_login / poll_login）：
  1. POST /v2/plugin/auth/state?platform=CLI  → {state, authUrl}
  2. authUrl 就是官方登录页地址，渲染成二维码用手机扫，在客户端/网页里确认
  3. GET  /v2/plugin/auth/token?state=...     → 未扫时 code=11217，成功后给 accessToken
  4. GET  /v2/plugin/login/account?state=...  → uid / nickname

state 只在内存里存 5 分钟（与上游一致）：二维码是一次性的，进程重启就作废，
本来就该重新发起。**不落盘**——它没有任何复用价值，落盘反而多一份可泄露的东西。
"""
from __future__ import annotations

import time

import segno

from . import db, runner
from .providers import registry
from .providers.base import ProviderError
from .security import encrypt_json

STATE_TTL = 300  # 秒，与上游 state 有效期一致

# state -> 发起时刻（进程内）
_states: dict[str, float] = {}


def qr_svg(url: str, scale: int = 4) -> str:
    """把授权链接画成可直接嵌进页面的内联 SVG（segno 纯 Python，不依赖 Pillow）。

    显式给黑白两色：默认是透明的，暗色主题下黑模块会糊成一片，扫不出来。
    """
    try:
        return segno.make(url, error="m").svg_inline(
            scale=scale, border=2, dark="#000000", light="#ffffff"
        )
    except Exception:  # noqa: BLE001
        return ""


async def start() -> dict:
    """发起一次扫码登录。返回 {state, authUrl, qr}（qr 为内联 SVG）。"""
    provider = registry.get("workbuddy")
    info = await provider.start_login()
    state = info["state"]
    _states[state] = time.time()
    # 顺手清掉过期的，避免内存里积压（同时最多也就几条）
    now = time.time()
    for key in [k for k, ts in _states.items() if now - ts > STATE_TTL]:
        _states.pop(key, None)
    return {"state": state, "authUrl": info["authUrl"], "qr": qr_svg(info["authUrl"])}


async def poll(state: str, name: str = "") -> dict:
    """轮询扫码结果；成功后落库并校验一次（顺带把余额/连签填好）。"""
    created = _states.get(state)
    if created is None:
        return {"status": "invalid", "message": "二维码已失效，请重新获取"}
    if time.time() - created > STATE_TTL:
        _states.pop(state, None)
        return {"status": "expired", "message": "二维码已过期，请重新获取"}

    try:
        result = await registry.get("workbuddy").poll_login(state)
    except ProviderError as exc:
        return {"status": "error", "message": exc.message}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}

    if result.get("status") != "ready":
        return result

    uid = result["uid"]
    cred = {
        "accessToken": result["accessToken"],
        "refreshToken": result.get("refreshToken", ""),
        "uid": uid,
        "expire_at": int(result.get("expire_at") or 0),
    }
    if result.get("domain"):
        cred["domain"] = result["domain"]

    label = (name or result.get("nickname") or "").strip() or "WorkBuddy 账号"
    existing = db.query_one(
        "SELECT * FROM accounts WHERE platform='workbuddy' AND uid=?", (uid,)
    )
    if existing:
        db.execute(
            "UPDATE accounts SET cred_enc=?, token_expire_at=?, enabled=1 WHERE id=?",
            (encrypt_json(cred), int(cred["expire_at"] or 0), existing["id"]),
        )
        account_id, action = existing["id"], "已更新"
    else:
        account_id = db.execute(
            """INSERT INTO accounts(platform, name, uid, cred_enc, token_expire_at, status, created_at)
               VALUES(?,?,?,?,?,?,?)""",
            ("workbuddy", label[:40], uid, encrypt_json(cred), int(cred["expire_at"] or 0),
             "unknown", db.now()),
        )
        action = "已添加"

    # 落盘成功后才丢弃 state（与上游同口径：不因为后面一步失败就把二维码判成失效）
    _states.pop(state, None)

    probe = await runner.probe_account(account_id)
    info = probe.get("info") or {}
    return {
        "status": "ready",
        "action": action,
        "account_id": account_id,
        "name": label,
        "message": probe.get("message", ""),
        "streak_days": info.get("streak_days"),
        "credit_balance": info.get("credit_balance"),
    }
