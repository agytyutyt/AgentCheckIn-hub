"""实时积分余额：短 TTL 缓存 + 落库 + 余额变动留痕（WorkBuddy 与 Qoder）。

为什么要单独查：面板任务回执里的积分是「本次到账」，不是可用余额；
WorkBuddy 的真余额散在十几个资源包里，Qoder 的在 quota/usage 的
userQuota + addOnQuota 里，消耗后必须实时汇总才看得准。口径参照
https://github.com/ithtelab/workbuddy-manager （server/services/credits.py、
tencent.fetch_credits）与 https://github.com/shuishuipingan/qoder2api-hub
（qoder_accounts.Account.fetch_credits）。WorkBuddy 侧信封里的 TotalDosage
是额度总量（各包 size 之和），拿它当余额会偏高，不能偷懒用。

为什么要缓存：面板每刷新一次页面就全量查上游，账号一多是无谓的账单请求，也可能
踩限流。TTL 内的重复读取直接回落库里已存的值；手动点「刷新」走 force=True。
返回体带 cached / age，前端据此区分「实时」与「x 秒前」，不让滞后数字伪装成刚查的。

余额变动留痕：签到、旅行领奖、活动任务、盲盒都会加钱，但每个接口只报自己那一份，
很容易漏。这里在每次真实查询成功后与上一次比对，**只要增加就记一条日志**，等于把
所有获取渠道一网打尽（减少是消耗，不属于「积分获取」，不记）。
"""
from __future__ import annotations

import asyncio
import os
import time

from . import db
from .providers import registry
from .providers.base import ProviderError

# 一次真实上游查询结果的有效窗口（秒）
TTL_SECONDS = int(os.environ.get("WB_CREDITS_TTL", "60"))
# 后台定时刷新间隔（分钟）：没人开页面时余额也照样往前走
REFRESH_MINUTES = int(os.environ.get("WB_CREDITS_REFRESH_MIN", "10"))

# account_id -> 上次「真实查询」的时刻（进程内，重启即失）
_touched: dict[int, float] = {}

# 单次余额增加的合理上限，异常值不入流水（正常收益远小于此）
_MAX_DELTA = 1_000_000


def invalidate(account_id: int | None = None) -> None:
    """让缓存失效。签到等会改变余额的操作之后调用，下一次读取就是新值。"""
    if account_id is None:
        _touched.clear()
    else:
        _touched.pop(account_id, None)


def _snapshot(acc: dict | None) -> dict:
    """库里已存的余额（不查上游）。"""
    if not acc:
        return {"balance": 0, "at": 0, "age": 0, "expires_at": 0, "expiry_amount": 0,
                "today_delta": 0, "open_balance": 0}
    at = int(acc.get("credit_total_at") or 0)
    balance = int(acc.get("credit_total") or 0)
    open_balance = int(acc.get("credit_open_balance") or 0)
    same_day = str(acc.get("credit_open_date") or "") == db.today_str()
    return {
        "balance": balance,
        "at": at,
        "age": max(0, db.now() - at) if at else 0,
        "expires_at": int(acc.get("credit_expires_at") or 0),
        "expiry_amount": int(acc.get("credit_expiry_amount") or 0),
        "open_balance": open_balance,
        "today_delta": balance - open_balance if same_day else 0,
    }


async def refresh(account_id: int, *, force: bool = False) -> dict:
    """取一个账号的实时余额；TTL 内且非 force 时回落库里已存值。

    返回：{ok, supported, cached, age, balance, expires_at, expiry_amount, packages, message}
    """
    acc = db.query_one("SELECT * FROM accounts WHERE id=?", (account_id,))
    if not acc:
        return {"ok": False, "supported": False, "message": "账号不存在", **_snapshot(acc)}

    platform = acc["platform"]
    if platform not in ("workbuddy", "qoder"):
        return {
            "ok": False,
            "supported": False,
            "message": "该平台没有余额接口",
            **_snapshot(acc),
        }

    last = _touched.get(account_id, 0)
    if not force and last and time.time() - last < TTL_SECONDS:
        return {"ok": True, "supported": True, "cached": True, **_snapshot(acc)}

    from .security import decrypt_json

    info: dict | None = None
    error = ""
    try:
        info = await registry.get(platform).fetch_credits(decrypt_json(acc["cred_enc"]))
    except ProviderError as exc:
        error = exc.message
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"

    if info is None:
        # 失败不留缓存：多半是临时网络或令牌问题，下一轮就该重试
        _touched.pop(account_id, None)
        return {
            "ok": False,
            "supported": True,
            "cached": True,
            "message": error,
            **_snapshot(acc),
        }

    # 部分平台查询时会顺带轮换令牌（Qoder 401 自愈），原样落库
    cred_update = info.get("cred_update")
    if cred_update:
        from .security import encrypt_json

        db.execute(
            "UPDATE accounts SET cred_enc=?, token_expire_at=? WHERE id=?",
            (
                encrypt_json(cred_update),
                int(cred_update.get("expire_at") or 0),
                account_id,
            ),
        )

    balance = int(info.get("balance") or 0)
    prev = int(acc.get("credit_total") or 0)

    # 当日开盘余额：业务日第一次读到余额时，把「上次读到的余额（= 昨收）」记成今开。
    # 于是「今日变动 = 余额 − 开盘」是**官方口径**：面板任务没记到的增减（客户端领的、
    # 手动兑的、上游补发的）都能体现；任务累计则另存一个字段做对照。
    today = db.today_str()
    open_date = str(acc.get("credit_open_date") or "")
    open_balance = int(acc.get("credit_open_balance") or 0)
    new_open_balance, new_open_date = open_balance, open_date
    if open_date != today:
        # 跨日（或首次读数）：昨收当今开；从没读过就用本次值建基线（今日变动 0）
        new_open_balance = prev if (open_date and prev) else balance
        new_open_date = today

    db.execute(
        """UPDATE accounts SET credit_total=?, credit_total_at=?, credit_expires_at=?,
                              credit_expiry_amount=?, credit_open_balance=?,
                              credit_open_date=? WHERE id=?""",
        (
            balance,
            db.now(),
            int(info.get("expires_at") or 0),
            int(info.get("expiry_amount") or 0),
            new_open_balance,
            new_open_date,
            account_id,
        ),
    )
    _touched[account_id] = time.time()

    # 首次见到的账号只建基线，否则会把历史余额当成「刚获得」记一笔
    delta = balance - prev
    if prev and 0 < delta <= _MAX_DELTA:
        db.log(0, f"积分余额 +{delta}（{prev} → {balance}）· {acc['name']}", "INFO")

    return {
        "ok": True,
        "supported": True,
        "cached": False,
        "age": 0,
        "balance": balance,
        # 今日变动（官方余额差）：与任务累计分开给，前端两处都能用
        "today_delta": balance - new_open_balance if new_open_date == today else 0,
        "open_balance": new_open_balance,
        "packages": int(info.get("packages") or 0),
        "expires_at": int(info.get("expires_at") or 0),
        "expiry_amount": int(info.get("expiry_amount") or 0),
        "expiries": info.get("expiries") or [],
    }


async def refresh_many(account_ids: list[int], budget: float = 8.0) -> None:
    """并发刷一批账号，整批有总时限。

    页面渲染里会调它，所以绝不能被上游拖死：到点就放弃，页面显示库里上一次的值，
    并如实标注为缓存。
    """
    if not account_ids:
        return
    try:
        await asyncio.wait_for(
            asyncio.gather(*(refresh(aid) for aid in account_ids), return_exceptions=True),
            timeout=budget,
        )
    except asyncio.TimeoutError:
        pass


async def tick() -> int:
    """后台定时刷新（调度器每 REFRESH_MINUTES 分钟一次）。返回真正查到新值的账号数。"""
    ids = [
        r["id"]
        for r in db.query(
            "SELECT id FROM accounts WHERE platform IN ('workbuddy','qoder') AND enabled=1 ORDER BY id"
        )
    ]
    if not ids:
        return 0
    done = await asyncio.gather(*(refresh(aid) for aid in ids), return_exceptions=True)
    return sum(1 for item in done if isinstance(item, dict) and item.get("ok") and not item.get("cached"))
