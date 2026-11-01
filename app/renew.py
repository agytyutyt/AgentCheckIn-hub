"""令牌续期 / 保活：每小时巡检，剩余寿命不足阈值就刷新。

为什么要自己续：WorkBuddy 的 accessToken 是 JWT（社区反馈约 90 天），到期就得重新
登录；但只要还在有效期内的 refreshToken 就能换新，没必要惊动用户。Qoder 同理——
deviceToken 也会过期，过期后签到/余额全断。
口径参考 ithtelab/workbuddy-manager 的 server/services/renew.py 与
shuishuipingan/qoder2api-hub 的 qoder_tasks.run_keepalive：
  · 每小时巡检；剩余寿命低于阈值即刷新（WB 3 天 / Qoder 7 天）
  · `force=True` 无条件刷（对应「每 N 天全量换一遍」的诉求）
  · 串行刷新：上游对高频请求有风控，并发得不偿失
  · WorkBuddy：refreshToken 走 X-Refresh-Token 头（见 workbuddy.refresh_token）
  · Qoder：refresh_token 走 body（deviceToken/refresh，双区域同路径不同基址）
"""
from __future__ import annotations

import os
import time

from . import db
from .providers import registry
from .providers.base import ProviderError, jwt_exp
from .security import decrypt_json, encrypt_json

RENEW_BEFORE_SECONDS = int(os.environ.get("WB_RENEW_BEFORE_DAYS", "3")) * 86400
# Qoder 的 deviceToken 寿命更短，阈值放宽到 7 天（env 可调）
KEEPALIVE_SECONDS = int(os.environ.get("QODER_KEEPALIVE_DAYS", "7")) * 86400
CHECK_MINUTES = int(os.environ.get("WB_RENEW_CHECK_MIN", "60"))


def needs_renew(expires_at: int, now: int | None = None) -> bool:
    """WorkBuddy：是否进入续期窗口。解不出到期时间（<=0）时不臆测。"""
    if expires_at <= 0:
        return False
    now = int(time.time()) if now is None else now
    return expires_at - now <= RENEW_BEFORE_SECONDS


def needs_keepalive(expires_at: int, now: int | None = None) -> bool:
    """Qoder：是否进入保活窗口。"""
    if expires_at <= 0:
        return False
    now = int(time.time()) if now is None else now
    return expires_at - now <= KEEPALIVE_SECONDS


def _expires_of(cred: dict, fallback: int) -> int:
    try:
        exp = int(cred.get("expire_at") or 0)
    except (TypeError, ValueError):
        exp = 0
    return exp or int(fallback or 0) or jwt_exp(str(cred.get("accessToken") or ""))


async def refresh_one(account_id: int, *, force: bool = False) -> dict:
    """续期 / 保活一个账号。返回 {ok, skipped, message, expire_at, days, need_login?}。"""
    acc = db.query_one("SELECT * FROM accounts WHERE id=?", (account_id,))
    if not acc:
        return {"ok": False, "message": "账号不存在"}
    platform = acc["platform"]
    if platform not in ("workbuddy", "qoder"):
        return {"ok": False, "supported": False, "message": "该平台不支持令牌续期"}

    cred = decrypt_json(acc["cred_enc"])
    exp = _expires_of(cred, int(acc.get("token_expire_at") or 0))
    due = needs_renew(exp) if platform == "workbuddy" else needs_keepalive(exp)
    if not force and not due:
        days = max(0, (exp - db.now()) // 86400) if exp else None
        return {"ok": True, "skipped": True, "message": "令牌仍在有效期内，暂不续期",
                "expire_at": exp, "days": days}

    if not str(cred.get("refreshToken") or "").strip():
        return {"ok": False, "need_login": True,
                "message": "该账号没有 refreshToken，只能重新扫码登录"}

    try:
        provider = registry.get(platform)
        if platform == "workbuddy":
            merged = await provider.refresh_token(cred)
        else:
            merged = await provider.refresh_credentials(cred)
    except ProviderError as exc:
        if exc.status == "need_login":
            db.execute(
                "UPDATE accounts SET status='need_login', last_error=? WHERE id=?",
                (exc.message, account_id),
            )
        return {"ok": False, "message": exc.message,
                "need_login": exc.status == "need_login"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}

    new_exp = _expires_of(merged, exp)
    db.execute(
        """UPDATE accounts SET cred_enc=?, token_expire_at=?, status='ok', last_error=''
           WHERE id=?""",
        (encrypt_json(merged), new_exp, account_id),
    )
    days = max(0, (new_exp - db.now()) // 86400) if new_exp else None
    verb = "令牌已续期" if platform == "workbuddy" else "Token 已保活刷新"
    tail = f"，剩余 {days} 天" if days is not None else ""
    db.log(0, f"{verb} · {acc['name']}{tail}", "INFO")
    return {"ok": True, "skipped": False, "message": f"续期成功{tail}",
            "expire_at": new_exp, "days": days}


async def tick() -> dict:
    """巡检一遍所有启用的账号（串行，避免触发风控）。"""
    stats = {"checked": 0, "renewed": 0, "failed": 0, "skipped": 0}
    rows = db.query(
        "SELECT id FROM accounts WHERE platform IN ('workbuddy','qoder') AND enabled=1 ORDER BY id"
    )
    for row in rows:
        stats["checked"] += 1
        try:
            result = await refresh_one(row["id"])
        except Exception:  # noqa: BLE001
            stats["failed"] += 1
            continue
        if not result.get("ok"):
            stats["failed"] += 1
        elif result.get("skipped"):
            stats["skipped"] += 1
        else:
            stats["renewed"] += 1
    return stats
