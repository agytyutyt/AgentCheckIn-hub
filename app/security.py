"""口令哈希、会话签名、凭证字段加密、日志脱敏。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import MASTER_KEY, SESSION_TTL

_PBKDF2_ROUNDS = 200_000
_AAD = b"checkinhub-cred-v1"


# ---------------------------------------------------------------- 口令
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return "pbkdf2_sha256${}${}${}".format(
        _PBKDF2_ROUNDS,
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(rounds))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


# ---------------------------------------------------------------- 会话
def _sign_key() -> bytes:
    return hashlib.sha256(("session:" + MASTER_KEY).encode()).digest()


def sign_session(username: str) -> str:
    payload = {"u": username, "exp": int(time.time()) + SESSION_TTL}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    sig = hmac.new(_sign_key(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def parse_session(token: str) -> dict | None:
    try:
        raw, sig = token.rsplit(".", 1)
        expect = hmac.new(_sign_key(), raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expect):
            return None
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


# ---------------------------------------------------------------- 凭证加密
def _cred_key() -> bytes:
    return hashlib.sha256(("cred:" + MASTER_KEY).encode()).digest()


def encrypt_json(obj: Any) -> bytes:
    nonce = os.urandom(12)
    plain = json.dumps(obj, ensure_ascii=False).encode()
    ct = AESGCM(_cred_key()).encrypt(nonce, plain, _AAD)
    return nonce + ct


def decrypt_json(blob: bytes) -> dict:
    if not blob:
        return {}
    nonce, ct = blob[:12], blob[12:]
    plain = AESGCM(_cred_key()).decrypt(nonce, ct, _AAD)
    return json.loads(plain.decode())


# ---------------------------------------------------------------- 脱敏
def mask(value: str | None, keep: int = 6) -> str:
    """令牌脱敏：只留首尾，中间用省略号。"""
    if not value:
        return ""
    if len(value) <= keep * 2:
        return value[:2] + "…"
    return f"{value[:keep]}…{value[-keep:]}"


def scrub(text: str) -> str:
    """把疑似 JWT / 长令牌替换掉，防止写进日志与页面。"""
    if not text:
        return ""
    out = text
    # JWT: 三段 base64url，首段以 eyJ 开头
    parts = out.split(" ")
    for i, p in enumerate(parts):
        if p.startswith("eyJ") and p.count(".") == 2 and len(p) > 40:
            parts[i] = mask(p)
    out = " ".join(parts)
    # 兜底：任意 80+ 位的连续 base64url 串
    import re

    return re.sub(
        r"[A-Za-z0-9_\-]{80,}",
        lambda m: m.group(0)[:6] + "…" + m.group(0)[-6:],
        out,
    )
