"""导出本机 Qoder 客户端的真实机器身份，并可推送到云端面板（--push）。

为什么需要：国际版每日 100 Credits 活动是**设备定向**的 —— 平台只发给账号真正
登录的那台机器。云端自己生成的组件身份拿不到活动（实测：云端 1 条无每日活动，
换成 PC 身份后同一条请求 2 条且 CLAIMABLE，并能从云端领取成功）。
把本机身份固定到面板后，云端就能替这台机器每天自动签到。

用法：
    # 只导出到 data/qoder-umid/identity-<realm>-<uid>.json（本机面板即生效）
    python tools/export_identity.py --realm intl

    # 导出并推送到云端面板（账号密码同面板登录；别把口令写进命令历史/脚本）
    python tools/export_identity.py --realm intl ^
        --push http://YOUR_SERVER_IP --user admin --password YOUR_PANEL_PASSWORD

通常不用直接敲这个命令：双击 tools\\sync_qoder_identity.bat 即可（口令放在
gitignore 掉的 sync_qoder_identity.local.bat 里，不进 git、也不进部署包）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools._boot  # noqa: E402,F401  自动切到带依赖的 venv

from app import qoderumid  # noqa: E402


def export(realm: str, uid: str) -> dict:
    exe = qoderumid._pick_runner(realm)
    if not exe:
        raise SystemExit(
            "找不到 runtime-info 组件。请在本机安装 Qoder 客户端后重试"
            "（或先用面板的「Qoder 登录」纳管账号）。"
        )
    print(f"组件: {exe}")
    return qoderumid._run(exe, uid)


def main() -> int:
    ap = argparse.ArgumentParser(description="导出/推送 Qoder 真实机器身份")
    ap.add_argument("--realm", default="intl", choices=["intl", "cn"])
    ap.add_argument("--uid", default="", help="账号 uid；不给则取该区域第一个账号")
    ap.add_argument("--push", default="", help="云端面板地址，如 http://1.2.3.4")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="")
    args = ap.parse_args()

    uid = args.uid
    if not uid:
        from app import db

        row = db.query_one(
            "SELECT uid FROM accounts WHERE platform='qoder' AND realm=? AND enabled=1",
            (args.realm,),
        )
        if not row:
            raise SystemExit(f"本地库里没有 {args.realm} 区域的 Qoder 账号，请用 --uid 指定")
        uid = row["uid"]

    ident = export(args.realm, uid)
    print(f"身份: machineToken={ident['machineToken'][:20]}… "
          f"code={ident['machineCode'][:10]}… type={ident['machineType'][:10]}…")

    path = qoderumid.save_pinned(args.realm, uid, ident)
    print(f"已写入: {path}")

    if args.push:
        import httpx

        url = args.push.rstrip("/")
        with httpx.Client(trust_env=False, timeout=60, follow_redirects=False) as c:
            c.post(f"{url}/login", data={"username": args.user, "password": args.password})
            r = c.post(f"{url}/api/qoder/identity",
                       json={"realm": args.realm, "uid": uid, "identity": ident})
            print(f"推送 {url}: HTTP {r.status_code} {r.text[:200]}")
            if r.status_code != 200:
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
