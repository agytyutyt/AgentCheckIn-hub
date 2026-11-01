"""本地渲染自检：用临时数据目录灌几条演示记录，逐页渲染并截图用。"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="checkinhub-ui-"))
shutil.copy(ROOT / "data" / "checkin.db", TMP / "checkin.db")
shutil.copy(ROOT / "data" / "master.key", TMP / "master.key")
os.environ["CHECKIN_DATA_DIR"] = str(TMP)
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402

db.init_db()

wb = db.query_one("SELECT * FROM accounts WHERE platform='workbuddy'")
qd = db.query_one("SELECT * FROM accounts WHERE platform='qoder'")
now = db.now()


def seed(task_id, acc, status, msg, delta=0, snap=0, attempt=1, hours_ago=0.0, detail="{}"):
    ts = int(now - hours_ago * 3600)
    rid = db.execute(
        """INSERT INTO runs(task_def_id, account_id, biz_date, attempt, status, message, delta_credit,
                            credit_snapshot, started_at, finished_at, duration_ms, detail, trigger)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (task_id, acc, db.today_str(), attempt, status, msg, delta, snap, ts, ts + 2, 812, detail, "cron"),
    )
    db.log(rid, f"开始执行（第 {attempt} 次）", "DEBUG")
    db.log(rid, f"结束：{status} · {msg}", "ERROR" if status in ("failed", "need_login") else "INFO")
    return rid


seed(1, wb["id"], "skipped", "今日已签到（今日 +100，连续 8 天，累计 800 积分），无需重复执行", snap=100, hours_ago=9.5)
seed(2, wb["id"], "skipped", "宠物空闲，但今日派出次数已达上限", hours_ago=0.3)
seed(3, qd["id"], "success", "领取 1 项活动，+100 Credits", delta=100, snap=100, hours_ago=8.2)
seed(2, wb["id"], "failed", "旅行状态查询失败 code=500 msg=upstream exploded", attempt=2, hours_ago=1.1)

db.execute(
    """INSERT INTO daily_stats(biz_date, account_id, task_key, credit) VALUES(?,?,?,?)
       ON CONFLICT(biz_date, account_id, task_key) DO UPDATE SET credit=excluded.credit""",
    (db.today_str(), wb["id"], "checkin", 100),
)
db.execute(
    """INSERT INTO daily_stats(biz_date, account_id, task_key, credit) VALUES(?,?,?,?)
       ON CONFLICT(biz_date, account_id, task_key) DO UPDATE SET credit=excluded.credit""",
    (db.today_str(), qd["id"], "credit", 100),
)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

ok = True
with TestClient(app) as c:
    c.post("/login", data={"username": "admin", "password": "admin123"})
    for path in ("/dashboard", "/accounts", "/tasks", "/runs", "/runs/1", "/logs", "/settings"):
        r = c.get(path)
        flag = "OK " if r.status_code == 200 else "ERR"
        if r.status_code != 200:
            ok = False
        print(f"  {flag} {path} -> {r.status_code} ({len(r.text)} bytes)")

print("\n临时数据目录：", TMP)
print("自检结果：", "全部通过" if ok else "有失败")
raise SystemExit(0 if ok else 1)
