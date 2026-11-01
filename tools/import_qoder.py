"""Qoder 凭证导入器 —— 一键从本机客户端导出并写入面板。

最简用法（双击 import-account.bat 等价）：

    python tools/import_qoder.py --save     # 读取 + 解密 + 直接写入面板
    python tools/import_qoder.py            # 只打印凭证 JSON

Windows 上客户端把令牌存在：
    %APPDATA%\\com.qodercn.app.stable\\Local State   → os_crypt.encrypted_key（DPAPI 包裹）
    %APPDATA%\\com.qodercn.app.stable\\auth.v1.dat    → v10 + 12B nonce + 密文 + 16B tag

实测（2026-10）：campaigns 接口只校验 Bearer，不强制 Cosy-* 设备头，
因此本工具默认只填 clientType，其余留空即可。
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
import sys
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIRS = ["com.qodercn.app.stable", "com.qoder.app.stable", "Qoder"]

# 客户端数据目录 → 区域。官方两版并存时目录名不同，正好用来判定账号属于哪个池：
#   国内版 com.qodercn.app.stable ／ 国际版 com.qoder.app.stable
# 「Qoder」是更早版本的目录名，那时只有国内版。
REALM_DIRS = {
    "com.qodercn.app.stable": "cn",
    "com.qoder.app.stable": "intl",
    "Qoder": "cn",
}
REALM_NAMES = {"cn": "国内版", "intl": "国际版"}


def realm_for_dir(app_dir: Path | None) -> str:
    """该数据目录属于哪个区域。取不到时按国内版（历史默认）。"""
    if app_dir is None:
        return "cn"
    return REALM_DIRS.get(Path(app_dir).name, "cn")


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi_unprotect(data: bytes) -> bytes:
    buf_in = DATA_BLOB(
        len(data), ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_char))
    )
    buf_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(buf_in), None, None, None, None, 0, ctypes.byref(buf_out)
    ):
        raise OSError("CryptUnprotectData 失败（可能不是当前用户加密的数据）")
    try:
        return ctypes.string_at(buf_out.pbData, buf_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(buf_out.pbData)


def find_app_dir(realm: str | None = None) -> Path | None:
    """定位客户端数据目录。

    两版客户端可以并存（登录过国际版的机器上两个目录都在），所以 `realm`
    指定时必须按区域过滤，否则永远只会命中国内版那个。
    """
    appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    names = APP_DIRS
    if realm:
        names = [n for n in APP_DIRS if REALM_DIRS.get(n, "cn") == realm]
        if not names:
            return None
    for name in names:
        p = appdata / name
        if p.is_dir() and (p / "auth.v1.dat").exists():
            return p
    for name in names:
        p = appdata / name
        if p.is_dir():
            return p
    return None


def load_aes_key(app_dir: Path) -> bytes:
    state_file = app_dir / "Local State"
    if not state_file.exists():
        raise FileNotFoundError(f"未找到 {state_file}")
    state = json.loads(state_file.read_text(encoding="utf-8"))
    enc_b64 = (state.get("os_crypt") or {}).get("encrypted_key")
    if not enc_b64:
        raise ValueError("Local State 中没有 os_crypt.encrypted_key")
    raw = base64.b64decode(enc_b64)
    if raw[:5] == b"DPAPI":
        raw = raw[5:]
    return dpapi_unprotect(raw)


def decrypt_auth_blob(blob: bytes, key: bytes) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if blob[:3] != b"v10":
        raise ValueError(f"未识别的 auth.v1.dat 头：{blob[:3]!r}")
    nonce, body = blob[3:15], blob[15:]
    ct, tag = body[:-16], body[-16:]
    plain = AESGCM(key).decrypt(nonce, ct + tag, None)
    return json.loads(plain.decode("utf-8", "ignore"))


def deep_find(obj, names: tuple[str, ...], types: tuple = (str,)):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in names and isinstance(v, types) and v:
                return v
        for v in obj.values():
            found = deep_find(v, names, types)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = deep_find(v, names, types)
            if found:
                return found
    return None


def _to_epoch(value) -> int:
    """兼容毫秒时间戳、秒时间戳与 ISO8601 字符串。"""
    if isinstance(value, (int, float)):
        n = int(value)
        return n // 1000 if n > 10_000_000_000 else n
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


def build_cred(realm: str | None = None, app_dir: Path | None = None) -> dict:
    if app_dir is None:
        app_dir = find_app_dir(realm)
    if not app_dir:
        hint = (
            f"未找到{'国际版' if realm == 'intl' else '国内版'}客户端数据目录"
            f"（%APPDATA%\\{'com.qoder.app.stable' if realm == 'intl' else 'com.qodercn.app.stable'}）"
        )
        raise SystemExit(hint)
    auth_file = app_dir / "auth.v1.dat"
    if not auth_file.exists():
        raise SystemExit(f"未找到 {auth_file}，请先在该区域客户端登录")

    try:
        key = load_aes_key(app_dir)
        payload = decrypt_auth_blob(auth_file.read_bytes(), key)
    except Exception as exc:
        raise SystemExit(f"解密失败：{exc}")

    token = deep_find(payload, ("token", "accessToken", "access_token"))
    rt = deep_find(payload, ("refreshToken", "refresh_token"))
    uid = deep_find(payload, ("id", "uid", "userId")) or ""
    name = deep_find(payload, ("name", "nickname")) or ""
    if not token:
        raise SystemExit("未从 auth.v1.dat 解析出 token")

    cred = {"accessToken": token, "refreshToken": rt, "uid": uid, "nickname": name}
    exp = payload.get("refreshTokenExpiresAt") or payload.get("expiresAt")
    if exp:
        cred["expire_at"] = _to_epoch(exp)
    cred["device"] = {"clientType": "10"}
    # 区域跟账号走：显式 --realm 优先，否则按数据目录判定
    cred["realm"] = realm or realm_for_dir(app_dir)
    return {k: v for k, v in cred.items() if v}


def save_to_panel(cred: dict) -> int:
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    # 用系统 Python 直接跑本脚本时，面板依赖不在这个解释器里：先自愈（切到 .venv）
    from _boot import ensure_deps, venv_python

    ensure_deps("httpx", "fastapi")
    try:
        from app import db, runner, security
    except Exception as exc:
        print(f"无法加载面板模块：{exc}", file=sys.stderr)
        exe = venv_python()
        if exe:
            print(
                f"   提示：用项目虚拟环境跑 —— \"{exe}\" tools\\import_qoder.py --save",
                file=sys.stderr,
            )
        return 4

    db.init_db()
    realm = cred.get("realm") or "cn"
    payload = {
        "accessToken": cred["accessToken"],
        "refreshToken": cred.get("refreshToken", ""),
        "uid": cred.get("uid", ""),
        "expire_at": cred.get("expire_at", 0),
        "device": cred.get("device") or {"clientType": "10"},
        "realm": realm,
    }
    blob = security.encrypt_json(payload)

    uid = cred.get("uid") or ""
    existing = None
    if uid:
        existing = db.query_one("SELECT * FROM accounts WHERE platform='qoder' AND uid=?", (uid,))
    if not existing:
        # 只在「目标池里恰好只有一个账号」时才当成同一账号更新：两个池各有账号时
        # 不能拿国内版的记录去覆盖国际版的凭证（uid 不同，靠这个兜底会张冠李戴）。
        rows = db.query(
            "SELECT * FROM accounts WHERE platform='qoder' AND COALESCE(NULLIF(realm,''),'cn')=?",
            (realm,),
        )
        if len(rows) == 1:
            existing = rows[0]

    if existing:
        db.execute(
            "UPDATE accounts SET cred_enc=?, token_expire_at=?, enabled=1, realm=?,"
            " uid=COALESCE(NULLIF(?,''), uid) WHERE id=?",
            (blob, int(payload["expire_at"] or 0), realm, uid, existing["id"]),
        )
        acc_id = existing["id"]
        action = "已更新"
    else:
        name = cred.get("nickname") or "Qoder 主账号"
        acc_id = db.execute(
            """INSERT INTO accounts(platform, name, uid, cred_enc, token_expire_at, status, realm, created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            ("qoder", name, uid, blob, int(payload["expire_at"] or 0), "unknown", realm, db.now()),
        )
        action = "已添加"

    import asyncio

    result = asyncio.run(runner.probe_account(acc_id))
    if result.get("ok"):
        info = result.get("info") or {}
        label = cred.get("nickname") or uid or "Qoder"
        print(
            f"✅ {action}账号「{label}」（{REALM_NAMES.get(realm, realm)}），连接正常"
            f"（{info.get('campaigns', 0)} 个活动，{info.get('claimable', 0)} 个可领）"
        )
        return 0
    print(f"⚠️  {action}账号（{REALM_NAMES.get(realm, realm)}），但校验未通过：{result.get('message')}")
    print("   账号已保存，可在面板「账号」页点「测试」重试。")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="导出 / 导入 Qoder 凭证（支持国内版 / 国际版）")
    ap.add_argument("--token", default="", help="直接提供 accessToken")
    ap.add_argument("--refresh-token", default="", help="直接提供 refreshToken")
    ap.add_argument("--merge", help="与已有导出 JSON 合并")
    ap.add_argument("--device", help="设备头 JSON 文件（一般不需要）")
    ap.add_argument("--out", help="写入 JSON 文件")
    ap.add_argument("--save", action="store_true", help="直接写入本机面板数据库")
    ap.add_argument(
        "--realm",
        choices=["cn", "intl"],
        help="区域：cn=国内版（openapi.qoder.com.cn）/ intl=国际版（openapi.qoder.sh）；"
        "不填则按客户端数据目录自动判定",
    )
    ap.add_argument("--app-dir", help="指定客户端数据目录名或绝对路径（两版并存时用）")
    args = ap.parse_args(argv)

    app_dir = None
    if args.app_dir:
        app_dir = Path(args.app_dir)
        if not app_dir.is_dir():
            appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
            app_dir = appdata / args.app_dir
        if not app_dir.is_dir():
            raise SystemExit(f"数据目录不存在：{args.app_dir}")

    if args.token:
        cred = {"accessToken": args.token.strip(), "device": {"clientType": "10"}}
        if args.refresh_token:
            cred["refreshToken"] = args.refresh_token.strip()
        if args.realm:
            cred["realm"] = args.realm
        if args.merge and Path(args.merge).exists():
            cred.update(json.loads(Path(args.merge).read_text(encoding="utf-8")))
    else:
        cred = build_cred(args.realm, app_dir)
        if args.merge and Path(args.merge).exists():
            merged = json.loads(Path(args.merge).read_text(encoding="utf-8"))
            merged.update(cred)
            cred = merged
    if args.device and Path(args.device).exists():
        cred["device"] = json.loads(Path(args.device).read_text(encoding="utf-8"))

    realm = cred.get("realm") or "cn"
    if not args.save:
        print(
            f"# 区域：{REALM_NAMES.get(realm, realm)}"
            + ("" if cred.get("realm") else "（未指定，按国内版处理）"),
            file=sys.stderr,
        )

    if args.save:
        return save_to_panel(cred)

    text = json.dumps(cred, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"# 已写入 {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
