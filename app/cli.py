"""命令行入口：不依赖 Web 服务也能跑任务，适合被系统 cron 兜底调用。

    python -m app.cli list
    python -m app.cli run checkin
    python -m app.cli run all
    python -m app.cli probe
    python -m app.cli backup
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from datetime import datetime

from . import db, runner
from .config import DB_PATH, TZ


def _tasks(selector: str) -> list[dict]:
    rows = db.query("SELECT * FROM task_defs ORDER BY sort")
    if selector in ("all", "", "*"):
        return rows
    picked = [t for t in rows if t["task_key"] == selector or t["platform"] == selector]
    if not picked:
        print(f"未找到任务：{selector}，可用任务：{[t['task_key'] for t in rows]}", file=sys.stderr)
    return picked


async def _run(selector: str) -> int:
    failed = 0
    for task in _tasks(selector):
        results = await runner.run_task(task["id"], "cli")
        for r in results:
            print(f"[{task['name']}] {r['status']:<9} {r['message']}")
            if r["status"] in ("failed", "need_login", "retryable"):
                failed += 1
    return 1 if failed else 0


async def _probe() -> int:
    failed = 0
    for acc in db.query("SELECT * FROM accounts WHERE enabled=1"):
        res = await runner.probe_account(acc["id"])
        mark = "OK  " if res.get("ok") else "FAIL"
        print(f"[{mark}] {acc['platform']:<9} {acc['name']}: {res.get('message')}")
        if not res.get("ok"):
            failed += 1
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="CheckinHub CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="列出任务")
    p_run = sub.add_parser("run", help="执行任务")
    p_run.add_argument("selector", nargs="?", default="all", help="task_key / platform / all")
    sub.add_parser("probe", help="校验全部账号凭证")
    sub.add_parser("backup", help="备份数据库")
    args = ap.parse_args()

    db.init_db()

    if args.cmd == "list":
        for t in db.query("SELECT * FROM task_defs ORDER BY sort"):
            state = "启用" if t["enabled"] else "停用"
            print(f"{t['id']:>2}  {t['platform']:<9} {t['task_key']:<10} {t['cron']:<24} {state}  {t['name']}")
        return 0

    if args.cmd == "run":
        return asyncio.run(_run(args.selector))

    if args.cmd == "probe":
        return asyncio.run(_probe())

    if args.cmd == "backup":
        stamp = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
        dest = DB_PATH.with_name(f"checkin-{stamp}.db")
        shutil.copy2(DB_PATH, dest)
        print(f"已备份到 {dest}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
