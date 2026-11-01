"""本地 Mock 上游：用于在没有真实令牌的情况下跑通全链路。

启动：
    python -m uvicorn tools.mock_api:app --port 8899
然后：
    WB_BASE_URL=http://127.0.0.1:8899 QODER_BASE_URL=http://127.0.0.1:8899 python -m app.serve

Mock 行为：
  - WorkBuddy 签到：当天首次调用 +100，再次调用返回已签
  - 宠物旅行：idle →（depart）→ traveling →（25 秒后）→ arrived →（claim）+6
  - Qoder：/campaigns 返回一个 CLAIMABLE；claim 后变 CLAIMED
"""
from __future__ import annotations

import base64
import json
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="CheckinHub Mock Upstream")


def _jwt(uid: str, ttl: int = 86400 * 30) -> str:
    def enc(obj: dict) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
        return raw

    return f"{enc({'alg':'none','typ':'JWT'})}.{enc({'sub':uid,'exp':int(time.time())+ttl})}.sig"


STATE: dict = {
    "checked": {},
    "streak": 3,
    "travel": {"state": "idle", "depart_at": 0, "claimed_today": False},
    "campaign": {"status": "CLAIMABLE", "amount": 100},
    # 扫码登录：默认「未扫码」，/demo/scan 置为已扫
    "auth_scanned": False,
    # 令牌刷新计数（每次刷新换一个新的 accessToken）
    "refresh_seq": 0,
    # Qoder sash 每日签到状态 + 实时余额（quota/usage）
    "sash_checked": {},
    "sash_streak": 2,
    # "ok" = 有 sash 签到接口；"missing" = 模拟国际版的 404
    "checkin_mode": "ok",
    # "ok" = 活动平台正常；"down" = 模拟活动平台 5xx（验证签到任务的 sash 兜底）
    "campaign_mode": "ok",
    # 设备码登录：默认未授权，/demo/device_scan 置为已授权
    "device_scanned": False,
    "device_seq": 0,
    "quota": {"userQuota": {"remaining": 300, "used": 100, "total": 400},
              "addOnQuota": {"remaining": 100, "used": 0, "total": 100}},
    # 资源包账单（实时余额的来源）。三个包分别覆盖三种口径：
    #   1) 有周期额度 → 取 CycleCapacityRemain
    #   2) 没有周期额度 → 回退 CapacityRemain
    #   3) Remain > Size 的脏数据 → 必须钳到 Size，否则余额虚高
    # 合计应为 400 + 100 + 100 = 600，而 size 之和是 700——两个数故意不同，
    # 用来证明代码读的是余额而不是 TotalDosage。
    "packages": [
        {
            "PackageName": "月度套餐",
            "CycleCapacitySize": 500,
            "CycleCapacityRemain": 400,
            "CycleCapacityUsed": 100,
            "CycleEndTime": "2099-01-01 00:00:00",
        },
        {
            "PackageName": "永久包",
            "CapacitySize": 100,
            "CapacityRemain": 100,
            "CapacityUsed": 0,
        },
        {
            "PackageName": "脏数据包",
            "CycleCapacitySize": 100,
            "CycleCapacityRemain": 110,
            "CycleCapacityUsed": 0,
            "CycleEndTime": "2099-01-01 00:00:00",
        },
    ],
}


@app.post("/v2/billing/meter/checkin-activity-status")
async def wb_status(request: Request):
    auth = request.headers.get("authorization", "")
    if "boom" in auth:
        return JSONResponse({"code": 500, "msg": "upstream exploded"}, status_code=500)
    if "bad" in auth:
        return {"code": 401, "msg": "token invalid"}
    day = time.strftime("%Y-%m-%d")
    return {
        "code": 0,
        "data": {
            "today_checked_in": STATE["checked"].get(day, False),
            "streak_days": STATE["streak"],
            "credit": 100,
            # 真实接口的字段名：当天到账叫 today_credit / daily_credit，没有 credit；
            # total_credits 是「活动累计」，与签到回执里同名不同义的那个数不是一回事
            "today_credit": 100,
            "daily_credit": 100,
            "total_credits": STATE["streak"] * 100,
        },
    }


@app.post("/v2/billing/meter/daily-checkin")
async def wb_checkin():
    day = time.strftime("%Y-%m-%d")
    if STATE["checked"].get(day):
        return {"code": 10001, "msg": "今天已签到，请明天再来"}
    STATE["checked"][day] = True
    STATE["streak"] += 1
    # 领取回执里的 total_credits 实测是「本次到账」（100），不是活动累计。
    # 代码不能拿它当累计显示，必须回查状态接口——这条就是为那个坑存在的。
    return {"code": 0, "data": {"credit": 100, "streak_days": STATE["streak"], "total_credits": 100}}


@app.post("/demo/topup")
async def demo_topup():
    """给第一个资源包充值 100，用来验证「余额上涨 → 记一条流水」。"""
    pkg = STATE["packages"][0]
    pkg["CycleCapacityRemain"] = min(pkg["CycleCapacitySize"], pkg["CycleCapacityRemain"] + 100)
    pkg["CycleCapacityUsed"] = pkg["CycleCapacitySize"] - pkg["CycleCapacityRemain"]
    return {"code": 0, "data": {"remain": pkg["CycleCapacityRemain"]}}


@app.post("/v2/billing/meter/get-user-resource")
async def wb_user_resource():
    """资源包账单 → 实时余额。

    照抄真实信封层级（data.Response.Data.Accounts）与 TotalDosage 口径：它是各包
    size 之和，**不是**余额，故意做成不同的数，防止代码偷懒读它。
    第三个包给了 CycleCapacityRemain > Size 的脏数据，用来验证钳位。
    """
    packages = STATE["packages"]
    total_dosage = sum(
        p.get("CycleCapacitySize") or p.get("CapacitySize") or 0 for p in packages
    )
    return {
        "code": 0,
        "msg": "OK",
        "data": {
            "Response": {
                "Data": {
                    "TotalCount": len(packages),
                    "TotalDosage": total_dosage,
                    "Accounts": packages,
                }
            }
        },
    }


@app.post("/v2/plugin/auth/state")
async def wb_auth_state():
    state = "mock-state-1"
    return {
        "code": 0,
        "data": {
            "state": state,
            "authUrl": f"https://copilot.tencent.com/login?platform=CLI&state={state}",
        },
    }


@app.get("/v2/plugin/auth/token")
async def wb_auth_token(state: str = ""):
    if not STATE["auth_scanned"]:
        return {"code": 11217, "msg": "login ing..."}
    return {
        "code": 0,
        "data": {
            "accessToken": "mock-access-scanned",
            "refreshToken": "mock-refresh-1",
            "expiresIn": 90 * 86400,
        },
    }


@app.get("/v2/plugin/login/account")
async def wb_auth_account(state: str = ""):
    return {"code": 0, "data": {"uid": "mock-uid-1", "nickname": "扫码账号"}}


@app.post("/demo/scan")
async def demo_scan():
    """模拟「用户已扫码并在手机上确认」。"""
    STATE["auth_scanned"] = True
    return {"code": 0}


@app.post("/v2/plugin/auth/token/refresh")
async def wb_refresh_token():
    STATE["refresh_seq"] += 1
    seq = STATE["refresh_seq"]
    return {
        "code": 0,
        "data": {
            "accessToken": f"mock-access-refreshed-{seq}",
            "refreshToken": f"mock-refresh-{seq + 1}",
            "expiresIn": 90 * 86400,
        },
    }


@app.get("/activity/growth/buddy/travel/status")
async def wb_travel_status():
    t = STATE["travel"]
    if t["state"] == "traveling" and time.time() - t["depart_at"] > 25:
        t["state"] = "arrived"
    return {
        "code": 0,
        "data": {
            "state": t["state"],
            "daily_limit_reached": False,
            "arrive_at": int(t["depart_at"] + 25) if t["state"] == "traveling" else 0,
            "server_now": int(time.time()),
            "location": {"name": "咖啡馆"},
            "reward_credit": 6,
        },
    }


@app.get("/activity/growth/buddy/travel/config")
async def wb_travel_config():
    return {
        "code": 0,
        "data": {
            "locations": [
                {"id": 1, "code": "coffee", "name": "咖啡馆", "duration_hours_min": 2, "duration_hours_max": 2},
                {"id": 2, "code": "mall", "name": "商场", "duration_hours_min": 3, "duration_hours_max": 3},
            ]
        },
    }


@app.post("/activity/growth/buddy/travel/depart")
async def wb_travel_depart(request: Request):
    body = await request.json()
    STATE["travel"] = {"state": "traveling", "depart_at": time.time(), "claimed_today": False, "loc": body}
    return {"code": 0, "data": {"location_id": body.get("location_id")}}


@app.post("/activity/growth/buddy/travel/claim")
async def wb_travel_claim():
    if STATE["travel"]["state"] != "arrived":
        return {"code": 400, "msg": "no unclaimed reward"}
    STATE["travel"]["state"] = "idle"
    return {"code": 0, "data": {"credit": 6}}


@app.get("/api/v2/quota/usage")
async def qd_quota():
    """实时余额（扁平结构，无 code/data 信封）：余额 = 两段 remaining 之和。

    userQuota.total(400) + addOnQuota.total(100) = 500 是「额度上限」，
    remaining 之和才是余额 400 —— 两个数故意不同，防止读错字段。
    """
    return STATE["quota"]


@app.get("/sash/api/v1/me/daily-check-in/status")
async def qd_checkin_status():
    if STATE["checkin_mode"] == "missing":
        # 国际版实测没有这套接口：让 mock 也能复现，用于验证「明确跳过」分支
        return JSONResponse({"code": 404, "msg": "not found"}, status_code=404)
    day = time.strftime("%Y-%m-%d")
    claimed = STATE["sash_checked"].get(day)
    return {
        "data": {
            "status": "CLAIMED" if claimed else "CLAIMABLE",
            "currentStreakDays": STATE["sash_streak"],
            "rewardCredits": 20,
            "lastClaimedAt": int(time.time()) if claimed else None,
        }
    }


@app.post("/demo/checkin_mode")
async def demo_checkin_mode(request: Request):
    body = await request.json()
    STATE["checkin_mode"] = str((body or {}).get("mode") or "ok")
    return {"code": 0, "mode": STATE["checkin_mode"]}


@app.post("/sash/api/v1/me/daily-check-in/claim")
async def qd_checkin_claim():
    day = time.strftime("%Y-%m-%d")
    if STATE["sash_checked"].get(day):
        return JSONResponse({"error": "ALREADY_CLAIMED"}, status_code=409)
    STATE["sash_checked"][day] = True
    STATE["sash_streak"] += 1
    return {"success": True, "data": {"rewardCredits": 20, "currentStreakDays": STATE["sash_streak"]}}


@app.post("/demo/qtopup")
async def demo_qtopup():
    """给 Qoder 的 addOnQuota 充 50，验证「余额上涨 → 记流水」。"""
    add = STATE["quota"]["addOnQuota"]
    add["remaining"] += 50
    add["total"] += 50
    return {"code": 0, "data": {"remaining": add["remaining"]}}


@app.get("/sash/api/v1/me/campaigns")
async def qoder_campaigns(request: Request):
    # 记录查询串：e2e 用它断言「客户端同款」的 forceRefresh=true 确实发出去了
    STATE["last_campaigns_query"] = request.url.query
    if STATE["campaign_mode"] == "down":
        return JSONResponse({"code": 500, "msg": "campaign platform down"}, status_code=503)
    if not request.headers.get("cosy-machinetoken"):
        return {"code": 400, "msg": "missing device headers"}
    # 身份门禁：只有携带「被认可」的设备身份才下发每日活动（模拟服务端按身份过滤，
    # 用于回归「身份自愈」：换一套身份后应该能领到）
    gate = STATE.get("identity_gate") or ""
    if gate and request.headers.get("cosy-machinetoken") != gate:
        return {
            "code": 0,
            "data": {
                "showCampaign": True,
                "claimable": False,
                "campaigns": [
                    {"id": "cmp-marketing", "actionType": "VIEW_DETAILS",
                     "claimStatus": "CLAIMED", "campaignKey": "act-marketing-001"},
                ],
            },
        }
    now = int(time.time() * 1000)
    return {
        "code": 0,
        "data": {
            "campaigns": [
                {
                    "id": "cmp-001",
                    "actionType": "CLAIM_BENEFIT",
                    "claimStatus": STATE["campaign"]["status"],
                    "benefits": [{"amount": STATE["campaign"]["amount"]}],
                    "startAt": now - 3600_000,
                    "endAt": now + 86400_000,
                },
                # 一次性营销活动：领完只剩 VIEW_DETAILS 且金额为空（国际版实测形态），
                # 用来验证「已领」判定不会把它当成今日已签
                {
                    "id": "cmp-view-1",
                    "actionType": "VIEW_DETAILS",
                    "claimStatus": "CLAIMED",
                    "benefits": [{"amount": None, "kind": None}],
                    "startAt": now - 8 * 86400_000,
                    "endAt": now + 86400_000,
                },
            ]
        },
    }


@app.post("/demo/campaign_mode")
async def demo_campaign_mode(request: Request):
    body = await request.json()
    STATE["campaign_mode"] = str((body or {}).get("mode") or "ok")
    return {"code": 0, "mode": STATE["campaign_mode"]}


@app.post("/demo/campaign_reset")
async def demo_campaign_reset():
    STATE["campaign"]["status"] = "CLAIMABLE"
    STATE["campaign"]["amount"] = 100
    return {"code": 0}


@app.get("/demo/last_campaigns_query")
async def demo_last_campaigns_query():
    """最近一次活动列表请求的查询串（e2e 断言 forceRefresh=true 用）。"""
    return {"code": 0, "query": STATE.get("last_campaigns_query", "")}


@app.get("/api/v1/deviceToken/poll")
async def qoder_device_poll(nonce: str = "", verifier: str = "", challenge_method: str = "S256"):
    if not STATE["device_scanned"]:
        return JSONResponse({"code": 202, "msg": "authorization pending"}, status_code=202)
    return {
        "token": "mock-access-device",
        "refresh_token": "mock-refresh-device",
        "user_id": "device-uid-1",
        "expires_in": 86400 * 30,
    }


@app.get("/api/v1/userinfo")
async def qoder_userinfo(request: Request):
    if not request.headers.get("authorization", "").startswith("Bearer "):
        return JSONResponse({"code": 401}, status_code=401)
    return {"name": "设备登录号", "email": "device@example.com"}


@app.post("/demo/device_scan")
async def demo_device_scan():
    """模拟「用户已在官方授权页完成手机号验证码登录并确认授权」。"""
    STATE["device_scanned"] = True
    return {"code": 0}


@app.post("/sash/api/v1/me/campaigns/{cid}/claim")
async def qoder_claim(cid: str):
    """领取。支持三种真实上游形态（用 /demo/claim_mode 切换）：

    ok     → 200 {status: CLAIMED}，并且**余额真的增加**（面额 100）
    replay → HTTP 409（官方幂等：今天已领过）
    same   → 200 {status: BLOCKED, failureCode: SAME_PERSON_ALREADY_CLAIMED}
    stock  → 200 {status: NOT_ELIGIBLE, failureCode: REDEMPTION_CODE_OUT_OF_STOCK}
    """
    mode = STATE.get("claim_mode", "ok")
    if STATE.get("claim_401_pending"):
        STATE["claim_401_pending"] = False
        return JSONResponse({"errorCode": "UNAUTHORIZED"}, status_code=401)
    if mode == "replay":
        return JSONResponse({"errorCode": "ALREADY_CLAIMED"}, status_code=409)
    if mode == "same":
        return {
            "code": 0,
            "status": "BLOCKED",
            "failureCode": "SAME_PERSON_ALREADY_CLAIMED",
            "benefit": {"kind": "CREDITS", "amount": 100},
        }
    if mode == "stock":
        return {
            "code": 0,
            "status": "NOT_ELIGIBLE",
            "failureCode": "REDEMPTION_CODE_OUT_OF_STOCK",
        }
    amount = 100
    if STATE["campaign"]["status"] != "CLAIMED":
        STATE["campaign"]["status"] = "CLAIMED"
        # 真上游领到就是余额上涨：addOnQuota 加面额，e2e 才能验证「delta=实际到账」
        add = STATE["quota"]["addOnQuota"]
        add["remaining"] += amount
        add["total"] += amount
    return {
        "code": 0,
        "data": {"status": "CLAIMED", "replayed": False,
                 "benefit": {"kind": "CREDITS", "amount": amount}},
    }


@app.post("/demo/claim_mode")
async def demo_claim_mode(mode: str = "ok"):
    """切换领取响应形态：ok / replay / same / stock。"""
    STATE["claim_mode"] = mode
    return {"code": 0, "mode": mode}


@app.post("/demo/identity_gate")
async def demo_identity_gate(token: str = ""):
    """只给指定 cosy-machinetoken 下发每日活动（空串=不设门禁）。"""
    STATE["identity_gate"] = token
    return {"code": 0, "gate": token}


@app.post("/demo/claim_401_once")
async def demo_claim_401_once(on: int = 1):
    """让下一次领取先返回 401（回归「领取时令牌过期 → 自愈刷新重试」）。"""
    STATE["claim_401_pending"] = bool(on)
    return {"code": 0, "on": bool(on)}


@app.post("/api/v1/deviceToken/refresh")
async def qoder_refresh(request: Request):
    body = await request.json()
    rt = body.get("refresh_token")
    if not rt:
        return {"code": 401, "msg": "no refresh token"}
    return {"code": 0, "data": {"token": _jwt("qoder-demo", 86400 * 60), "refreshToken": rt + "-r2"}}


@app.get("/demo/token")
async def demo_token():
    return {
        "workbuddy": {"accessToken": _jwt("wb-demo-uid"), "uid": "wb-demo-uid"},
        "qoder": {
            "accessToken": _jwt("qoder-demo"),
            "refreshToken": "demo-refresh",
            "uid": "qoder-demo",
            "device": {
                "clientType": "10",
                "machineOS": "windows",
                "machineHostname": "DEMO-PC",
                "machineId": "demo-machine-id",
                "machineToken": "demo-machine-token",
                "machineCode": "demo-code",
                "machineType": "desktop",
                "version": "0.9.0",
            },
        },
    }


@app.post("/demo/advance")
async def demo_advance():
    """把宠物状态推进到 arrived，便于测试领取分支。"""
    if STATE["travel"]["state"] == "traveling":
        STATE["travel"]["state"] = "arrived"
    return {"state": STATE["travel"]["state"]}
