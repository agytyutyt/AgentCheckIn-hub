"""端到端自测：拉起 Mock 上游 → 建账号 → 跑三个任务 → 校验结果。

运行：
    .venv/Scripts/python.exe tests/e2e.py

不依赖真实令牌，可用于部署前的冒烟验证。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


MOCK_PORT = _free_port()
os.environ["WB_BASE_URL"] = f"http://127.0.0.1:{MOCK_PORT}"
# 积分余额走独立的 billing 域名（真实环境是 www.codebuddy.cn），测试也要指到 mock
os.environ["WB_BILLING_URL"] = f"http://127.0.0.1:{MOCK_PORT}"
os.environ["QODER_BASE_URL"] = f"http://127.0.0.1:{MOCK_PORT}"
os.environ["ADMIN_PASSWORD"] = "e2e-pass-123"
TMP = Path(tempfile.mkdtemp(prefix="checkinhub-e2e-"))
os.environ["CHECKIN_DATA_DIR"] = str(TMP)
os.environ["HOST"] = "127.0.0.1"

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import credits, db, qoderumid, renew, runner, security  # noqa: E402
from app.main import app  # noqa: E402
from app.providers import qoder  # noqa: E402

results: list[tuple[bool, str]] = []

# 和 app.providers.base 一样忽略系统代理：本机开着 HTTP_PROXY 时，
# 对 127.0.0.1 的探测请求会被代理劫持（或直接解析失败），测试就假失败了。
_http = httpx.Client(trust_env=False, timeout=5.0)


def check(cond: bool, label: str) -> None:
    results.append((bool(cond), label))
    print(("  PASS  " if cond else "  FAIL  ") + label)


def start_mock() -> uvicorn.Server:
    config = uvicorn.Config("tools.mock_api:app", host="127.0.0.1", port=MOCK_PORT, log_level="error")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        try:
            _http.get(f"http://127.0.0.1:{MOCK_PORT}/demo/token", timeout=0.5)
            return server
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("mock 上游启动失败")


def main() -> int:
    server = start_mock()
    demo = _http.get(f"http://127.0.0.1:{MOCK_PORT}/demo/token").json()

    with TestClient(app) as client:
        check(client.get("/healthz").status_code == 200, "健康检查")
        check(client.get("/dashboard", follow_redirects=False).status_code == 303, "未登录跳转")
        client.post("/login", data={"username": "admin", "password": "e2e-pass-123"})
        for path in ("/dashboard", "/accounts", "/tasks", "/runs", "/logs", "/settings"):
            check(client.get(path).status_code == 200, f"页面渲染 {path}")

        r = client.post(
            "/api/accounts",
            json={"platform": "workbuddy", "name": "WB", "credential": json.dumps(demo["workbuddy"])},
        )
        check(r.json()["probe"]["ok"], "WorkBuddy 账号校验")
        r = client.post(
            "/api/accounts",
            json={"platform": "qoder", "name": "QD", "credential": json.dumps(demo["qoder"])},
        )
        check(r.json()["probe"]["ok"], "Qoder 账号校验")
        r = client.post(
            "/api/accounts", json={"platform": "qoder", "name": "缺设备头", "credential": '{"accessToken":"x"}'}
        )
        check(r.status_code == 400, "拒绝缺少 Cosy-* 的 Qoder 凭证")

        # 任务 id 按 task_key 查（种子顺序变化时不至于硬编码错位）。
        # Qoder 只有归一后的「每日签到」一个任务：领取/幂等/兜底都在它身上测。
        qd_checkin = db.query_one(
            "SELECT id FROM task_defs WHERE platform='qoder' AND task_key='checkin'"
        )["id"]

        first = asyncio.run(runner.run_task(1, "manual"))
        check(first[0]["status"] == "success" and "100" in first[0]["message"], "签到成功 +100")
        again = asyncio.run(runner.run_task(1, "manual"))
        check(again[0]["status"] == "skipped", "重复签到幂等")

        # 实时积分余额：逐套餐汇总，并且必须钳住 Remain>Size 的脏数据包
        # mock 三个包 = 400 + 100 + 100(钳位后) = 600；size 之和是 700，
        # 读错成 TotalDosage 的话这里就是 700 —— 两个数刻意不同。
        wb_acc = db.query_one("SELECT * FROM accounts WHERE platform='workbuddy'")
        snap = asyncio.run(credits.refresh(wb_acc["id"], force=True))
        check(
            snap.get("ok") and snap.get("balance") == 600,
            f"实时余额逐包汇总（实际 {snap.get('balance')}，期望 600）",
        )
        check(snap.get("packages") == 3, f"资源包计数（实际 {snap.get('packages')}）")
        check(not snap.get("cached"), "force 绕过缓存真查上游")
        cached = asyncio.run(credits.refresh(wb_acc["id"]))
        check(bool(cached.get("cached")), "TTL 内命中缓存，不重复打上游")

        # 充值 → 余额上涨 → 自动留下一条积分流水
        _http.post(f"http://127.0.0.1:{MOCK_PORT}/demo/topup")
        up = asyncio.run(credits.refresh(wb_acc["id"], force=True))
        check(up.get("balance") == 700, f"充值后余额上涨（实际 {up.get('balance')}，期望 700）")
        flow = db.query("SELECT msg FROM run_logs WHERE msg LIKE '积分余额%'")
        check(len(flow) == 1 and "+100" in flow[0]["msg"], f"余额上涨记流水（{len(flow)} 条）")

        # ---- 今日新增 = 官方余额差（余额 − 当日开盘），不是任务累计 ----
        check(
            isinstance(up.get("today_delta"), int) and isinstance(up.get("open_balance"), int),
            f"余额接口给出官方差额口径（today_delta={up.get('today_delta')}）",
        )
        # 首次读数把当前余额当开盘值 → 充值后差额应等于 +100
        check(
            up.get("today_delta") == 100,
            f"今日新增=官方余额差（实际 {up.get('today_delta')}，期望 100）",
        )
        single = client.post(f"/api/accounts/{wb_acc['id']}/credits").json()
        check(
            isinstance(single.get("today_delta"), int) and isinstance(single.get("task_today"), int),
            f"单账号刷新返回 today_delta/task_today（{single.get('today_delta')}/{single.get('task_today')}）",
        )
        check(
            "null" not in json.dumps(single.get("today_delta")),
            "单账号刷新不会给出 null（+null bug 回归）",
        )

        # ---- Qoder 实时余额：quota/usage 的 userQuota + addOnQuota 两段求和 ----
        qd_acc = db.query_one("SELECT * FROM accounts WHERE platform='qoder' AND realm='cn'")
        qbal = asyncio.run(credits.refresh(qd_acc["id"], force=True))
        check(
            qbal.get("ok") and qbal.get("balance") == 400,
            f"Qoder 实时余额两段求和（实际 {qbal.get('balance')}，期望 400）",
        )

        depart = asyncio.run(runner.run_task(2, "manual"))
        check(depart[0]["status"] == "success", "派出宠物")
        _http.post(f"http://127.0.0.1:{MOCK_PORT}/demo/advance")
        claim = asyncio.run(runner.run_task(2, "manual"))
        check(claim[0]["status"] == "success" and "+6" in claim[0]["message"], "宠物归来领取 +6")

        # 令牌临期 → 自动刷新并回写
        acc = db.query_one("SELECT * FROM accounts WHERE platform='qoder'")
        cred = security.decrypt_json(acc["cred_enc"])
        old_rt = cred["refreshToken"]
        cred["expire_at"] = int(time.time()) - 10
        db.execute(
            "UPDATE accounts SET cred_enc=?, token_expire_at=? WHERE id=?",
            (security.encrypt_json(cred), cred["expire_at"], acc["id"]),
        )
        asyncio.run(runner.run_task(qd_checkin, "manual"))
        new = security.decrypt_json(db.query_one("SELECT * FROM accounts WHERE id=?", (acc["id"],))["cred_enc"])
        check(new["refreshToken"] != old_rt and new["expire_at"] > time.time(), "令牌轮换并落盘")

        # 失效令牌 → need_login，且不应浪费重试
        wb = db.query_one("SELECT * FROM accounts WHERE platform='workbuddy'")
        c2 = security.decrypt_json(wb["cred_enc"])
        c2["accessToken"] = "bad-token"
        db.execute("UPDATE accounts SET cred_enc=? WHERE id=?", (security.encrypt_json(c2), wb["id"]))
        bad = asyncio.run(runner.run_task(1, "manual"))
        check(bad[0]["status"] == "need_login", "失效令牌判定为需登录")
        check(not db.query("SELECT * FROM retry_queue WHERE status='pending'"), "需登录不排重试")

        # 上游 5xx → 可重试 + 进补偿队列
        c2["accessToken"] = "boom-token"
        db.execute("UPDATE accounts SET cred_enc=? WHERE id=?", (security.encrypt_json(c2), wb["id"]))
        boom = asyncio.run(runner.run_task(1, "manual"))
        check(boom[0]["status"] == "retryable", "5xx 判定为可重试")
        check(len(db.query("SELECT * FROM retry_queue WHERE status='pending'")) >= 1, "可重试进入补偿队列")

        # ---- 扫码纳管：未扫 → waiting；扫过 → 自动建号并校验 ----
        start = client.post("/api/auth/start").json()
        check(
            start.get("ok") and start.get("state") and str(start.get("qr", "")).lstrip().startswith("<svg"),
            "扫码：拿到 state 与二维码 SVG",
        )
        p1 = client.get("/api/auth/poll", params={"state": start["state"]}).json()
        check(p1.get("status") == "waiting", f"扫码：未扫码时为等待（{p1.get('status')}）")
        check(
            client.get("/api/auth/poll", params={"state": "不存在"}).json().get("status") == "invalid",
            "扫码：未知 state 判为失效",
        )
        _http.post(f"http://127.0.0.1:{MOCK_PORT}/demo/scan")
        p2 = client.get("/api/auth/poll", params={"state": start["state"]}).json()
        check(
            p2.get("status") == "ready" and p2.get("action") == "已添加",
            f"扫码：授权后自动建号（{p2.get('status')}/{p2.get('action')}）",
        )
        scanned = db.query_one("SELECT * FROM accounts WHERE uid='mock-uid-1'")
        check(bool(scanned and scanned["platform"] == "workbuddy"), "扫码：账号已落库")
        scred = security.decrypt_json(scanned["cred_enc"]) if scanned else {}
        check(bool(scred.get("refreshToken")), "扫码：refreshToken 一并保存")
        again = client.get("/api/auth/poll", params={"state": start["state"]}).json()
        check(again.get("status") == "invalid", "扫码：用过的 state 不能复用")

        # ---- 令牌续期：剩余 < 3 天自动刷新，未到期则跳过 ----
        # 用扫码进来的账号（带 refreshToken）；手工粘贴的演示号只有 accessToken，
        # 那条路单独断言「提示需重新扫码」。
        scan_acc = db.query_one("SELECT * FROM accounts WHERE uid='mock-uid-1'")
        c3 = security.decrypt_json(scan_acc["cred_enc"])
        old_access, old_refresh = c3["accessToken"], c3["refreshToken"]
        c3["expire_at"] = int(time.time()) + 2 * 86400
        db.execute(
            "UPDATE accounts SET cred_enc=?, token_expire_at=? WHERE id=?",
            (security.encrypt_json(c3), c3["expire_at"], scan_acc["id"]),
        )
        check(renew.needs_renew(c3["expire_at"]), "续期：剩 2 天应进入续期窗口")
        rr = asyncio.run(renew.refresh_one(scan_acc["id"]))
        check(rr.get("ok") and not rr.get("skipped"), f"续期：自动刷新成功（{rr.get('message')}）")
        c4 = security.decrypt_json(
            db.query_one("SELECT * FROM accounts WHERE id=?", (scan_acc["id"],))["cred_enc"]
        )
        check(c4["accessToken"] != old_access, "续期：accessToken 已更换")
        check(c4["refreshToken"] != old_refresh, "续期：refreshToken 已轮换并保存")
        check(c4["expire_at"] > time.time() + 80 * 86400, "续期：到期时间已顺延")
        skip = asyncio.run(renew.refresh_one(scan_acc["id"]))
        check(bool(skip.get("skipped")), "续期：未到期时跳过（不无谓打上游）")
        check(renew.needs_renew(0) is False, "续期：无到期时间时不臆测")

        # 没有 refreshToken 的账号：说清「只能重新扫码」，而不是反复重试
        demo_acc = db.query_one(
            "SELECT * FROM accounts WHERE platform='workbuddy' AND uid!='mock-uid-1'"
        )
        c5 = security.decrypt_json(demo_acc["cred_enc"])
        c5.pop("refreshToken", None)
        c5["expire_at"] = int(time.time()) + 86400
        db.execute(
            "UPDATE accounts SET cred_enc=?, token_expire_at=? WHERE id=?",
            (security.encrypt_json(c5), c5["expire_at"], demo_acc["id"]),
        )
        nr = asyncio.run(renew.refresh_one(demo_acc["id"]))
        check(bool(nr.get("need_login")), f"续期：无 refreshToken 时提示需重新扫码（{nr.get('message')}）")

        # ---- Qoder 每日签到：由「每天领 100 积分 / Credits」活动承接 ----
        _http.post(f"http://127.0.0.1:{MOCK_PORT}/demo/campaign_reset")
        _http.post(f"http://127.0.0.1:{MOCK_PORT}/demo/claim_mode", params={"mode": "ok"})
        res = asyncio.run(runner.run_task(qd_checkin, "manual"))
        check(
            res[0]["status"] == "success" and "签到成功 +100 积分" in res[0]["message"],
            f"Qoder CN 签到由活动承接（{res[0]['message']}）",
        )
        # delta 取「官方余额实际到账」，不是活动面额（run 记录里落的就是这个值）
        rdelta = db.query_one(
            "SELECT delta_credit FROM runs WHERE account_id=? AND task_def_id=?"
            " ORDER BY id DESC LIMIT 1",
            (qd_acc["id"], qd_checkin),
        )
        check(
            (rdelta or {}).get("delta_credit") == 100,
            f"签到 delta=实际到账（实际 {(rdelta or {}).get('delta_credit')}，期望 100）",
        )
        res2 = asyncio.run(runner.run_task(qd_checkin, "manual"))
        check(
            res2[0]["status"] == "skipped" and "今日已签到" in res2[0]["message"],
            f"Qoder 签到重复幂等（{res2[0]['message']}）",
        )

        # ---- 领取结果分类（口径同 qoder-trae-workbuddy-switch interpret_claim）----
        _http.post(f"http://127.0.0.1:{MOCK_PORT}/demo/campaign_reset")
        _http.post(f"http://127.0.0.1:{MOCK_PORT}/demo/claim_mode", params={"mode": "replay"})
        r409 = asyncio.run(runner.run_task(qd_checkin, "manual"))
        check(
            r409[0]["status"] == "skipped" and "今日已签到" in r409[0]["message"],
            f"409 幂等重放算已领（{r409[0]['status']}/{r409[0]['message'][:24]}）",
        )
        _http.post(f"http://127.0.0.1:{MOCK_PORT}/demo/campaign_reset")
        _http.post(f"http://127.0.0.1:{MOCK_PORT}/demo/claim_mode", params={"mode": "same"})
        rsame = asyncio.run(runner.run_task(qd_checkin, "manual"))
        check(
            rsame[0]["status"] == "skipped" and "同人已领取" in rsame[0]["message"],
            f"同人已领不算失败（{rsame[0]['status']}/{rsame[0]['message'][:20]}）",
        )
        _http.post(f"http://127.0.0.1:{MOCK_PORT}/demo/campaign_reset")
        _http.post(f"http://127.0.0.1:{MOCK_PORT}/demo/claim_mode", params={"mode": "stock"})
        rstock = db.query_one("SELECT 1")
        rstock = asyncio.run(runner.run_task(qd_checkin, "manual"))
        check(
            rstock[0]["status"] in ("retryable", "failed")
            and "名额已发完" in rstock[0]["message"],
            f"名额发完带中文原因（{rstock[0]['status']}/{rstock[0]['message'][:30]}）",
        )
        _http.post(f"http://127.0.0.1:{MOCK_PORT}/demo/claim_mode", params={"mode": "ok"})
        # 活动列表必须带客户端同款查询参数：forceRefresh 才能绕过服务端缓存拿到当日活动
        # （国际版「永远待下发」的根因就是少了它 —— 服务端回缓存视图）
        lq = _http.get(f"http://127.0.0.1:{MOCK_PORT}/demo/last_campaigns_query").json()
        check(
            "clientType=10" in lq.get("query", "")
            and "forceRefresh=true" in lq.get("query", ""),
            f"活动列表带 clientType=10&forceRefresh=true（实际 {lq.get('query')!r}）",
        )

        # ---- 区域账号池：国际版独立成池，同一路径不同基址，互不干扰 ----
        intl = client.post(
            "/api/accounts",
            json={
                "platform": "qoder",
                "name": "Intl 池",
                "realm": "intl",
                "credential": json.dumps(demo["qoder"]),
            },
        ).json()
        check(intl.get("ok"), f"国际版账号添加（{intl.get('message')}）")
        acc_intl = db.query_one("SELECT * FROM accounts WHERE realm='intl'")
        check(bool(acc_intl), "国际版账号池已建立")
        ibal = asyncio.run(credits.refresh(acc_intl["id"], force=True))
        cn_now = asyncio.run(credits.refresh(qd_acc["id"], force=True))["balance"]
        # 两个池指向同一上游账号余额口径：国际版读到的数必须与国内版一致
        #（领取会让余额上涨，所以不能写死 400，比对同一时刻的国内版读数）
        check(
            ibal.get("ok") and ibal.get("balance") == cn_now,
            f"国际版账号余额与上游一致（实际 {ibal.get('balance')}，国内 {cn_now}）",
        )
        cn_bal = db.query_one(
            "SELECT credit_total FROM accounts WHERE id=?", (qd_acc["id"],)
        )["credit_total"]
        check(cn_bal == cn_now, "国内 / 国际池各记各的余额，互不覆盖")

        # ---- 设备身份固定：国际版每日活动是「设备定向」的，云端必须用真实客户端身份 ----
        fake_ident = {
            "machineToken": "P1gTESTtokenForRegression0123456789",
            "machineCode": "0123456789abcdef",
            "machineType": "abcdef0123456789",
        }
        imp = client.post(
            "/api/qoder/identity",
            json={"realm": "intl", "uid": acc_intl["uid"], "identity": fake_ident},
        ).json()
        check(bool(imp.get("ok")), f"设备身份可导入（{imp.get('message')}）")
        pinned = asyncio.run(qoderumid.prepare("intl", acc_intl["uid"]))
        check(
            bool(pinned) and pinned.get("machineToken") == fake_ident["machineToken"],
            "固定身份优先生效（不再跑本地组件）",
        )
        bad = client.post(
            "/api/qoder/identity",
            json={"realm": "intl", "uid": acc_intl["uid"], "identity": {"machineToken": "x"}},
        )
        check(bad.status_code == 400, f"残缺身份被拒绝（HTTP {bad.status_code}）")

        # ---- 自愈 1：身份不被认可时，自动换另一套身份重试并领取 ----
        # 场景：固定身份（BAD）拿不到每日活动，云端组件身份（GOOD）能拿到。
        _http.post(f"http://127.0.0.1:{MOCK_PORT}/demo/campaign_reset")
        _http.post(f"http://127.0.0.1:{MOCK_PORT}/demo/claim_mode", params={"mode": "ok"})
        _http.post(f"http://127.0.0.1:{MOCK_PORT}/demo/identity_gate",
                   params={"token": "GOOD-TOKEN"})
        orig_alt = qoderumid.alternate

        async def _fake_alt(realm, uid):
            return "component", {"machineToken": "GOOD-TOKEN",
                                 "machineCode": "code-good", "machineType": "type-good"}

        qoderumid.alternate = _fake_alt
        try:
            heal = asyncio.run(runner.run_task(qd_checkin, "manual"))
            check(
                any("自愈" in (r.get("message") or "") for r in heal),
                "身份自愈：换身份后领到（"
                + (next((r.get("message") or "" for r in heal
                         if "自愈" in (r.get("message") or "")), "无"))[:40] + "）",
            )
        finally:
            qoderumid.alternate = orig_alt
            _http.post(f"http://127.0.0.1:{MOCK_PORT}/demo/identity_gate",
                       params={"token": ""})

        # ---- 自愈 2：领取时 401（令牌刚过期）→ 就地刷新令牌后重试成功 ----
        _http.post(f"http://127.0.0.1:{MOCK_PORT}/demo/campaign_reset")
        _http.post(f"http://127.0.0.1:{MOCK_PORT}/demo/claim_401_once", params={"on": 1})
        heal2 = asyncio.run(runner.run_task(qd_checkin, "manual"))
        check(
            any("签到成功" in (r.get("message") or "") for r in heal2),
            "领取 401 自愈：刷新令牌后重试成功（"
            + (heal2[0].get("message") or "")[:36] + "）",
        )

        # Qoder 余额上涨 → 记流水（与 WorkBuddy 各一条）
        pre_topup = asyncio.run(credits.refresh(qd_acc["id"], force=True))["balance"]
        _http.post(f"http://127.0.0.1:{MOCK_PORT}/demo/qtopup")
        upq = asyncio.run(credits.refresh(qd_acc["id"], force=True))
        check(
            upq.get("balance") == pre_topup + 50,
            f"Qoder 充值后余额 +50（实际 {upq.get('balance')}，期望 {pre_topup + 50}）",
        )
        flow = db.query("SELECT msg FROM run_logs WHERE msg LIKE '积分余额%'")
        # 每次余额上涨都记一条：WorkBuddy 充值、Qoder 签到到账（可能多笔）、Qoder 充值
        check(len(flow) >= 3, f"余额上涨都记流水（实际 {len(flow)} 条，至少 3）")
        # 流水里带账号名：两个平台都要出现过（用库里真实名字，不写死）
        wb_names = [r["name"] for r in db.query(
            "SELECT name FROM accounts WHERE platform='workbuddy'")]
        qd_names = [r["name"] for r in db.query(
            "SELECT name FROM accounts WHERE platform='qoder'")]
        check(
            any(any(n and n in row["msg"] for n in wb_names) for row in flow)
            and any(any(n and n in row["msg"] for n in qd_names) for row in flow),
            "两个平台都有余额流水",
        )

        # ---- 活动平台整体不可达：国内版回退 sash 老接口；国际版如实失败（无兜底）----
        _http.post(f"http://127.0.0.1:{MOCK_PORT}/demo/campaign_mode", json={"mode": "down"})
        await_ = time.sleep(1.5)   # 让上一个 run 的「同任务互斥锁」释放，避免假跳过
        res3 = asyncio.run(runner.run_task(qd_checkin, "manual"))
        check(
            res3[0]["status"] == "success" and "签到成功 +20 积分" in res3[0]["message"],
            f"活动平台故障时国内版 sash 兜底（{res3[0]['message']}）",
        )
        check(
            res3[1]["status"] in ("failed", "retryable"),
            f"国际版在活动平台故障时不走 sash 兜底（{res3[1]['status']}）",
        )
        _http.post(f"http://127.0.0.1:{MOCK_PORT}/demo/campaign_mode", json={"mode": "ok"})

        # ---- 基址候选：国内单基址；国际版必须能降级到备用域名 ----
        saved_base = os.environ.pop("QODER_BASE_URL", None)
        try:
            cn_bases = qoder.candidates({"realm": "cn"})
            intl_bases = qoder.candidates({"realm": "intl"})
            default_bases = qoder.candidates({})
        finally:
            if saved_base is not None:
                os.environ["QODER_BASE_URL"] = saved_base
        check(cn_bases == ["https://openapi.qoder.com.cn"], f"国内版基址（{cn_bases}）")
        check(
            intl_bases[0] == "https://openapi.qoder.sh"
            and "https://center.qoder.sh" in intl_bases
            and len(intl_bases) >= 2,
            f"国际版多基址可降级（{intl_bases}）",
        )
        check(default_bases == cn_bases, "无 realm 的老账号按国内版处理")

        # ---- 命名：国际版 Qoder / 国内版 Qoder CN ----
        check(
            qoder.realm_name("intl") == "Qoder" and qoder.realm_name("cn") == "Qoder CN",
            f"区域命名（{qoder.realm_name('intl')} / {qoder.realm_name('cn')}）",
        )

        # ---- 设备码登录：官方授权页（手机号+验证码在官方页完成）→ 轮询建号 ----
        qst = client.post("/api/qauth/start", json={"realm": "intl"}).json()
        check(
            qst.get("ok")
            and "qoder.com/device/selectAccounts" in qst.get("url", "")
            and "challenge=" in qst.get("url", "")
            and "nonce=" in qst.get("url", "")
            and str(qst.get("qr", "")).lstrip().startswith("<svg"),
            "Qoder 登录：拿到官方授权页链接与二维码",
        )
        qp1 = client.get("/api/qauth/poll", params={"session": qst["session"]}).json()
        check(qp1.get("status") == "pending", "Qoder 登录：未授权时为等待")
        _http.post(f"http://127.0.0.1:{MOCK_PORT}/demo/device_scan")
        qp2 = client.get("/api/qauth/poll", params={"session": qst["session"]}).json()
        check(
            qp2.get("status") == "ready" and qp2.get("action") == "已添加",
            f"Qoder 登录：授权后自动建号（{qp2.get('status')}/{qp2.get('action')}）",
        )
        dacc = db.query_one("SELECT * FROM accounts WHERE uid='device-uid-1'")
        check(
            bool(dacc and dacc["platform"] == "qoder" and dacc["realm"] == "intl"),
            "Qoder 登录：账号入国际池",
        )
        dcred = security.decrypt_json(dacc["cred_enc"]) if dacc else {}
        check(
            bool(dcred.get("refreshToken")) and (dcred.get("device") or {}).get("clientType") == "10",
            "Qoder 登录：凭证含 refreshToken 与设备标识",
        )
        qp3 = client.get("/api/qauth/poll", params={"session": qst["session"]}).json()
        check(qp3.get("status") == "invalid", "Qoder 登录：会话一次性")

        page = client.get("/dashboard").text
        check("今日任务" in page and "成功" in page, "总览卡片渲染")
        check(
            "WorkBuddy 积分余额" in page and "Qoder 积分余额" in page,
            "总览余额按平台拆分展示",
        )
        check(
            "官方余额差" in page and "任务累计" in page,
            "总览标注今日新增的口径来源",
        )
        total = db.query_one("SELECT SUM(credit) c FROM daily_stats")["c"]
        # 汇总口径会随「第几次真领到」变化（签到成功几次就记几笔），所以只钉下限：
        #   100(wb签到) + 6(旅行) + 100(Qoder 签到到账) + 20(sash 兜底) = 226
        # 断下限足以拦住「聚合坏掉/不记账」，又不会因为多跑一次领取就假红。
        check(total >= 226, f"积分汇总已记账（实际 {total}，至少 226）")

    server.should_exit = True
    shutil.rmtree(TMP, ignore_errors=True)

    passed = sum(1 for ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
