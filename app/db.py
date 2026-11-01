"""SQLite 访问层。数据量极小（每天几十行），用标准库 + 单连接锁足够。"""
from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any, Iterable

from .config import DB_PATH

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    pw_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    name TEXT NOT NULL,
    uid TEXT DEFAULT '',
    cred_enc BLOB NOT NULL,
    token_expire_at INTEGER DEFAULT 0,
    status TEXT DEFAULT 'unknown',
    last_error TEXT DEFAULT '',
    last_ok_at INTEGER DEFAULT 0,
    credit_total INTEGER DEFAULT 0,
    credit_total_at INTEGER DEFAULT 0,
    credit_expires_at INTEGER DEFAULT 0,
    credit_expiry_amount INTEGER DEFAULT 0,
    realm TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS task_defs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    task_key TEXT NOT NULL,
    name TEXT NOT NULL,
    cron TEXT NOT NULL,
    window_start TEXT DEFAULT '00:00',
    window_end TEXT DEFAULT '23:59',
    max_attempts INTEGER DEFAULT 3,
    enabled INTEGER DEFAULT 1,
    sort INTEGER DEFAULT 0,
    UNIQUE(platform, task_key)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_def_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    biz_date TEXT NOT NULL,
    attempt INTEGER DEFAULT 1,
    status TEXT NOT NULL,
    message TEXT DEFAULT '',
    delta_credit INTEGER DEFAULT 0,
    credit_snapshot INTEGER DEFAULT 0,
    started_at INTEGER NOT NULL,
    finished_at INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT 0,
    detail TEXT DEFAULT '',
    trigger TEXT DEFAULT 'cron'
);
CREATE INDEX IF NOT EXISTS idx_runs_biz ON runs(biz_date, task_def_id, account_id);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);

CREATE TABLE IF NOT EXISTS run_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER DEFAULT 0,
    ts INTEGER NOT NULL,
    level TEXT DEFAULT 'INFO',
    msg TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON run_logs(ts DESC);

CREATE TABLE IF NOT EXISTS retry_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_def_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    biz_date TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    next_at INTEGER NOT NULL,
    status TEXT DEFAULT 'pending',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_retry_due ON retry_queue(status, next_at);

CREATE TABLE IF NOT EXISTS daily_stats (
    biz_date TEXT NOT NULL,
    account_id INTEGER NOT NULL,
    task_key TEXT NOT NULL,
    credit INTEGER DEFAULT 0,
    PRIMARY KEY (biz_date, account_id, task_key)
);
"""


def now() -> int:
    return int(time.time())


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.execute("PRAGMA foreign_keys=ON")
    return _conn


def init_db() -> None:
    with _lock:
        conn = get_conn()
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
        _seed_task_defs()


def _migrate(conn: sqlite3.Connection) -> None:
    """老库补列 + 数据回填。

    分两步走，**顺序不能混**：先把所有缺失的列补齐，再做 UPDATE 回填。
    反例（曾经踩过）：在「加 credit_expires_at」的分支里顺手 UPDATE realm，
    而 realm 这一列排在后面还没加 —— 老库升级直接 sqlite3.OperationalError:
    no such column: realm。服务端因为早就是新版所以没暴露，本地老库必崩。
    """
    wanted = {
        "runs": {"credit_snapshot": "INTEGER DEFAULT 0"},
        "accounts": {
            # credit_total 是实时可用余额（WorkBuddy 资源包汇总 / Qoder quota 汇总）
            "credit_total": "INTEGER DEFAULT 0",
            "credit_total_at": "INTEGER DEFAULT 0",
            "credit_expires_at": "INTEGER DEFAULT 0",
            "credit_expiry_amount": "INTEGER DEFAULT 0",
            # 当日开盘余额：业务日第一次读到余额时记录「昨天的收盘值」，
            # 今日变动 = 当前余额 − 开盘余额（官方口径，能覆盖面板没记到的增减）
            "credit_open_balance": "INTEGER DEFAULT 0",
            "credit_open_date": "TEXT DEFAULT ''",
            "realm": "TEXT DEFAULT ''",
        },
    }
    added_cols: set[tuple[str, str]] = set()
    for table, columns in wanted.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name in existing:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            added_cols.add((table, name))

    # ---- 回填（此时列一定都在）----
    cols = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
    if ("accounts", "credit_expires_at") in added_cols:
        # credit_expires_at 是「余额改走资源包账单」的标志位：老库里的 credit_total
        # 装的是签到活动的累计，口径完全不同。留着它会让第一次余额查询被当成
        # 「余额凭空涨了 454」记进流水，所以加列时一并清零。
        conn.execute("UPDATE accounts SET credit_total=0, credit_total_at=0")
    if "realm" in cols:
        # 补列/历史遗留的空 realm 都按国内版处理（此前只有国内基址）。
        # 幂等：每次启动跑一遍也没副作用。
        conn.execute(
            "UPDATE accounts SET realm='cn' "
            "WHERE platform='qoder' AND COALESCE(realm,'')=''"
        )
    if ("accounts", "credit_open_date") in added_cols:
        conn.execute(
            "UPDATE accounts SET credit_open_balance=credit_total, credit_open_date=?",
            (today_str(),),
        )

    # 当日开盘余额回填（一次性，settings 标记幂等）：
    # 升级当天没有「昨收」可参考，用两个线索还原今开，取**更早**的那个：
    #   ① 当前余额 − 今天的任务累计（面板自己发的领取）
    #   ② 今天最早一条「积分余额 +N（prev → now）」日志里的 prev
    #      （能覆盖面板任务之外的增减，例如在客户端手动领的）
    # 此后每天由 credits.refresh 自然维护，不再依赖这里。
    if "credit_open_date" in cols and get_setting("credit_open_backfill") != "3":
        import re as _re
        from datetime import datetime as _dt

        from .config import TZ as _TZ

        today = today_str()
        start_ts = int(
            _dt.now(_TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        )
        log_rows = conn.execute(
            "SELECT msg FROM run_logs WHERE ts>=? AND msg LIKE '积分余额 %' ORDER BY ts",
            (start_ts,),
        ).fetchall()
        for aid, name, total in conn.execute(
            "SELECT id, name, credit_total FROM accounts"
        ).fetchall():
            task_sum = conn.execute(
                "SELECT COALESCE(SUM(credit),0) FROM daily_stats"
                " WHERE account_id=? AND biz_date=?",
                (aid, today),
            ).fetchone()[0]
            open_balance = max(0, int(total or 0) - int(task_sum or 0))
            for (msg,) in log_rows if total else ():
                # 日志形态：积分余额 +100（500 → 600）· 账号名   ——「+」是字面量，必须转义
                m = _re.search(r"积分余额 \+(\d+)（(\d+) → (\d+)）· (.+)$", msg or "")
                if m and m.group(4).strip() == name:
                    open_balance = min(open_balance, int(m.group(2)))
                    break
            conn.execute(
                "UPDATE accounts SET credit_open_balance=?, credit_open_date=? WHERE id=?",
                (open_balance, today, aid),
            )
        set_setting("credit_open_backfill", "3")
    # ---- 任务归一（2026-10-30，幂等）----
    # Qoder 的「签到」统一由「每天领 100 积分 / Credits」活动承接（官方业务日
    # 10:00 UTC+8 刷新），按账号区域自动适配：国内版领积分、国际版领 Credits。
    # 签到任务挪进 10:05-11:55 的活动窗口；与之完全重复的「每日 Credits」任务
    # 直接删除（运行历史查询已改 LEFT JOIN，历史不丢）。
    conn.execute(
        """UPDATE task_defs SET name='Qoder 每日签到（领 100 积分 / Credits）',
           cron='5,15,25,35,45,55 10-13 * * *', window_start='10:00', window_end='13:00'
           WHERE platform='qoder' AND task_key='checkin' AND name IN
                 ('Qoder 每日签到', 'Qoder 每日签到（领 100 积分 / Credits）')"""
    )
    conn.execute("DELETE FROM task_defs WHERE platform='qoder' AND task_key='credit'")
    # 窗口放宽到 13:00（2026-11-01 实测：国际版每日活动可能到 11:00 才下发，
    # 12:00 截止会漏；活动平台对重复领取是幂等的，多试几次没有副作用）。
    conn.execute(
        """UPDATE task_defs SET cron='5,15,25,35,45,55 10-13 * * *',
           window_start='10:00', window_end='13:00'
           WHERE platform='qoder' AND task_key='checkin'
             AND cron IN ('5,15,25,35,45,55 10-11 * * *', '5,15,25,35,45,55 10-13 * * *')"""
    )


def _seed_task_defs() -> None:
    defaults = [
        ("workbuddy", "checkin", "WorkBuddy 每日签到", "30 8 * * *", "00:05", "23:30", 3, 10, 1),
        ("workbuddy", "travel", "WorkBuddy 宠物旅行", "*/30 * * * *", "00:00", "23:59", 2, 20, 1),
        # Qoder 唯一的每日任务：签到由「每天领 100 积分 / Credits」活动承接，
        # 按账号区域自动适配（国内领积分、国际领 Credits），业务日 10:00 刷新。
        # 窗口留到 13:00：实测国际版活动可能 11:00 才下发（2026-11-01）。
        ("qoder", "checkin", "Qoder 每日签到（领 100 积分 / Credits）",
         "5,15,25,35,45,55 10-13 * * *", "10:00", "13:00", 3, 25, 1),
    ]
    conn = get_conn()
    for platform, key, name, cron, ws, we, attempts, sort, enabled in defaults:
        conn.execute(
            """INSERT OR IGNORE INTO task_defs
               (platform, task_key, name, cron, window_start, window_end, max_attempts, enabled, sort)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (platform, key, name, cron, ws, we, attempts, enabled, sort),
        )
    conn.commit()


def query(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    with _lock:
        cur = get_conn().execute(sql, tuple(params))
        return [dict(r) for r in cur.fetchall()]


def query_one(sql: str, params: Iterable[Any] = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    with _lock:
        conn = get_conn()
        cur = conn.execute(sql, tuple(params))
        conn.commit()
        return cur.lastrowid or cur.rowcount


def execute_many(sql: str, rows: Iterable[Iterable[Any]]) -> None:
    with _lock:
        conn = get_conn()
        conn.executemany(sql, [tuple(r) for r in rows])
        conn.commit()


def get_setting(key: str, default: str = "") -> str:
    row = query_one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    execute(
        "INSERT INTO settings(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def today_str() -> str:
    """业务日（按配置时区）。原来用裸 datetime.now()，容器里是 UTC，
    每天 00:00-08:00 会和 runner 的 biz_date 差一天，总览「今日」对不上。"""
    from datetime import datetime

    from .config import TZ

    return datetime.now(TZ).strftime("%Y-%m-%d")


def log(run_id: int, msg: str, level: str = "INFO") -> None:
    execute(
        "INSERT INTO run_logs(run_id, ts, level, msg) VALUES(?,?,?,?)",
        (run_id, now(), level, msg),
    )
