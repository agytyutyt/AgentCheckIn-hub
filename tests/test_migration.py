"""升级路径回归测试：老库必须能平滑升级到当前 schema。

为什么单独测：2026-10-30 踩过一次真实事故 —— `_migrate()` 把「补列」和「数据回填」
混在同一个循环里，回填语句 UPDATE 了「排在后面、还没添加」的列，老库升级直接：

    sqlite3.OperationalError: no such column: realm

服务端因为早已是新版（列都在，added=False 不会触发那条 UPDATE）完全没暴露，
本地老库必崩。这类 bug 只有「拿历史 schema 升级」才测得出。

造历史库的方式：**取当前 SCHEMA 再 DROP 掉后来新增的列**，比手写历史 DDL 更真实
（手写过一次就因为漏了 runs.biz_date 而误报，见 git 历史）。DROP COLUMN 需要
SQLite ≥ 3.35，Python 3.11+ 自带的都满足。

用法：python tests/test_migration.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 各年代「还没有」的列（按功能上线顺序倒推）
SNAPSHOTS: dict[str, dict[str, list[str]]] = {
    "极老（无任何 credits / realm 列）": {
        "accounts": ["realm", "credit_expiry_amount", "credit_expires_at",
                     "credit_total_at", "credit_total"],
        "runs": ["credit_snapshot"],
    },
    "中期（有 credit_total，无 expires / realm）": {
        "accounts": ["realm", "credit_expiry_amount", "credit_expires_at"],
        "runs": ["credit_snapshot"],
    },
    "近期（有 4 个 credit 列，无 realm）": {
        "accounts": ["realm"],
        "runs": ["credit_snapshot"],
    },
}

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    results.append((bool(ok), label))
    print(("  PASS  " if ok else "  FAIL  ") + label)


def current_schema() -> str:
    """从子进程读 SCHEMA，避免在测试进程里创建 data 目录。"""
    proc = subprocess.run(
        [sys.executable, "-c", "import sys; from app.db import SCHEMA; sys.stdout.write(SCHEMA)"],
        cwd=str(ROOT), env=dict(os.environ, PYTHONPATH=str(ROOT)),
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise SystemExit("读取 SCHEMA 失败：" + proc.stderr[-500:])
    return proc.stdout


def make_old_db(path: Path, schema: str, drops: dict[str, list[str]]) -> None:
    """当前 schema 建库 → DROP 掉后来新增的列 → 灌一条老口径数据 + 旧种子任务。"""
    conn = sqlite3.connect(path)
    conn.executescript(schema)
    for table, cols in drops.items():
        for col in cols:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
    cols_now = [r[1] for r in conn.execute("PRAGMA table_info(accounts)")]
    row = {
        "platform": "qoder",
        "name": "老 Qoder",
        "uid": "old-uid",
        "cred_enc": "x",
        "token_expire_at": 0,
        "status": "ok",
        "last_error": "",
        "credit_total": 454,      # 老口径：签到活动累计（不是余额）
        "credit_total_at": 1,
        "enabled": 1,
        "created_at": "2026-10-01T00:00:00",
    }
    keys = [c for c in cols_now if c in row]
    conn.execute(
        f"INSERT INTO accounts ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
        [row[k] for k in keys],
    )
    # 旧版种子的 Qoder 任务行：验证任务语义迁移（签到挪活动窗口 / Credits 停用）
    conn.execute(
        "INSERT INTO task_defs(platform, task_key, name, cron, window_start, window_end,"
        " max_attempts, enabled, sort) VALUES"
        " ('qoder','credit','Qoder 每日 Credits','5,15,25,35,45,55 10-11 * * *','10:00','12:00',3,1,30)"
    )
    conn.execute(
        "INSERT INTO task_defs(platform, task_key, name, cron, window_start, window_end,"
        " max_attempts, enabled, sort) VALUES"
        " ('qoder','checkin','Qoder 每日签到','35 8 * * *','00:05','23:30',3,1,25)"
    )
    # 今天的一条余额上涨流水 + 对应账号（余额 600）：迁移要据此还原开盘价 500
    # 日志形态必须与 credits.py 里的一致，含字面量「+」
    flow = dict(row)
    flow.update({"name": "流水账号", "uid": "flow-uid", "credit_total": 600})
    conn.execute(
        f"INSERT INTO accounts ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
        [flow[k] for k in keys],
    )
    conn.execute(
        "INSERT INTO run_logs(run_id, ts, level, msg) VALUES (0, ?, 'INFO', ?)",
        (
            int(time.time()) - 60,
            "积分余额 +100（500 → 600）· 流水账号",
        ),
    )
    conn.commit()
    conn.close()


def run_init(data_dir: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, CHECKIN_DATA_DIR=str(data_dir), PYTHONPATH=str(ROOT))
    return subprocess.run(
        [sys.executable, "-c", "from app import db; db.init_db()"],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
    )


def main() -> int:
    schema = current_schema()
    tmp_root = Path(tempfile.mkdtemp(prefix="ch-migrate-"))
    try:
        for label, drops in SNAPSHOTS.items():
            print(f"\n── {label}")
            data_dir = tmp_root / label.split("（")[0]
            data_dir.mkdir(parents=True, exist_ok=True)
            db_path = data_dir / "checkin.db"
            make_old_db(db_path, schema, drops)
            had_credit_expires = "credit_expires_at" not in drops["accounts"]

            proc = run_init(data_dir)
            tail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "ok"
            check(proc.returncode == 0, f"init_db 不报错（{tail}）")
            if proc.returncode != 0:
                continue

            conn = sqlite3.connect(db_path)
            cols_now = {r[1] for r in conn.execute("PRAGMA table_info(accounts)")}
            for col in ("credit_total", "credit_total_at", "credit_expires_at",
                        "credit_expiry_amount", "realm"):
                check(col in cols_now, f"补出列 accounts.{col}")
            runs_cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
            check("credit_snapshot" in runs_cols, "补出列 runs.credit_snapshot")

            row = conn.execute(
                "SELECT realm, credit_total FROM accounts WHERE platform='qoder'"
                " AND name='老 Qoder'"
            ).fetchone()
            check(row is not None and row[0] == "cn",
                  f"Qoder 账号回填 realm=cn（实际 {row and row[0]}）")
            if had_credit_expires:
                check(row[1] == 454, f"已有 credit_expires_at 时不清零（实际 {row[1]}）")
            else:
                check(row[1] == 0, f"老口径 credit_total 已清零（实际 {row[1]}）")

            tasks = conn.execute("SELECT COUNT(*) FROM task_defs").fetchone()[0]
            check(tasks >= 3, f"任务种子已补齐（{tasks} 条）")
            trow = conn.execute(
                "SELECT name, cron, window_start FROM task_defs"
                " WHERE platform='qoder' AND task_key='checkin'"
            ).fetchone()
            check(
                bool(trow) and "领 100 积分" in trow[0] and trow[1].startswith("5,15")
                and trow[2] == "10:00",
                f"签到任务迁移到活动窗口（{trow}）",
            )
            crow = conn.execute(
                "SELECT COUNT(*) FROM task_defs WHERE platform='qoder' AND task_key='credit'"
            ).fetchone()
            check(crow[0] == 0, f"重复的每日 Credits 任务已删除（剩 {crow[0]} 行）")

            # 当日开盘余额：必须从「积分余额 +100（500 → 600）」流水还原成 500，
            # 否则「今日新增」会算成 0（面板任务之外的增减会丢失）。
            # 注意：最老的那档老库升级时会把 credit_total 清零（老口径是累计值），
            # 余额未知 ⇒ 开盘价只能是 0，这里按档位分别断言。
            opened = conn.execute(
                "SELECT credit_open_balance, credit_open_date FROM accounts"
                " WHERE name='流水账号'"
            ).fetchone()
            expect_open = 500 if had_credit_expires else 0
            check(
                opened is not None and opened[0] == expect_open,
                f"用余额流水还原当日开盘价（实际 {opened and opened[0]}，期望 {expect_open}）",
            )
            check(
                bool(opened and opened[1]),
                f"记录开盘日期（实际 {opened and opened[1]}）",
            )
            conn.close()

            proc2 = run_init(data_dir)
            check(proc2.returncode == 0, "重复 init_db 幂等")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    passed = sum(1 for ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
