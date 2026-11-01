"""FastAPI 应用：页面路由 + JSON API + SSE 日志流。"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import authflow, credits, db, notify, qoderlogin, qoderumid, renew, runner
from .providers.qoder import realm_name as qoder_realm_name
from .config import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    APP_NAME,
    APP_VERSION,
    DB_PATH,
    HOST,
    PORT,
    TZ,
    TZ_NAME,
)
from .providers import registry
from .providers.base import ProviderError
from .scheduler import mgr
from .security import (
    decrypt_json,
    encrypt_json,
    hash_password,
    mask,
    parse_session,
    sign_session,
    verify_password,
)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Qoder 区域（国内 / 国际两个独立账号池）
REALM_CHOICES = ("cn", "intl")


def _fmt_dt(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts), TZ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "—"


def _fmt_time(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts), TZ).strftime("%H:%M:%S")
    except Exception:
        return "—"


templates.env.filters["datetime_fmt"] = _fmt_dt
templates.env.filters["time_fmt"] = _fmt_time

app = FastAPI(title=APP_NAME, version=APP_VERSION, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

OPEN_PATHS = ("/login", "/healthz", "/static", "/favicon.ico")


# ------------------------------------------------------------------ 认证
def current_user(request: Request) -> dict | None:
    token = request.cookies.get("ch_session", "")
    if not token:
        return None
    return parse_session(token)


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path
    if any(path == p or path.startswith(p + "/") for p in OPEN_PATHS):
        return await call_next(request)
    if current_user(request) is None:
        if path.startswith("/api/"):
            return JSONResponse({"ok": False, "message": "未登录"}, status_code=401)
        return RedirectResponse(f"/login?next={quote(path)}", status_code=303)
    return await call_next(request)


def render(name: str, context: dict, status_code: int = 200):
    """兼容 Starlette 1.x 的模板渲染入口。"""
    return templates.TemplateResponse(context["request"], name, context, status_code=status_code)


def ctx(request: Request, **kwargs) -> dict:
    user = current_user(request) or {}
    base = {
        "request": request,
        "app_name": APP_NAME,
        "version": APP_VERSION,
        "user": user.get("u", ""),
        "tz": TZ_NAME,
        "now": datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
    }
    base.update(kwargs)
    return base


def _fmt_age(seconds: int) -> str:
    if seconds < 45:
        return "刚刚"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前"
    return f"{seconds // 86400} 天前"


def _attach_credit(accounts: list[dict]) -> None:
    """给每个账号补上余额展示字段。

    WorkBuddy 读 accounts.credit_total —— 那是 billing 资源包接口逐包汇总出来的
    **实时可用余额**；Qoder 没有余额接口（候选路径实测全 404），只能退化成面板
    本地累计，文案上必须分开，不能都叫「余额」。
    """
    today = db.today_str()
    now = db.now()
    local: dict[int, int] = {}
    today_sum: dict[int, int] = {}
    for row in db.query("SELECT account_id, biz_date, credit FROM daily_stats"):
        aid = row["account_id"]
        value = row["credit"] or 0
        local[aid] = local.get(aid, 0) + value
        if row["biz_date"] == today:
            today_sum[aid] = today_sum.get(aid, 0) + value

    for acc in accounts:
        at = int(acc.get("credit_total_at") or 0)
        server = int(acc.get("credit_total") or 0)
        supported = acc["platform"] in ("workbuddy", "qoder")
        # 区域徽标：Qoder 分 Qoder CN / Qoder 两个池；WorkBuddy 不标
        if acc["platform"] == "qoder":
            acc["realm"] = (acc.get("realm") or "cn").lower()
            acc["realm_name"] = qoder_realm_name(acc["realm"])
        else:
            acc["realm"], acc["realm_name"] = "", ""
        acc["credit_supported"] = supported
        # 两套口径：
        #   credit_today      = 官方余额差（当前余额 − 当日开盘余额），**准确且完整**
        #   credit_task_today = 面板任务当天累计（只含面板自己发起的领取，作对照）
        task_today = today_sum.get(acc["id"], 0)
        acc["credit_task_today"] = task_today
        open_date = str(acc.get("credit_open_date") or "")
        open_balance = int(acc.get("credit_open_balance") or 0)
        if supported and open_date == today:
            acc["credit_today"] = server - open_balance
            acc["credit_today_source"] = "官方余额差"
        else:
            acc["credit_today"] = task_today
            acc["credit_today_source"] = "任务累计"
        if supported and (at or server):
            acc["credit_balance"] = server
            acc["credit_kind"] = "积分余额"
            acc["credit_age_text"] = _fmt_age(now - at) if at else "未读取"
            exp_at = int(acc.get("credit_expires_at") or 0)
            exp_amt = int(acc.get("credit_expiry_amount") or 0)
            # 只提醒 15 天内要过期的那部分，平时不占版面
            acc["credit_expiry_text"] = (
                f"其中 {exp_amt} 将于 {datetime.fromtimestamp(exp_at, TZ).strftime('%m-%d')} 过期"
                if exp_at and exp_amt and 0 < exp_at - now < 15 * 86400
                else ""
            )
        else:
            acc["credit_balance"] = server or local.get(acc["id"], 0)
            acc["credit_kind"] = "累计已领"
            acc["credit_age_text"] = ""
            acc["credit_expiry_text"] = ""


@app.get("/healthz")
async def healthz():
    return {"ok": True, "version": APP_VERSION, "time": datetime.now(TZ).isoformat()}


@app.get("/favicon.ico")
async def favicon():
    return JSONResponse({}, status_code=204)


# ------------------------------------------------------------------ 登录
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/dashboard", status_code=303)
    return render("login.html", ctx(request, error=""))


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    if username != ADMIN_USERNAME or not verify_password(password, _admin_hash()):
        return render("login.html", ctx(request, error="用户名或口令不正确"), status_code=401
        )
    resp = RedirectResponse("/dashboard", status_code=303)
    resp.set_cookie(
        "ch_session",
        sign_session(username),
        httponly=True,
        samesite="lax",
        max_age=7 * 86400,
        path="/",
    )
    return resp


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("ch_session")
    return resp


def _admin_hash() -> str:
    stored = db.get_setting("admin_pw_hash", "")
    if stored:
        return stored
    hashed = hash_password(ADMIN_PASSWORD)
    db.set_setting("admin_pw_hash", hashed)
    return hashed


def _init_admin_password() -> None:
    """环境变量优先；否则用默认口令 admin123。首次写入时在控制台提示。"""
    from .config import ADMIN_PASSWORD_FROM_ENV, DEFAULT_ADMIN_PASSWORD

    stored = db.get_setting("admin_pw_hash", "")
    just_set = False
    if ADMIN_PASSWORD_FROM_ENV:
        if not stored or not verify_password(ADMIN_PASSWORD, stored):
            db.set_setting("admin_pw_hash", hash_password(ADMIN_PASSWORD))
            just_set = True
    elif not stored:
        db.set_setting("admin_pw_hash", hash_password(ADMIN_PASSWORD))
        just_set = True

    if just_set and ADMIN_PASSWORD == DEFAULT_ADMIN_PASSWORD:
        print("=" * 62, flush=True)
        print(f"  登录口令：{ADMIN_PASSWORD}（用户名 {ADMIN_USERNAME}）", flush=True)
        print("  这是默认口令，公网部署前请在 .env 修改 ADMIN_PASSWORD，", flush=True)
        print("  或登录后到「设置 → 登录口令」修改。", flush=True)
        print("=" * 62, flush=True)


# ------------------------------------------------------------------ 页面
@app.get("/")
async def index():
    return RedirectResponse("/dashboard", status_code=303)


async def _accounts_with_credit() -> list[dict]:
    """查余额（TTL 内自动跳过）→ 重查账号 → 附展示字段。

    顺序很重要：refresh 会把新值写进库，渲染用的必须是刷新之后重新查出来的行，
    否则页面显示的永远是上一次的值。
    """
    order = "ORDER BY CASE platform WHEN 'workbuddy' THEN 0 ELSE 1 END, id"
    rows = db.query(f"SELECT * FROM accounts {order}")
    await credits.refresh_many(
        [r["id"] for r in rows if r["platform"] in ("workbuddy", "qoder")]
    )
    rows = db.query(f"SELECT * FROM accounts {order}")
    _attach_credit(rows)
    return rows


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    accounts = await _accounts_with_credit()
    tasks = db.query("SELECT * FROM task_defs ORDER BY sort, id")
    today = db.today_str()

    latest = db.query(
        """SELECT r.* FROM runs r
           JOIN (SELECT account_id, task_def_id, MAX(id) AS mid FROM runs
                 WHERE biz_date=? GROUP BY account_id, task_def_id) t ON r.id=t.mid""",
        (today,),
    )
    status_map = {f"{r['account_id']}:{r['task_def_id']}": r for r in latest}

    # 今日净增积分：优先取**官方余额差**（当前余额 − 当日开盘），它涵盖所有增减
    # （面板任务、客户端领取、上游补发）；拿不到开盘基线的账号才回落任务累计。
    task_credit_today = (
        db.query_one(
            "SELECT COALESCE(SUM(credit), 0) AS c FROM daily_stats WHERE biz_date=?", (today,)
        )
        or {}
    ).get("c", 0)
    credit_today = sum(
        (a.get("credit_today") or 0) for a in accounts if a.get("credit_supported")
    )
    ok_today = sum(1 for r in latest if r["status"] in ("success", "skipped"))
    fail_today = sum(1 for r in latest if r["status"] in ("failed", "need_login"))

    since = (datetime.now(TZ) - timedelta(days=13)).strftime("%Y-%m-%d")
    series = db.query(
        """SELECT biz_date, SUM(CASE WHEN status IN ('success','skipped') THEN 1 ELSE 0 END) AS ok,
                  SUM(CASE WHEN status IN ('failed','need_login') THEN 1 ELSE 0 END) AS bad
           FROM runs WHERE biz_date>=? GROUP BY biz_date ORDER BY biz_date""",
        (since,),
    )
    series_map = {r["biz_date"]: r for r in series}
    days = [
        ((datetime.now(TZ) - timedelta(days=i)).strftime("%Y-%m-%d"),
         series_map.get((datetime.now(TZ) - timedelta(days=i)).strftime("%Y-%m-%d"), {}))
        for i in range(13, -1, -1)
    ]
    peak = max([d[1].get("ok", 0) + d[1].get("bad", 0) for d in days] + [1])

    alerts = db.query(
        "SELECT * FROM accounts WHERE status IN ('need_login','error') AND enabled=1"
    )
    recent_fail = db.query(
        "SELECT r.*, a.name AS account_name, COALESCE(t.name,'已归一任务') AS task_name"
        " FROM runs r "
        "JOIN accounts a ON a.id=r.account_id LEFT JOIN task_defs t ON t.id=r.task_def_id "
        "WHERE r.status IN ('failed','need_login') ORDER BY r.id DESC LIMIT 5"
    )

    return render("dashboard.html", ctx(
            request,
            active="dashboard",
            accounts=accounts,
            tasks=tasks,
            status_map=status_map,
            next_runs=mgr.next_runs(),
            credit_today=credit_today,
            task_credit_today=task_credit_today,
            # 余额按平台拆分展示（需求：总览里 WorkBuddy 与 Qoder 分开）
            credit_balance_total=sum(a["credit_balance"] for a in accounts if a["credit_supported"]),
            credit_balance_workbuddy=sum(
                a["credit_balance"] for a in accounts
                if a["credit_supported"] and a["platform"] == "workbuddy"
            ),
            credit_balance_qoder=sum(
                a["credit_balance"] for a in accounts
                if a["credit_supported"] and a["platform"] == "qoder"
            ),
            ok_today=ok_today,
            fail_today=fail_today,
            days=days,
            peak=peak,
            alerts=alerts,
            recent_fail=recent_fail,
            today=today,
        ),
    )


@app.get("/accounts", response_class=HTMLResponse)
async def accounts_page(request: Request):
    accounts = await _accounts_with_credit()
    next_runs = mgr.next_runs()
    tasks = db.query("SELECT * FROM task_defs ORDER BY sort")
    task_count = {}
    for t in tasks:
        task_count[t["platform"]] = task_count.get(t["platform"], 0) + 1
    rows = []
    now_ts = db.now()
    for a in accounts:
        exp = a.get("token_expire_at") or 0
        if exp:
            left = int((exp - now_ts) / 86400)
            a["expire_text"] = f"{left} 天后过期" if left >= 0 else "已过期"
            a["expire_warn"] = left <= 7
        else:
            a["expire_text"] = "未知"
            a["expire_warn"] = False
        cred = decrypt_json(a["cred_enc"])
        a["token_hint"] = mask(cred.get("accessToken", ""))
        a["has_refresh"] = bool(cred.get("refreshToken"))
        a["location"] = cred.get("location", "")
        a["auto_depart"] = cred.get("auto_depart", True)
        if a["platform"] == "qoder":
            a["realm"] = (a.get("realm") or "cn").lower()
            a["realm_name"] = qoder_realm_name(a["realm"])
        rows.append(a)
    return render("accounts.html", ctx(
            request,
            active="accounts",
            accounts=rows,
            platforms=registry.platforms(),
            next_runs=next_runs,
        ),
    )


@app.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request):
    tasks = db.query("SELECT * FROM task_defs ORDER BY sort, id")
    next_runs = mgr.next_runs()
    accounts = db.query("SELECT platform, COUNT(*) AS c FROM accounts WHERE enabled=1 GROUP BY platform")
    counts = {r["platform"]: r["c"] for r in accounts}
    for t in tasks:
        t["next_run"] = next_runs.get(t["id"], "—")
        t["accounts"] = counts.get(t["platform"], 0)
    return render("tasks.html", ctx(request, active="tasks", tasks=tasks)
    )


@app.get("/runs", response_class=HTMLResponse)
async def runs_page(request: Request, page: int = 1, status: str = "", account: str = ""):
    page = max(1, page)
    size = 25
    where, params = ["1=1"], []
    if status:
        where.append("r.status=?")
        params.append(status)
    if account:
        where.append("a.name LIKE ?")
        params.append(f"%{account}%")
    clause = " AND ".join(where)

    total = db.query_one(
        f"SELECT COUNT(*) AS c FROM runs r JOIN accounts a ON a.id=r.account_id WHERE {clause}",
        params,
    )["c"]
    rows = db.query(
        f"""SELECT r.*, a.name AS account_name,
               COALESCE(t.name,'已归一任务') AS task_name,
               COALESCE(t.platform, a.platform) AS platform
            FROM runs r JOIN accounts a ON a.id=r.account_id
            LEFT JOIN task_defs t ON t.id=r.task_def_id
            WHERE {clause} ORDER BY r.id DESC LIMIT ? OFFSET ?""",
        params + [size, (page - 1) * size],
    )
    pages = max(1, (total + size - 1) // size)
    return render("runs.html", ctx(
            request,
            active="runs",
            runs=rows,
            page=page,
            pages=pages,
            total=total,
            status=status,
            account=account,
        ),
    )


@app.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_detail(request: Request, run_id: int):
    row = db.query_one(
        """SELECT r.*, a.name AS account_name,
              COALESCE(t.name,'已归一任务') AS task_name,
              COALESCE(t.platform, a.platform) AS platform
           FROM runs r JOIN accounts a ON a.id=r.account_id
           LEFT JOIN task_defs t ON t.id=r.task_def_id
           WHERE r.id=?""",
        (run_id,),
    )
    if not row:
        raise HTTPException(404)
    logs = db.query("SELECT * FROM run_logs WHERE run_id=? ORDER BY id", (run_id,))
    try:
        detail = json.dumps(json.loads(row["detail"] or "{}"), ensure_ascii=False, indent=2)
    except Exception:
        detail = row["detail"] or "{}"
    return render("run_detail.html", ctx(request, active="runs", run=row, logs=logs, detail=detail),
    )


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    # 200 条 ≈ 两天的结果行（宠物旅行的空跑已降到 DEBUG，默认折叠）
    rows = db.query("SELECT * FROM run_logs ORDER BY id DESC LIMIT 200")
    return render("logs.html", ctx(request, active="logs", logs=list(reversed(rows)), last_id=rows[0]["id"] if rows else 0),
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    cfg = {
        "wecom_webhook": db.get_setting("wecom_webhook", ""),
        "generic_webhook": db.get_setting("generic_webhook", ""),
        "notify_enabled": db.get_setting("notify_enabled", "1"),
        "notify_on_success": db.get_setting("notify_on_success", "0"),
    }
    return render("settings.html", ctx(
            request,
            active="settings",
            cfg=cfg,
            db_path=str(DB_PATH),
            host=HOST,
            port=PORT,
            wecom_ready=notify.wecom_enabled(),
        ),
    )


# ------------------------------------------------------------------ 账号 API
def _parse_credential(raw: str) -> dict:
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("凭证内容为空")
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except Exception as exc:
            raise ValueError(f"JSON 解析失败：{exc}")
        if "accounts" in data and isinstance(data["accounts"], list) and data["accounts"]:
            data = data["accounts"][0]
        return data
    return {"accessToken": raw}


@app.post("/api/accounts")
async def create_account(payload: dict = Body(...)):
    platform = str(payload.get("platform", "")).strip()
    name = str(payload.get("name", "")).strip() or "未命名账号"
    if platform not in ("workbuddy", "qoder"):
        return JSONResponse({"ok": False, "message": "未知平台"}, status_code=400)
    try:
        cred = _parse_credential(str(payload.get("credential", "")))
    except ValueError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)

    from .providers.base import jwt_exp, jwt_sub

    token = cred.get("accessToken") or cred.get("token") or ""
    if not token:
        return JSONResponse({"ok": False, "message": "凭证中缺少 accessToken"}, status_code=400)
    cred["accessToken"] = token
    cred["uid"] = cred.get("uid") or jwt_sub(token)
    cred["expire_at"] = int(cred.get("expire_at") or jwt_exp(token) or 0)

    if platform == "qoder" and not cred.get("device"):
        return JSONResponse(
            {
                "ok": False,
                "message": "Qoder 凭证缺少 Cosy-* 设备标识，请用 tools/import_qoder.py 导出完整 JSON 后粘贴",
            },
            status_code=400,
        )
    # Qoder 分国内 / 国际两个池：区域跟账号走（决定打哪个基址），写进凭证与账号列
    realm = str(payload.get("realm") or "").lower()
    if platform == "qoder":
        cred["realm"] = realm if realm in REALM_CHOICES else "cn"

    if payload.get("location"):
        cred["location"] = str(payload["location"]).strip()
    if payload.get("auto_depart") is not None:
        cred["auto_depart"] = bool(payload["auto_depart"])

    # 同平台 + 同 uid + 同区域视为同一账号：更新而不是再插一行（双击/重试会重复提交）
    existing = db.query_one(
        "SELECT * FROM accounts WHERE platform=? AND uid=?"
        " AND COALESCE(NULLIF(realm,''),'cn')=?",
        (platform, cred["uid"], str(cred.get("realm") or "cn")),
    )
    if existing:
        db.execute(
            "UPDATE accounts SET cred_enc=?, token_expire_at=?, enabled=1, realm=? WHERE id=?",
            (
                encrypt_json(cred),
                cred["expire_at"],
                str(cred.get("realm") or ""),
                existing["id"],
            ),
        )
        result = await runner.probe_account(existing["id"])
        return {
            "ok": True,
            "updated": True,
            "message": "账号已更新" + ("，连接正常" if result.get("ok") else f"，但校验失败：{result.get('message')}"),
            "id": existing["id"],
            "probe": result,
        }

    acc_id = db.execute(
        """INSERT INTO accounts(platform, name, uid, cred_enc, token_expire_at, status, realm, created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            platform,
            name,
            cred["uid"],
            encrypt_json(cred),
            cred["expire_at"],
            "unknown",
            str(cred.get("realm") or ""),
            db.now(),
        ),
    )
    result = await runner.probe_account(acc_id)
    return {
        "ok": True,
        "message": "账号已添加" + ("，连接正常" if result.get("ok") else f"，但校验失败：{result.get('message')}"),
        "id": acc_id,
        "probe": result,
    }


@app.post("/api/accounts/{acc_id}/update")
async def update_account(acc_id: int, payload: dict = Body(...)):
    acc = db.query_one("SELECT * FROM accounts WHERE id=?", (acc_id,))
    if not acc:
        return JSONResponse({"ok": False, "message": "账号不存在"}, status_code=404)
    cred = decrypt_json(acc["cred_enc"])
    if "name" in payload:
        db.execute("UPDATE accounts SET name=? WHERE id=?", (str(payload["name"]).strip(), acc_id))
    if "location" in payload:
        cred["location"] = str(payload["location"]).strip()
    if "auto_depart" in payload:
        cred["auto_depart"] = bool(payload["auto_depart"])
    if acc["platform"] == "qoder" and str(payload.get("realm") or "").lower() in REALM_CHOICES:
        realm = str(payload["realm"]).lower()
        cred["realm"] = realm
        db.execute("UPDATE accounts SET realm=? WHERE id=?", (realm, acc_id))
    if payload.get("credential"):
        try:
            new_cred = _parse_credential(str(payload["credential"]))
        except ValueError as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        cred.update(new_cred)
        db.execute(
            "UPDATE accounts SET cred_enc=?, token_expire_at=? WHERE id=?",
            (encrypt_json(cred), int(cred.get("expire_at") or 0), acc_id),
        )
    else:
        db.execute("UPDATE accounts SET cred_enc=? WHERE id=?", (encrypt_json(cred), acc_id))
    return {"ok": True, "message": "已保存"}


@app.post("/api/accounts/{acc_id}/toggle")
async def toggle_account(acc_id: int):
    acc = db.query_one("SELECT * FROM accounts WHERE id=?", (acc_id,))
    if not acc:
        return JSONResponse({"ok": False, "message": "账号不存在"}, status_code=404)
    db.execute("UPDATE accounts SET enabled=? WHERE id=?", (0 if acc["enabled"] else 1, acc_id))
    return {"ok": True, "message": "已" + ("停用" if acc["enabled"] else "启用")}


@app.post("/api/accounts/{acc_id}/delete")
async def delete_account(acc_id: int):
    db.execute("DELETE FROM accounts WHERE id=?", (acc_id,))
    return {"ok": True, "message": "已删除"}


@app.post("/api/accounts/{acc_id}/test")
async def test_account(acc_id: int):
    result = await runner.probe_account(acc_id)
    return result


# ------------------------------------------------------------------ 积分余额 API
@app.get("/api/credits")
async def api_credits(force: int = 0):
    """各账号余额快照，给前端定时拉取（不整页刷新）。

    默认走 TTL 缓存；`?force=1` 绕过缓存真查上游。
    """
    order = "ORDER BY CASE platform WHEN 'workbuddy' THEN 0 ELSE 1 END, id"
    rows = db.query(f"SELECT * FROM accounts {order}")
    ids = [r["id"] for r in rows if r["platform"] in ("workbuddy", "qoder")]
    if ids:
        await asyncio.gather(
            *(credits.refresh(i, force=bool(force)) for i in ids), return_exceptions=True
        )
    rows = db.query(f"SELECT * FROM accounts {order}")
    _attach_credit(rows)
    return {
        "ok": True,
        "ttl": credits.TTL_SECONDS,
        "generated_at": db.now(),
        "total": sum(r["credit_balance"] for r in rows if r["credit_supported"]),
        "accounts": [
            {
                "id": r["id"],
                "name": r["name"],
                "platform": r["platform"],
                "realm": r.get("realm") or "",
                "realm_name": r.get("realm_name") or "",
                "supported": r["credit_supported"],
                "kind": r["credit_kind"],
                "balance": r["credit_balance"],
                "today": r["credit_today"],
                "today_source": r.get("credit_today_source") or "",
                "task_today": r.get("credit_task_today") or 0,
                "age_text": r["credit_age_text"],
                "expiry_text": r["credit_expiry_text"],
            }
            for r in rows
        ],
    }


@app.post("/api/accounts/{acc_id}/credits")
async def refresh_account_credits(acc_id: int):
    """手动刷新单个账号的余额（绕过缓存真查一次）。

    返回里带 `today_delta`（官方余额差：余额 − 当日开盘）与 `task_today`
    （面板任务累计，对照用），前端据此更新「今日新增」，不再传 null。
    """
    result = await credits.refresh(acc_id, force=True)
    row = db.query_one(
        "SELECT COALESCE(SUM(credit), 0) AS c FROM daily_stats"
        " WHERE biz_date=? AND account_id=?",
        (db.today_str(), acc_id),
    )
    result["task_today"] = int((row or {}).get("c") or 0)
    return result


# ------------------------------------------------------------------ 任务 API
@app.post("/api/tasks/{task_id}/toggle")
async def toggle_task(task_id: int):
    task = db.query_one("SELECT * FROM task_defs WHERE id=?", (task_id,))
    if not task:
        return JSONResponse({"ok": False, "message": "任务不存在"}, status_code=404)
    db.execute("UPDATE task_defs SET enabled=? WHERE id=?", (0 if task["enabled"] else 1, task_id))
    mgr.reload()
    return {"ok": True, "message": "已" + ("停用" if task["enabled"] else "启用")}


@app.post("/api/tasks/{task_id}/cron")
async def update_cron(task_id: int, payload: dict = Body(...)):
    cron = str(payload.get("cron", "")).strip()
    try:
        from apscheduler.triggers.cron import CronTrigger

        CronTrigger.from_crontab(cron, timezone=TZ)
    except Exception:
        return JSONResponse({"ok": False, "message": "cron 表达式无效"}, status_code=400)
    db.execute("UPDATE task_defs SET cron=? WHERE id=?", (cron, task_id))
    mgr.reload()
    return {"ok": True, "message": "调度已更新"}


@app.post("/api/tasks/{task_id}/run")
async def trigger_task(task_id: int):
    task = db.query_one("SELECT * FROM task_defs WHERE id=?", (task_id,))
    if not task:
        return JSONResponse({"ok": False, "message": "任务不存在"}, status_code=404)
    asyncio.create_task(runner.run_task(task_id, "manual"))
    return {"ok": True, "message": "已触发，稍后刷新查看结果"}


@app.post("/api/accounts/{acc_id}/renew")
async def renew_account(acc_id: int):
    """手动续期单个账号（无条件刷新，不看剩余天数）。"""
    result = await renew.refresh_one(acc_id, force=True)
    return {"ok": bool(result.get("ok")), **result}


# ------------------------------------------------------------------ 扫码纳管
@app.post("/api/auth/start")
async def auth_start():
    """发起扫码登录，返回 state 与二维码 SVG。"""
    try:
        payload = await authflow.start()
    except ProviderError as exc:
        return JSONResponse({"ok": False, "message": exc.message}, status_code=502)
    return {"ok": True, **payload}


@app.get("/api/auth/poll")
async def auth_poll(state: str, name: str = ""):
    """轮询扫码结果；ready 时落库并返回账号信息。"""
    return {"ok": True, **await authflow.poll(state, name)}


# -------------------------------------------------- Qoder 设备码登录（手机号+验证码）
@app.post("/api/qauth/start")
async def qauth_start(payload: dict = Body(default={})):
    """发起 Qoder 登录：返回官方授权页链接（手机号+验证码在官方页完成）与二维码。"""
    realm = str((payload or {}).get("realm") or "cn").lower()
    if realm not in REALM_CHOICES:
        realm = "cn"
    try:
        payload = await qoderlogin.start(realm)
    except ProviderError as exc:
        return JSONResponse({"ok": False, "message": exc.message}, status_code=502)
    return {"ok": True, **payload}


@app.get("/api/qauth/poll")
async def qauth_poll(session: str):
    """轮询 Qoder 登录结果；ready 时落库并返回账号信息。"""
    return {"ok": True, **await qoderlogin.poll(session)}


@app.post("/api/qoder/identity")
async def import_qoder_identity(payload: dict = Body(...)):
    """接收本机客户端导出的真实机器身份并固定（设备定向活动的关键）。

    国际版每日 100 Credits 只发给账号真正登录的那台机器；云端自己生成的组件身份
    拿不到（实测：云端 1 条活动无每日，换成 PC 身份后 2 条且 CLAIMABLE 并领取成功）。
    用 `python tools/export_identity.py --realm intl --push http://<面板>` 推过来即可，
    之后的定时签到会用这份身份领取。
    """
    realm = str(payload.get("realm") or "intl").lower()
    if realm not in REALM_CHOICES:
        return JSONResponse({"ok": False, "message": "realm 只能是 cn / intl"}, status_code=400)
    uid = str(payload.get("uid") or "").strip()
    if not uid:
        row = db.query_one(
            "SELECT uid FROM accounts WHERE platform='qoder' AND realm=? AND enabled=1"
            " ORDER BY id LIMIT 1",
            (realm,),
        )
        uid = (row or {}).get("uid") or ""
    if not uid:
        return JSONResponse({"ok": False, "message": "该区域还没有 Qoder 账号"}, status_code=400)
    try:
        path = qoderumid.save_pinned(realm, uid, payload.get("identity") or {})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "message": f"身份无效：{exc}"}, status_code=400)
    db.log(0, f"已固定 Qoder {qoder_realm_name(realm)} 设备身份（{uid[:8]}…）", "INFO")
    return {"ok": True, "message": "设备身份已固定，下一次签到一起生效", "file": path.name}


# ------------------------------------------------------------------ 设置 API
@app.post("/api/settings")
async def save_settings(payload: dict = Body(...)):
    for key in ("wecom_webhook", "generic_webhook"):
        if key in payload:
            db.set_setting(key, str(payload[key]).strip())
    for key in ("notify_enabled", "notify_on_success"):
        if key in payload:
            db.set_setting(key, "1" if payload[key] in (True, "1", 1, "on") else "0")
    return {"ok": True, "message": "设置已保存"}


@app.post("/api/settings/test")
async def test_notify():
    ok, message = await notify.test()
    return {"ok": ok, "message": message}


@app.post("/api/password")
async def change_password(payload: dict = Body(...)):
    old = str(payload.get("old", ""))
    new = str(payload.get("new", ""))
    if not verify_password(old, _admin_hash()):
        return JSONResponse({"ok": False, "message": "原口令不正确"}, status_code=400)
    if len(new) < 8:
        return JSONResponse({"ok": False, "message": "新口令至少 8 位"}, status_code=400)
    db.set_setting("admin_pw_hash", hash_password(new))
    return {"ok": True, "message": "口令已更新"}


@app.get("/backup")
async def backup():
    if not DB_PATH.exists():
        raise HTTPException(404)
    name = f"checkin-{datetime.now(TZ).strftime('%Y%m%d-%H%M')}.db"
    return FileResponse(str(DB_PATH), filename=name, media_type="application/octet-stream")


# ------------------------------------------------------------------ 日志流
@app.get("/api/logs/stream")
async def log_stream(request: Request, since: int = 0):
    async def gen():
        last = since
        while True:
            if await request.is_disconnected():
                break
            rows = db.query(
                "SELECT id, ts, level, msg FROM run_logs WHERE id>? ORDER BY id LIMIT 200", (last,)
            )
            for row in rows:
                last = row["id"]
                row["time"] = datetime.fromtimestamp(row["ts"], TZ).strftime("%H:%M:%S")
                yield f"data: {json.dumps(row, ensure_ascii=False)}\n\n"
            yield ": ping\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.get("/api/summary")
async def summary():
    today = db.today_str()
    rows = db.query(
        "SELECT status, COUNT(*) c FROM runs WHERE biz_date=? GROUP BY status", (today,)
    )
    return {"biz_date": today, "by_status": {r["status"]: r["c"] for r in rows}}


# ------------------------------------------------------------------ 生命周期
@app.on_event("startup")
async def on_startup():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    db.init_db()
    _init_admin_password()
    mgr.start()


@app.on_event("shutdown")
async def on_shutdown():
    mgr.shutdown()
