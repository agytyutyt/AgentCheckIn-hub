"""Qoder 设备码登录：面板出授权链接 → 用户浏览器里手机号+验证码登录 → 面板轮询建号。

与 WorkBuddy 扫码（authflow）同构，差异在于授权形态：Qoder 的官方授权页
（qoder.com/device/selectAccounts）就是手机号 + 短信验证码登录页（阿里云风控在
官方页完成），所以面板侧只需要把链接给用户（页面里同时渲染成二维码，手机扫开
一样能输手机号），然后轮询 deviceToken/poll 等 dt- 令牌。

会话只存内存、5 分钟有效：授权是短时动作，进程重启重来即可，落盘毫无价值。
"""
from __future__ import annotations

import time
import uuid

from . import db, runner
from .authflow import qr_svg
from .providers import registry
from .providers.base import ProviderError
from .security import encrypt_json

SESSION_TTL = 300  # 授权链接 5 分钟有效（与 CreditDaddy / qodercli 一致）

# session_id -> 会话
_sessions: dict[str, dict] = {}


async def start(realm: str) -> dict:
    """发起一次 Qoder 登录。返回 {session, url, qr, realm}。"""
    provider = registry.get("qoder")
    info = await provider.start_login(realm)
    sid = uuid.uuid4().hex
    now = time.time()
    _sessions[sid] = {
        "realm": realm,
        "nonce": info["nonce"],
        "verifier": info["verifier"],
        "created": now,
    }
    for key in [k for k, s in _sessions.items() if now - s["created"] > SESSION_TTL]:
        _sessions.pop(key, None)
    return {"session": sid, "url": info["url"], "qr": qr_svg(info["url"]), "realm": realm}


async def poll(session_id: str) -> dict:
    """轮询登录结果；ready 时落库并校验一次（顺带把余额填好）。"""
    session = _sessions.get(session_id)
    if session is None:
        return {"status": "invalid", "message": "登录会话不存在或已过期，请重新发起"}
    if time.time() - session["created"] > SESSION_TTL:
        _sessions.pop(session_id, None)
        return {"status": "expired", "message": "授权链接已过期，请重新发起"}

    provider = registry.get("qoder")
    try:
        result = await provider.poll_login(session["realm"], session["nonce"], session["verifier"])
    except ProviderError as exc:
        if exc.status == "retryable":
            return {"status": "error", "message": exc.message}
        _sessions.pop(session_id, None)
        return {"status": "error", "message": exc.message}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}

    if result.get("status") != "ready":
        return result

    realm = session["realm"]
    cred = {
        "accessToken": result["accessToken"],
        "refreshToken": result.get("refreshToken", ""),
        "uid": result["uid"],
        "expire_at": int(result.get("expire_at") or 0),
        "device": {"clientType": "10"},
        "realm": realm,
        "source": "device",
    }
    label = (result.get("nickname") or result.get("email") or "").strip() or "Qoder 账号"
    existing = db.query_one(
        "SELECT * FROM accounts WHERE platform='qoder' AND uid=? AND COALESCE(NULLIF(realm,''),'cn')=?",
        (result["uid"], realm),
    )
    if existing:
        db.execute(
            "UPDATE accounts SET cred_enc=?, token_expire_at=?, enabled=1, realm=? WHERE id=?",
            (encrypt_json(cred), int(cred["expire_at"] or 0), realm, existing["id"]),
        )
        account_id, action = existing["id"], "已更新"
    else:
        account_id = db.execute(
            """INSERT INTO accounts(platform, name, uid, cred_enc, token_expire_at, status, realm, created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            ("qoder", label[:40], result["uid"], encrypt_json(cred),
             int(cred["expire_at"] or 0), "unknown", realm, db.now()),
        )
        action = "已添加"

    # 落盘成功后才丢会话（与 WorkBuddy 扫码同口径）
    _sessions.pop(session_id, None)

    probe = await runner.probe_account(account_id)
    info = probe.get("info") or {}
    return {
        "status": "ready",
        "action": action,
        "account_id": account_id,
        "name": label,
        "realm": realm,
        "message": probe.get("message", ""),
        "credit_balance": info.get("credit_balance"),
        "campaigns": info.get("campaigns"),
    }
