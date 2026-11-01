"""WorkBuddy 凭证导入器 —— 一键从本机客户端导出明文令牌。

最简用法（双击 import-account.bat 等价）：

    python tools/import_workbuddy.py --save     # 读取 + 解密 + 直接写入面板，零复制粘贴
    python tools/import_workbuddy.py            # 只打印凭证 JSON（给云端面板用）
    python tools/import_workbuddy.py --token eyJ...        # 手动粘贴令牌
    python tools/import_workbuddy.py --scan capture.har    # 从抓包文件里提取

5.6+ 客户端把登录态加密成 `$wbEncrypted`（AES-256-GCM）。本工具不逆向算法，
而是以 ELECTRON_RUN_AS_NODE 模式启动本机 WorkBuddy.exe，让它自己交出
`atRestSecretKey`（仅在本进程 stdout 中传递，不落盘），再据此解密。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AAD_MAGIC = b"WB-AAD\x00\x01"

# 让客户端（Electron）以纯 Node 身份运行，通过内部绑定取出落盘密钥。
# 成功时 stdout 输出 base64(sha256(atRestSecretKey))，失败退出码 2。
KEY_JS = r"""
try {
  const c = require('crypto');
  const p = JSON.parse(process._linkedBinding('electron_browser_workbuddy_storage').loggerGet());
  if (p.version !== 1 || typeof p.atRestSecretKey !== 'string') process.exit(2);
  const b = Buffer.from(p.atRestSecretKey, 'base64');
  if (b.length !== 32 || b.toString('base64') !== p.atRestSecretKey || b.every(x => x === 0)) process.exit(2);
  process.stdout.write(c.createHash('sha256').update(p.atRestSecretKey, 'utf8').digest('base64'));
} catch (e) { process.exit(2); }
"""

JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}")


# ------------------------------------------------------------------ 定位
def candidate_paths() -> list[Path]:
    home = Path.home()
    system = platform.system()
    items: list[Path] = []
    if system == "Windows":
        for env_key in ("LOCALAPPDATA", "APPDATA"):
            base = os.environ.get(env_key)
            if base:
                items.append(Path(base) / "CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info")
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
        items.append(local / "CodeBuddyExtension/Data/Public/auth")
    elif system == "Darwin":
        items.append(
            home
            / "Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info"
        )
    else:
        items.append(home / ".config/CodeBuddyExtension/Data/Public/auth")
        items.append(home / ".local/share/CodeBuddyExtension/Data/Public/auth")
    return items


def find_info() -> Path | None:
    for p in candidate_paths():
        if p.is_file():
            return p
        if p.is_dir():
            for f in sorted(p.glob("*.info")):
                if f.name.endswith(".info"):
                    return f
    return None


def find_client_exe() -> Path | None:
    override = os.environ.get("WORKBUDDY_CLIENT_EXE", "").strip()
    if override and Path(override).exists():
        return Path(override)

    if platform.system() == "Darwin":
        mac = Path("/Applications/WorkBuddy.app/Contents/MacOS/WorkBuddy")
        if mac.exists():
            return mac

    names = ["WorkBuddy.exe"] if platform.system() == "Windows" else ["WorkBuddy"]
    roots: list[Path] = []
    for env_key in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env_key)
        if base:
            roots += [Path(base) / "Programs" / "WorkBuddy", Path(base) / "WorkBuddy"]
    for drive in "CDEFG":
        roots.append(Path(f"{drive}:/WorkBuddy"))

    for root in roots:
        for name in names:
            exe = root / name
            if exe.exists():
                return exe
    return None


# ------------------------------------------------------------------ 取密钥
_key_cache: bytes | None = None


def load_official_key() -> bytes | None:
    """让客户端自己交出密钥；失败返回 None。"""
    global _key_cache
    if _key_cache is not None:
        return _key_cache

    exe = find_client_exe()
    if not exe:
        return None

    env = os.environ.copy()
    env["ELECTRON_RUN_AS_NODE"] = "1"
    env.pop("NODE_OPTIONS", None)
    flags = 0x08000000 if platform.system() == "Windows" else 0  # CREATE_NO_WINDOW
    try:
        proc = subprocess.run(
            [str(exe), "-e", KEY_JS],
            capture_output=True,
            timeout=25,
            env=env,
            creationflags=flags,
        )
    except Exception:
        return None

    if proc.returncode != 0:
        return None
    try:
        key = base64.b64decode(proc.stdout.strip(), validate=True)
    except Exception:
        return None
    if len(key) != 32:
        return None
    _key_cache = key
    return key


# ------------------------------------------------------------------ 解密
def _b64d(s: str) -> bytes:
    return base64.b64decode(s + "=" * (-len(s) % 4))


def _aad(key_id: str) -> bytes:
    """客户端信封的 AAD 结构（suite=sym-v1 / field 域）。"""
    aad = AAD_MAGIC
    for value in (b"WBEV1", b"sym-v1"):
        aad += len(value).to_bytes(4, "big") + value
    aad += (1).to_bytes(4, "big")
    aad += len(key_id).to_bytes(4, "big") + key_id.encode()
    aad += b"\x02\x00\x00"
    return aad


def decrypt_envelope(envelope: str, key: bytes) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    env = json.loads(_b64d(envelope).decode())
    if env.get("suite") not in (1, "1", None):
        raise ValueError(f"不支持的加密套件 suite={env.get('suite')}")

    key_id = hashlib.sha256(key).hexdigest()[:16]
    if env.get("keyId") and env["keyId"] != key_id:
        raise ValueError(
            f"密钥与登录态不匹配（信封 keyId={env['keyId']}，本机推导 {key_id}）。"
            "通常是客户端版本已更新，请升级本工具。"
        )

    nonce = _b64d(env["nonce"])
    tag = _b64d(env["authTag"])
    ct = _b64d(env["ciphertext"])

    layouts = [
        _aad(key_id),
        b"WB-AAD\x00" + bytes([2]) + key_id.encode() + nonce,
        b"WB-AAD\x00" + bytes([1]) + key_id.encode() + nonce,
    ]
    for aad in layouts:
        try:
            return AESGCM(key).decrypt(nonce, ct + tag, aad).decode("utf-8", "ignore")
        except Exception:
            continue
    raise ValueError("解密失败：AAD 布局与当前客户端不一致（通常是客户端升级了）")


# ------------------------------------------------------------------ 扫描
def _jwt_payload(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part).decode("utf-8", "ignore"))
    except Exception:
        return {}


def scan_tokens(text: str) -> list[tuple[str, dict]]:
    found: dict[str, dict] = {}
    for match in JWT_RE.finditer(text):
        token = match.group(0).rstrip(".,\"'")
        payload = _jwt_payload(token)
        if payload.get("sub"):
            found[token] = payload
    return sorted(found.items(), key=lambda kv: int(kv[1].get("exp") or 0), reverse=True)


def mask(token: str) -> str:
    return f"{token[:12]}…{token[-6:]}" if len(token) > 24 else token[:4] + "…"


# ------------------------------------------------------------------ 规整
def normalize(session: dict, key: bytes | None) -> dict:
    auth = session.get("auth") or session
    raw_token = auth.get("accessToken") or auth.get("token") or ""
    token = raw_token

    if isinstance(raw_token, dict) or str(raw_token).startswith("$wbEncrypted"):
        if key is None:
            raise SystemExit(
                "该登录态是 5.6+ 加密格式，但未能从客户端取得密钥。\n"
                "请确认：① 已安装并登录 WorkBuddy 桌面端；② 工具与客户端在同一台机器上。\n"
                "仍失败时可用 --token 手动粘贴，或 --scan 从抓包文件提取。"
            )
        envelope = (
            raw_token.get("envelope") if isinstance(raw_token, dict) else auth.get("envelope", "")
        )
        token = decrypt_envelope(envelope, key)

    def plain(value):
        if isinstance(value, dict) and value.get("envelope") and key is not None:
            try:
                return decrypt_envelope(value["envelope"], key)
            except Exception:
                return ""
        return value if isinstance(value, str) else ""

    nick = plain(auth.get("nickname")) or plain((session.get("account") or {}).get("nickname"))

    out = {
        "accessToken": token,
        "uid": auth.get("uid") or (session.get("account") or {}).get("uid") or "",
        "refreshToken": plain(auth.get("refreshToken")),
        "nickname": nick,
    }
    exp = auth.get("expiresAt") or auth.get("expire_at") or 0
    if exp:
        out["expire_at"] = int(int(exp) / 1000) if int(exp) > 10_000_000_000 else int(exp)
    return {k: v for k, v in out.items() if v}


# ------------------------------------------------------------------ 写入面板
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
                f"   提示：用项目虚拟环境跑 —— \"{exe}\" tools\\import_workbuddy.py --save",
                file=sys.stderr,
            )
        return 4

    db.init_db()
    payload = {
        "accessToken": cred["accessToken"],
        "uid": cred.get("uid", ""),
        "refreshToken": cred.get("refreshToken", ""),
        "expire_at": cred.get("expire_at", 0),
    }
    blob = security.encrypt_json(payload)

    uid = cred.get("uid") or ""
    existing = None
    if uid:
        existing = db.query_one(
            "SELECT * FROM accounts WHERE platform='workbuddy' AND uid=?", (uid,)
        )
    if existing:
        db.execute(
            "UPDATE accounts SET cred_enc=?, token_expire_at=?, enabled=1 WHERE id=?",
            (blob, int(payload["expire_at"] or 0), existing["id"]),
        )
        acc_id = existing["id"]
        action = "已更新"
    else:
        name = (cred.get("nickname") or "").strip()
        if len(name) < 2:  # 昵称可能为空或只有一个标点
            name = "WorkBuddy 主账号"
        acc_id = db.execute(
            """INSERT INTO accounts(platform, name, uid, cred_enc, token_expire_at, status, created_at)
               VALUES(?,?,?,?,?,?,?)""",
            ("workbuddy", name, uid, blob, int(payload["expire_at"] or 0), "unknown", db.now()),
        )
        action = "已添加"

    import asyncio

    result = asyncio.run(runner.probe_account(acc_id))
    if result.get("ok"):
        info = result.get("info") or {}
        label = existing["name"] if existing else name
        print(f"✅ {action}账号「{label}」，连接正常（连续签到 {info.get('streak_days', 0)} 天）")
        print("   刷新面板页面即可看到该账号，无需重启服务。")
        return 0
    print(f"⚠️  {action}账号，但校验未通过：{result.get('message')}")
    print("   账号已保存，可在面板「账号」页点「测试」重试。")
    return 0


# ------------------------------------------------------------------ 入口
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="导出 / 导入 WorkBuddy 凭证")
    ap.add_argument("--token", help="直接提供 accessToken")
    ap.add_argument("--refresh-token", default="", help="可选 refreshToken")
    ap.add_argument("--uid", default="", help="可选 uid，默认从 JWT 解析")
    ap.add_argument("--scan", help="从抓包文件（HAR / 文本）提取令牌，- 表示读标准输入")
    ap.add_argument("--pick", type=int, default=0, help="--scan 时的候选序号")
    ap.add_argument("--file", help="指定 .info 文件路径")
    ap.add_argument("--out", help="写入 JSON 文件")
    ap.add_argument("--save", action="store_true", help="直接写入本机面板数据库（推荐）")
    ap.add_argument("--key", default="", help="手动提供 32 字节密钥（一般不需要）")
    args = ap.parse_args(argv)

    if args.scan:
        raw_text = (
            sys.stdin.read()
            if args.scan == "-"
            else Path(args.scan).read_text(encoding="utf-8", errors="ignore")
        )
        candidates = scan_tokens(raw_text)
        if not candidates:
            print("未在文件中找到有效 JWT（需要带 sub 声明）", file=sys.stderr)
            return 5
        for i, (tok, payload) in enumerate(candidates[:10]):
            exp = payload.get("exp")
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(exp))) if exp else "未知"
            print(f"  #{i}  sub={payload.get('sub')}  到期={when}  {mask(tok)}", file=sys.stderr)
        if len(candidates) > 10:
            print(f"  ... 共 {len(candidates)} 个候选", file=sys.stderr)
        token, payload = candidates[max(0, min(args.pick, len(candidates) - 1))]
        cred = {"accessToken": token, "uid": payload.get("sub", "")}
        if payload.get("exp"):
            cred["expire_at"] = int(payload["exp"])
        return _finish(cred, args)

    if args.token:
        cred = {"accessToken": args.token.strip()}
        if args.uid:
            cred["uid"] = args.uid
        if args.refresh_token:
            cred["refreshToken"] = args.refresh_token
        return _finish(cred, args)

    path = Path(args.file) if args.file else find_info()
    if not path or not path.exists():
        print("未找到 WorkBuddy 登录态文件。请先登录桌面客户端，或用 --token 手动粘贴。", file=sys.stderr)
        for p in candidate_paths():
            print("  期望路径：", p, file=sys.stderr)
        return 2

    session = json.loads(path.read_text(encoding="utf-8"))
    key = None
    if args.key:
        raw = args.key.strip()
        try:
            key = (
                bytes.fromhex(raw)
                if all(c in "0123456789abcdefABCDEF" for c in raw)
                else base64.b64decode(raw)
            )
        except Exception:
            key = None
    if key is None:
        print("正在向 WorkBuddy 客户端索取解密密钥…", file=sys.stderr)
        key = load_official_key()
        if key is None:
            print("未能从客户端取得密钥（未安装 / 未登录 / 版本不匹配）。", file=sys.stderr)

    cred = normalize(session, key)
    print(f"# 来源：{path}", file=sys.stderr)
    return _finish(cred, args)


def _finish(cred: dict, args) -> int:
    if not cred.get("accessToken"):
        print("未解析出 accessToken", file=sys.stderr)
        return 3

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
