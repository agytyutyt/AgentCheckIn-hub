"""任务执行器：单账号单任务的完整生命周期（执行 → 落库 → 重试 → 通知）。"""
from __future__ import annotations

import asyncio
import json
import time
import traceback
from datetime import datetime

import httpx

from . import credits, db, notify
from .config import TZ
from .providers import registry
from .providers.base import ProviderError, TaskResult
from .security import decrypt_json, encrypt_json, scrub

BACKOFF_SEC = [600, 1800, 7200]  # 10min / 30min / 2h
_locks: dict[str, asyncio.Lock] = {}


def _lock(key: str) -> asyncio.Lock:
    if key not in _locks:
        _locks[key] = asyncio.Lock()
    return _locks[key]


def within_window(task: dict) -> bool:
    now = datetime.now(TZ).strftime("%H:%M")
    start, end = task.get("window_start", "00:00"), task.get("window_end", "23:59")
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end


def _biz_date() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def _fmt_ts(ts: int) -> str:
    try:
        return datetime.fromtimestamp(int(ts), TZ).strftime("%m-%d %H:%M:%S")
    except Exception:
        return "-"


async def run_account(task: dict, account: dict, trigger: str = "cron", attempt: int = 1) -> dict:
    """执行一次；返回 {run_id, status, message}。并发冲突时返回 skipped。"""
    lock = _lock(f"{task['id']}:{account['id']}")
    if lock.locked():
        return {"run_id": 0, "status": "skipped", "message": "同一任务正在执行，本次跳过"}

    async with lock:
        biz = _biz_date()
        t0 = time.time()
        started = db.now()
        run_id = db.execute(
            """INSERT INTO runs(task_def_id, account_id, biz_date, attempt, status, started_at, trigger)
               VALUES(?,?,?,?,?,?,?)""",
            (task["id"], account["id"], biz, attempt, "running", started, trigger),
        )
        # 开始行用 DEBUG：日志页默认只看结果行，一次任务一行，签到不会被轮询顶掉；
        # 想看完整过程时在日志页勾「显示空跑/调试」即可。
        db.log(run_id, f"开始执行 {task['name']}（账号：{account['name']}，第 {attempt} 次）", "DEBUG")

        result: TaskResult
        try:
            cred = decrypt_json(account["cred_enc"])
            provider = registry.get(task["platform"])
            result = await provider.run(cred, task["task_key"])
        except ProviderError as exc:
            result = TaskResult(exc.status, exc.message, detail=exc.detail)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            result = TaskResult("retryable", f"网络异常：{type(exc).__name__} {exc}")
        except Exception as exc:  # 兜底，避免线程静默死亡
            result = TaskResult(
                "retryable",
                f"未预期异常：{type(exc).__name__}: {exc}",
                detail={"trace": traceback.format_exc()[-1200:]},
            )

        finished = db.now()
        elapsed_ms = int((time.time() - t0) * 1000)
        message = scrub(result.message)[:500]
        db.execute(
            """UPDATE runs SET status=?, message=?, delta_credit=?, credit_snapshot=?, finished_at=?,
                               duration_ms=?, detail=?
               WHERE id=?""",
            (
                result.status,
                message,
                result.delta_credit,
                int(result.credit_snapshot or 0),
                finished,
                elapsed_ms,
                notify.dumps(result.detail),
                run_id,
            ),
        )
        if result.quiet:
            # 纯空跑（已签 / 名额用完 / 还在路上）：DEBUG 落盘，日志页默认折叠，
            # 但仍可在运行记录和「显示空跑」里查到，不会丢。
            db.log(run_id, f"空跑：{result.status} · {message}", "DEBUG")
        else:
            db.log(
                run_id,
                f"结束：{result.status} · {message}",
                "ERROR" if result.status in ("failed", "need_login") else "INFO",
            )

        # 令牌轮换落盘
        if result.cred_update:
            db.execute(
                "UPDATE accounts SET cred_enc=?, token_expire_at=? WHERE id=?",
                (
                    encrypt_json(result.cred_update),
                    int(result.cred_update.get("expire_at") or 0),
                    account["id"],
                ),
            )
            db.log(run_id, "令牌已轮换并加密回写")

        # 账号状态
        if result.status == "need_login":
            db.execute(
                "UPDATE accounts SET status='need_login', last_error=? WHERE id=?",
                (message, account["id"]),
            )
        elif result.ok:
            db.execute(
                "UPDATE accounts SET status='ok', last_error='', last_ok_at=? WHERE id=?",
                (finished, account["id"]),
            )
        else:
            db.execute(
                "UPDATE accounts SET status='error', last_error=? WHERE id=?",
                (message, account["id"]),
            )

        # 当日积分汇总。快照语义（服务端给的「今天该任务累计」）按天取最大值，
        # 增量语义（旅行领取、Qoder 领取）才累加——否则一天跑几次就会记成 100×N。
        if result.credit_snapshot is not None:
            db.execute(
                """INSERT INTO daily_stats(biz_date, account_id, task_key, credit) VALUES(?,?,?,?)
                   ON CONFLICT(biz_date, account_id, task_key)
                   DO UPDATE SET credit = MAX(credit, excluded.credit)""",
                (biz, account["id"], task["task_key"], int(result.credit_snapshot or 0)),
            )
        elif result.delta_credit:
            db.execute(
                """INSERT INTO daily_stats(biz_date, account_id, task_key, credit) VALUES(?,?,?,?)
                   ON CONFLICT(biz_date, account_id, task_key)
                   DO UPDATE SET credit = credit + excluded.credit""",
                (biz, account["id"], task["task_key"], result.delta_credit),
            )

        # 领到钱就意味着余额变了：让 TTL 缓存立刻失效，下一次读就是新值，
        # 不至于出现「刚签到 +100，余额还是三分钟前那个数」。
        if result.delta_credit and task["platform"] in ("workbuddy", "qoder"):
            credits.invalidate(account["id"])

        # 重试与告警
        if result.should_retry:
            await _schedule_retry(task, account, attempt, message)
            if attempt >= int(task.get("max_attempts", 3)) or not within_window(task):
                await notify.send(
                    f"{task['name']} 执行失败",
                    f"账号：{account['name']}\n原因：{message}\n已重试 {attempt} 次",
                    "failed",
                )
        elif result.status == "need_login":
            await notify.send(
                f"{task['name']} 需要重新登录",
                f"账号：{account['name']}\n{message}",
                "need_login",
            )
        elif result.status == "success" and db.get_setting("notify_on_success", "0") == "1":
            await notify.send(
                f"{task['name']} 成功", f"账号：{account['name']}\n{message}", "success"
            )

        return {"run_id": run_id, "status": result.status, "message": message}


async def _schedule_retry(task: dict, account: dict, attempt: int, message: str) -> None:
    max_attempts = int(task.get("max_attempts", 3))
    if attempt >= max_attempts or not within_window(task):
        return
    delay = BACKOFF_SEC[min(attempt - 1, len(BACKOFF_SEC) - 1)]
    db.execute(
        """INSERT INTO retry_queue(task_def_id, account_id, biz_date, attempt, next_at, created_at)
           VALUES(?,?,?,?,?,?)""",
        (task["id"], account["id"], _biz_date(), attempt + 1, db.now() + delay, db.now()),
    )
    db.log(
        0,
        f"已排入补偿队列：{task['name']} / {account['name']}，{delay // 60} 分钟后重试",
        "WARN",
    )


async def run_task(task_id: int, trigger: str = "manual") -> list[dict]:
    task = db.query_one("SELECT * FROM task_defs WHERE id=?", (task_id,))
    if not task:
        return []
    accounts = db.query(
        "SELECT * FROM accounts WHERE platform=? AND enabled=1 ORDER BY id", (task["platform"],)
    )
    results = []
    for acc in accounts:
        results.append(await run_account(task, acc, trigger))
        await asyncio.sleep(1)
    return results


async def probe_account(account_id: int) -> dict:
    account = db.query_one("SELECT * FROM accounts WHERE id=?", (account_id,))
    if not account:
        return {"ok": False, "message": "账号不存在"}
    try:
        cred = decrypt_json(account["cred_enc"])
        info = await registry.get(account["platform"]).probe(cred)
    except ProviderError as exc:
        db.execute(
            "UPDATE accounts SET status=? , last_error=? WHERE id=?",
            ("need_login" if exc.status == "need_login" else "error", exc.message, account_id),
        )
        return {"ok": False, "message": exc.message}
    except Exception as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}

    update = info.pop("cred_update", None)
    if update:
        db.execute(
            "UPDATE accounts SET cred_enc=?, token_expire_at=? WHERE id=?",
            (encrypt_json(update), int(update.get("expire_at") or 0), account_id),
        )
    db.execute(
        """UPDATE accounts SET status='ok', last_error='', last_ok_at=?,
           uid=COALESCE(NULLIF(?,''), uid), token_expire_at=? WHERE id=?""",
        (db.now(), info.get("uid", ""), int(info.get("expire_at") or 0), account_id),
    )
    # 顺手把实时余额落库，点「测试」就能立刻看到最新数字
    if info.get("credit_balance") is not None:
        db.execute(
            """UPDATE accounts SET credit_total=?, credit_total_at=?, credit_expires_at=?
               WHERE id=?""",
            (
                int(info["credit_balance"]),
                db.now(),
                int(info.get("credit_expires_at") or 0),
                account_id,
            ),
        )
        credits.invalidate(account_id)
    return {"ok": True, "message": "连接正常", "info": info}


async def retry_tick() -> int:
    """补偿队列扫描，每分钟一次。"""
    now = db.now()
    due = db.query(
        "SELECT * FROM retry_queue WHERE status='pending' AND next_at<=? ORDER BY next_at LIMIT 10",
        (now,),
    )
    done = 0
    for item in due:
        task = db.query_one("SELECT * FROM task_defs WHERE id=?", (item["task_def_id"],))
        account = db.query_one("SELECT * FROM accounts WHERE id=?", (item["account_id"],))
        db.execute("UPDATE retry_queue SET status='running' WHERE id=?", (item["id"],))
        if not task or not account or not within_window(task):
            db.execute("UPDATE retry_queue SET status='expired' WHERE id=?", (item["id"],))
            continue
        await run_account(task, account, "retry", int(item["attempt"]))
        db.execute("UPDATE retry_queue SET status='done' WHERE id=?", (item["id"],))
        done += 1
    return done
