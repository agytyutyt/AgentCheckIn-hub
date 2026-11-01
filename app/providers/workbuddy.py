"""WorkBuddy（copilot.tencent.com）适配器。

接口事实：
  POST /v2/billing/meter/checkin-activity-status   查询签到状态（只接受 POST）
  POST /v2/billing/meter/daily-checkin             执行签到
  POST /v2/billing/meter/get-user-resource         资源包账单（实时积分余额，基址 www.codebuddy.cn）
  GET  /activity/growth/buddy/travel/status        宠物旅行状态
  GET  /activity/growth/buddy/travel/config        可选探险地点
  POST /activity/growth/buddy/travel/depart        派出
  POST /activity/growth/buddy/travel/claim         领取旅行积分
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from .base import Provider, ProviderError, TaskResult, jwt_exp, jwt_sub, request

BASE = os.environ.get("WB_BASE_URL", "https://copilot.tencent.com").rstrip("/")
# 积分余额不在 copilot.tencent.com 上：资源包账单走 www.codebuddy.cn（= 桌面端 billing 基址）。
# 口径参照 https://github.com/ithtelab/workbuddy-manager （server/services/realm.py）
BILLING_BASE = os.environ.get("WB_BILLING_URL", "https://www.codebuddy.cn").rstrip("/")
STATUS_EP = BASE + "/v2/billing/meter/checkin-activity-status"
CHECKIN_EP = BASE + "/v2/billing/meter/daily-checkin"
RESOURCE_EP = BILLING_BASE + "/v2/billing/meter/get-user-resource"
# 扫码登录 / 令牌续期（口径见 ithtelab/workbuddy-manager 的 tencent.start_login / refresh_token）
AUTH_STATE_EP = BASE + "/v2/plugin/auth/state?platform=CLI"
AUTH_TOKEN_EP = BASE + "/v2/plugin/auth/token"
AUTH_ACCOUNT_EP = BASE + "/v2/plugin/login/account"
REFRESH_EP = BASE + "/v2/plugin/auth/token/refresh"

# CycleEndTime 是 UTC+8 墙钟，与容器时区无关
TZ8 = timezone(timedelta(hours=8))
TRAVEL_STATUS_EP = BASE + "/activity/growth/buddy/travel/status"
TRAVEL_CONFIG_EP = BASE + "/activity/growth/buddy/travel/config"
TRAVEL_DEPART_EP = BASE + "/activity/growth/buddy/travel/depart"
TRAVEL_CLAIM_EP = BASE + "/activity/growth/buddy/travel/claim"


def _headers(cred: dict) -> dict[str, str]:
    token = cred.get("accessToken", "")
    uid = cred.get("uid") or jwt_sub(token)
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "X-User-Id": uid,
        "User-Agent": "Mozilla/5.0",
    }


# 扫码登录 / 刷新走「像官方客户端」的那套头：缺 X-CodeBuddy-Request 这类风控头
# 容易在敏感接口上被拦（参考项目实测）。UA 用单段 WorkBuddy/<ver> 之外的完整形态。
_AUTH_UA = os.environ.get("WB_AUTH_UA", "WorkBuddy/2.137.1 WorkBuddy/2.137.1 CLI/2.63.2")


def _auth_headers(token: str | None = None) -> dict[str, str]:
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Accept-Language": "zh-CN",
        "X-Requested-With": "XMLHttpRequest",
        "X-CodeBuddy-Request": "1",
        "User-Agent": _AUTH_UA,
        "Origin": "https://www.codebuddy.cn",
        "Referer": "https://www.codebuddy.cn/",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _loads(status: int, text: str) -> dict:
    if status == 401:
        raise ProviderError(
            "登录态失效（401），请在 WorkBuddy 客户端重新登录后用导入器重新导出令牌",
            "need_login",
        )
    if status >= 500:
        raise ProviderError(f"上游返回 {status}", "retryable")
    try:
        return json.loads(text)
    except Exception:
        raise ProviderError(f"响应不是合法 JSON（{status}）：{text[:120]}", "retryable")


def _fmt(ts: int) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(int(ts)))
    except Exception:
        return str(ts)


def _today_credit(data: dict) -> int:
    """取「今天签到到账的积分」。

    真实状态接口（2026-10）返回 today_credit / daily_credit，没有 credit 字段；
    早期版本与 Mock 上游用的是 credit。三者按优先级兜底，避免不同版本下记成 0。
    """
    for key in ("today_credit", "daily_credit", "credit"):
        value = data.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return 0


def _as_int(value) -> int | None:
    """能转 int 就转，转不了给 None（区分「接口没给」和「给了 0」）。"""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _num(item: dict, key: str) -> float:
    """数值字段；null / 布尔 / 字符串一律按 0，不让脏数据把合计带偏。"""
    value = item.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _package_remain(item: dict) -> float:
    """单个资源包的剩余额度（口径逐条对齐上游 packageRemainUsed）。

    有周期额度（CycleCapacitySize>0）时只看 Cycle 三字段，并把 remain 钳进 [0, size]
    再用 used 反修正一次；没有周期额度时回退 Capacity 三字段。
    双向钳位不是洁癖：腾讯偶发 CycleCapacityRemain > CycleCapacitySize 的脏数据，
    只钳负值会高估余额。
    """
    size = _num(item, "CycleCapacitySize")
    if size > 0:
        remain = min(max(_num(item, "CycleCapacityRemain"), 0.0), size)
        used = size - remain
        cycle_used = _num(item, "CycleCapacityUsed")
        if cycle_used > used:
            used = cycle_used
            if size >= used:
                remain = size - used
        return remain
    return _num(item, "CapacityRemain")


def _package_expiry(item: dict) -> int | None:
    """套餐到期时刻（epoch 秒）。

    腾讯给的 CycleEndTime 是 **UTC+8 墙钟**，与容器时区无关：必须显式按 +08:00 解析，
    用本机时区（mktime / fromtimestamp）在 UTC 容器上会整体偏移 8 小时。
    """
    raw = item.get("CycleEndTime")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        naive = datetime.strptime(raw.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return int(naive.replace(tzinfo=TZ8).timestamp())


def _extract_accounts(payload) -> list | None:
    """资源包数组藏在 data.Response.Data.Accounts，但各家版本层级不完全一致，逐层下钻。"""
    cur = payload
    for _ in range(4):
        if isinstance(cur, dict):
            if isinstance(cur.get("Accounts"), list):
                return cur["Accounts"]
            for key in ("Response", "Data", "data", "response"):
                if isinstance(cur.get(key), (dict, list)):
                    cur = cur[key]
                    break
            else:
                return None
        elif isinstance(cur, list):
            return cur
        else:
            return None
    return None


class WorkBuddyProvider(Provider):
    key = "workbuddy"
    name = "WorkBuddy"
    tasks = {"checkin": "每日签到", "travel": "宠物旅行"}

    async def probe(self, cred: dict) -> dict:
        token = cred.get("accessToken", "")
        if not token:
            raise ProviderError("缺少 accessToken", "need_login")
        status, text = await request("POST", STATUS_EP, _headers(cred), {})
        body = _loads(status, text)
        if body.get("code") != 0:
            raise ProviderError(
                f"状态查询失败 code={body.get('code')} msg={body.get('msg', '')}",
                "need_login" if body.get("code") in (401, 403) else "failed",
            )
        data = body.get("data") or {}
        info = {
            "uid": cred.get("uid") or jwt_sub(token),
            "expire_at": cred.get("expire_at") or jwt_exp(token),
            "today_checked_in": bool(data.get("today_checked_in")),
            "streak_days": data.get("streak_days", 0),
            # 真实接口没有 data.credit 这个字段：当天积分叫 today_credit，
            # 老版本叫 daily_credit，两个都兜一下（否则校验信息里恒为 0）。
            "credit": _today_credit(data),
            # 活动累计（只增不减），不是可用余额；余额见 fetch_credits
            "activity_credits": _as_int(data.get("total_credits")),
        }
        # 顺带把实时余额取回来，点「测试」就能刷新到；查失败不影响凭证校验
        # （活动接口已经证明令牌可用），只把原因带回去。
        try:
            credits = await self.fetch_credits(cred)
            info["credit_balance"] = credits["balance"]
            info["credit_expires_at"] = credits["expires_at"]
            info["credit_packages"] = credits["packages"]
        except ProviderError as exc:
            info["credit_message"] = exc.message
        except Exception as exc:  # noqa: BLE001
            info["credit_message"] = f"{type(exc).__name__}: {exc}"
        return info

    async def run(self, cred: dict, task: str) -> TaskResult:
        if task == "checkin":
            return await self._checkin(cred)
        if task == "travel":
            return await self._travel(cred)
        raise ProviderError(f"未知任务 {task}")

    # ------------------------------------------------------------ 实时积分余额
    async def fetch_credits(self, cred: dict) -> dict:
        """资源包账单 → 实时可用余额。

        签到活动接口的 total_credits 是「活动累计、只增不减」，不是可用额度；
        真正的余额散在十几个资源包里，要逐个取 CycleCapacityRemain 再汇总。
        注意信封里的 TotalDosage 是额度总量（size 之和），拿它当余额会偏高。
        """
        now = time.time()
        body = {
            "PageNumber": 1,
            "PageSize": 100,
            "ProductCode": "p_tcaca",
            # 0=生效中 3=即将到期；只要还有余额的包，所以窗口往后放到 101 年兜住全部
            "Status": [0, 3],
            "PackageEndTimeRangeBegin": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "PackageEndTimeRangeEnd": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(now + 365 * 101 * 86400)
            ),
        }
        status, text = await request("POST", RESOURCE_EP, _headers(cred), body)
        payload = _loads(status, text)
        if payload.get("code") in (401, 403):
            raise ProviderError(
                f"登录态失效 code={payload.get('code')}，请重新登录客户端后重新导出令牌", "need_login"
            )
        if payload.get("code") != 0:
            raise ProviderError(
                f"积分查询失败 code={payload.get('code')} msg={payload.get('msg', '')}", "retryable"
            )

        items = _extract_accounts(payload.get("data"))
        if items is None:
            raise ProviderError("积分响应结构无法识别（资源包数组没找到）", "retryable")

        total = 0.0
        expiries: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            # 先逐个钳负值再累加，顺序不能换：先求和再钳会让一个坏套餐扣掉整份额度
            remain = max(0.0, _package_remain(item))
            total += remain
            at = _package_expiry(item) if remain > 0 else None
            if at is not None:
                expiries.append({"at": at, "amount": int(remain)})
        expiries.sort(key=lambda e: e["at"])
        return {
            "balance": int(total) if float(total).is_integer() else round(total, 2),
            "packages": len(items),
            "expiries": expiries[:5],
            "expires_at": expiries[0]["at"] if expiries else 0,
            "expiry_amount": expiries[0]["amount"] if expiries else 0,
        }

    # ------------------------------------------------------------ 扫码登录
    async def start_login(self) -> dict:
        """发起扫码登录，返回 {state, authUrl}。

        协议：POST /v2/plugin/auth/state?platform=CLI → {state, authUrl}
        authUrl 就是官方登录页地址，渲染成二维码后用手机扫码即可授权。
        """
        status, text = await request("POST", AUTH_STATE_EP, _auth_headers())
        body = _loads(status, text)
        if body.get("code") != 0:
            raise ProviderError(
                f"获取授权链接失败 code={body.get('code')} msg={body.get('msg', '')}", "retryable"
            )
        data = body.get("data") or {}
        state = str(data.get("state") or "")
        if not state:
            raise ProviderError("授权接口没有返回 state", "retryable")
        return {"state": state, "authUrl": str(data.get("authUrl") or "")}

    async def poll_login(self, state: str) -> dict:
        """轮询扫码结果：waiting / expired / ready。

        未扫码时上游返回 code=11217「login ing...」；扫码通过后返回 accessToken，
        再查 /v2/plugin/login/account 拿 uid / 昵称。
        """
        status, text = await request(
            "GET", f"{AUTH_TOKEN_EP}?state={quote(state)}", _auth_headers()
        )
        try:
            body = json.loads(text)
        except Exception:
            return {"status": "waiting", "message": "响应不是 JSON"}
        code = body.get("code")
        data = body.get("data") or {}
        if code != 0 or not data.get("accessToken"):
            msg = str(body.get("msg") or "")
            if "expire" in msg.lower() or "失效" in msg:
                return {"status": "expired", "message": msg}
            return {"status": "waiting", "message": msg}
        token = str(data["accessToken"])
        refresh = str(data.get("refreshToken") or "")
        try:
            expires_in = int(data.get("expiresIn") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600
        account: dict = {}
        try:
            s2, t2 = await request(
                "GET", f"{AUTH_ACCOUNT_EP}?state={quote(state)}", _auth_headers(token)
            )
            account = json.loads(t2).get("data") or {}
        except Exception:  # noqa: BLE001
            pass
        uid = str(account.get("uid") or "") or jwt_sub(token)
        if not uid:
            return {"status": "waiting", "message": "已授权，正在读取账号信息"}
        return {
            "status": "ready",
            "uid": uid,
            "nickname": str(account.get("nickname") or ""),
            "accessToken": token,
            "refreshToken": refresh,
            "expire_at": int(time.time()) + expires_in,
            "domain": str(data.get("domain") or ""),
        }

    # ------------------------------------------------------------ 令牌续期
    async def refresh_token(self, cred: dict) -> dict:
        """用 refreshToken 换新 accessToken，返回合并后的完整凭证。

        协议：POST /v2/plugin/auth/token/refresh，refreshToken 走 **X-Refresh-Token 头**
        （不是 body）；`X-Auth-Refresh-Source: plugin` 是官方客户端的刷新渠道标识，
        缺了可能被风控当成异常来源。expiresIn 缺省时保留旧到期时间，不要乱写。
        """
        refresh = str(cred.get("refreshToken") or "").strip()
        if not refresh:
            raise ProviderError("该账号没有 refreshToken，只能重新扫码登录", "need_login")
        headers = _headers(cred)
        headers["X-Refresh-Token"] = refresh
        headers["X-Auth-Refresh-Source"] = "plugin"
        status, text = await request("POST", REFRESH_EP, headers)
        if status >= 400:
            raise ProviderError(
                f"刷新失败（HTTP {status}）——若反复失败说明登录态已失效，需重新扫码", "need_login"
            )
        try:
            payload = json.loads(text)
        except Exception:
            raise ProviderError("刷新响应不是 JSON", "retryable")
        body = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        access = str((body or {}).get("accessToken") or "")
        if not access:
            raise ProviderError("刷新响应里没有 accessToken，需重新扫码登录", "need_login")

        merged = dict(cred)
        merged["accessToken"] = access
        if body.get("refreshToken"):
            merged["refreshToken"] = str(body["refreshToken"])
        expires_in = body.get("expiresIn")
        if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
            if 0 < expires_in < 10 * 365 * 86400:
                merged["expire_at"] = int(time.time() + expires_in)
        if isinstance(body.get("domain"), str) and body["domain"]:
            merged["domain"] = body["domain"]
        merged.setdefault("expire_at", jwt_exp(access))
        merged["uid"] = merged.get("uid") or jwt_sub(access)
        return merged

    # ------------------------------------------------------------ 签到
    async def _checkin(self, cred: dict) -> TaskResult:
        headers = _headers(cred)
        status, text = await request("POST", STATUS_EP, headers, {})
        body = _loads(status, text)
        if body.get("code") in (401, 403):
            raise ProviderError(
                f"登录态失效 code={body.get('code')}，请重新登录客户端后重新导出令牌", "need_login"
            )
        if body.get("code") != 0:
            raise ProviderError(f"状态查询失败 code={body.get('code')} msg={body.get('msg', '')}")

        data = body.get("data") or {}
        streak = data.get("streak_days", 0)
        if data.get("today_checked_in"):
            # 已签 ≠ 没有记录。状态接口同时给了当天真实到账的积分（today_credit）
            # 与累计值（total_credits），原样记下来并写进快照——否则「每天确实领到
            # 100 分」在运行记录里永远显示 0，看起来就像签到没成功。
            credit = _today_credit(data)
            total = data.get("total_credits")
            bits = []
            if credit:
                bits.append(f"今日 +{credit}")
            bits.append(f"连续 {streak} 天")
            if total is not None:
                bits.append(f"累计 {int(total)} 积分")
            return TaskResult(
                "skipped",
                f"今日已签到（{'，'.join(bits)}），无需重复执行",
                credit_snapshot=credit or None,
                detail={
                    "streak_days": streak,
                    "today_checked_in": True,
                    "today_credit": credit,
                    "total_credits": total,
                },
            )

        status, text = await request("POST", CHECKIN_EP, headers, {})
        body = _loads(status, text)
        code = body.get("code")
        if code == 0:
            d = body.get("data") or {}
            credit = _today_credit(d) if d else 0
            # 领取回执里的 total_credits 口径不可靠（实测给的是本次到账数，不是活动累计），
            # 所以回查一次状态拿权威的连签/累计，而不是把 100 当成累计显示出去。
            streak = d.get("streak_days", streak)
            total = None
            try:
                s2, t2 = await request("POST", STATUS_EP, headers, {})
                fresh = (json.loads(t2).get("data") or {}) if s2 < 500 else {}
                if fresh:
                    streak = fresh.get("streak_days", streak)
                    total = fresh.get("total_credits")
            except Exception:
                pass
            tail = f"连续 {streak} 天" + (f"，累计 {int(total)} 积分" if total is not None else "")
            # 领取成功时 credit 就是当天累计，按快照落库；重复执行取最大值不会翻倍
            return TaskResult(
                "success",
                f"签到成功 +{credit} 积分（{tail}）" if credit else f"签到成功（{tail}）",
                delta_credit=credit,
                credit_snapshot=credit,
                detail={"credit": credit, "streak_days": streak, "total_credits": total},
            )
        if code == 10001 or "已签" in str(body.get("msg", "")):
            # 并发下被别人抢先签了：回查一次状态，把当天积分补记进快照
            credit = 0
            streak2 = streak
            total2 = None
            try:
                s2, t2 = await request("POST", STATUS_EP, headers, {})
                d2 = (json.loads(t2).get("data") or {}) if s2 < 500 else {}
                credit = _today_credit(d2)
                streak2 = d2.get("streak_days", streak)
                total2 = d2.get("total_credits")
            except Exception:
                pass
            return TaskResult(
                "skipped",
                f"今日已签到（接口确认{'，今日 +%d' % credit if credit else ''}，连续 {streak2} 天）",
                credit_snapshot=credit or None,
                detail={
                    "code": code,
                    "today_credit": credit,
                    "streak_days": streak2,
                    "total_credits": total2,
                },
            )
        raise ProviderError(f"签到失败 code={code} msg={body.get('msg', '')}", "retryable")

    # ------------------------------------------------------------ 旅行
    async def _travel(self, cred: dict) -> TaskResult:
        headers = _headers(cred)
        status, text = await request("GET", TRAVEL_STATUS_EP, headers)
        body = _loads(status, text)
        if body.get("code") in (401, 403):
            raise ProviderError(
                f"登录态失效 code={body.get('code')}，请重新登录客户端后重新导出令牌", "need_login"
            )
        if body.get("code") != 0:
            raise ProviderError(f"旅行状态查询失败 code={body.get('code')} msg={body.get('msg', '')}")

        data = body.get("data") or {}
        state = data.get("state")
        limit = bool(data.get("daily_limit_reached"))
        loc = data.get("location") or {}

        if state == "traveling":
            arrive = int(data.get("arrive_at") or 0)
            server_now = int(data.get("server_now") or time.time())
            remain = max(0, arrive - server_now)
            return TaskResult(
                "skipped",
                f"宠物正在「{loc.get('name', '途中')}」探险，约 {remain // 60} 分钟后（{_fmt(arrive)}）归来",
                quiet=True,
                detail={"state": state, "arrive_at": arrive, "remain_sec": remain},
            )

        if state == "arrived":
            status, text = await request("POST", TRAVEL_CLAIM_EP, headers, {})
            body = _loads(status, text)
            code = body.get("code")
            if code == 0:
                d = body.get("data") or {}
                credit = int(
                    d.get("credit") or d.get("reward_credit") or data.get("reward_credit") or 0
                )
                return TaskResult(
                    "success",
                    f"宠物归来，领取积分 +{credit}",
                    delta_credit=credit,
                    detail={"credit": credit},
                )
            if code == 400 and "no unclaimed" in str(body.get("msg", "")).lower():
                return TaskResult("skipped", "宠物已归来，但无可领取奖励（已领过）")
            raise ProviderError(f"领取失败 code={code} msg={body.get('msg', '')}", "retryable")

        if state == "idle":
            if limit:
                return TaskResult(
                    "skipped", "宠物空闲，但今日派出次数已达上限", quiet=True, detail={"state": state}
                )
            if cred.get("auto_depart") is False:
                return TaskResult(
                    "skipped", "仅领取模式：宠物空闲，未自动派出", quiet=True, detail={"state": state}
                )

            status, text = await request("GET", TRAVEL_CONFIG_EP, headers)
            try:
                cfg = json.loads(text)
            except Exception:
                cfg = {}
            locations = (cfg.get("data") or {}).get("locations") or []
            if not locations:
                raise ProviderError("未获取到可选探险地点配置", "retryable")

            want = (cred.get("location") or "").strip()
            target = locations[0]
            if want:
                for item in locations:
                    if str(item.get("id")) == want or item.get("code") == want:
                        target = item
                        break

            status, text = await request(
                "POST", TRAVEL_DEPART_EP, headers, {"location_id": target.get("id")}
            )
            body = _loads(status, text)
            if body.get("code") == 0:
                dur = target.get("duration_hours_max") or target.get("duration_hours_min") or "?"
                return TaskResult(
                    "success",
                    f"已派出宠物前往「{target.get('name', '')}」，约 {dur} 小时后归来",
                    detail={"location": target.get("name"), "location_id": target.get("id")},
                )
            raise ProviderError(
                f"派出失败 code={body.get('code')} msg={body.get('msg', '')}", "retryable"
            )

        return TaskResult("skipped", f"未识别的宠物状态：{state}", detail={"state": state})
