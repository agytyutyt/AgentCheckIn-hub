"""Qoder 适配器：国内版 openapi.qoder.com.cn / 国际版 openapi.qoder.sh。

接口事实（逆向自官方桌面端，口径参照 shuishuipingan/qoder2api-hub 的
qoder_accounts.py）：
  GET  /api/v2/quota/usage                        实时余额：userQuota + addOnQuota（扁平结构，无信封）
  GET  /api/v2/user/plan                          套餐名
  GET  /sash/api/v1/me/campaigns                  活动列表（双区域通用）
  POST /sash/api/v1/me/campaigns/{cid}/claim      领取奖励
  GET  /sash/api/v1/me/daily-check-in/status      每日签到状态（仅国内版；国际版 404）
  POST /sash/api/v1/me/daily-check-in/claim       每日签到领取（仅国内版）
  POST /api/v1/deviceToken/refresh                刷新令牌（保活，双区域通用）

区域差异（关键，别混）：
  · 基址：国内 openapi.qoder.com.cn，国际 openapi.qoder.sh（官方备选 api1/2/3.qoder.sh）
  · 国际版没有 /sash/api/v1/me/daily-check-in/*（实测 404），「每日领取 100 Credits」
    由活动平台（campaigns）承接 —— 本适配器里 credit 任务就是活动平台，双区域通用
  · 业务路径相同，只有基址不同；同一份 Cosy-* 设备头两边都用

账号在添加时选择区域（cred["realm"] = "cn"|"intl"，缺省 cn），入库 accounts.realm，
两个区域各自独立成池、互不干扰。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from datetime import datetime

from .. import qoderumid
from .base import Provider, ProviderError, TaskResult, jwt_exp, jwt_sub, request

# 区域常量（逆向自官方桌面客户端）。显示名：国际版叫 Qoder，国内版叫 Qoder CN
REALMS: dict[str, dict] = {
    "cn": {
        "name": "Qoder CN",
        "openapi": ["https://openapi.qoder.com.cn"],
        "domain": "qoder.com.cn",
    },
    "intl": {
        "name": "Qoder",
        # 国际版 openapi.qoder.sh 在部分网络/时期不可达（官方客户端自带 api1/2/3
        # 故障切换域名）。候选按「主 → center.qoder.sh → 备用域名」排列，
        # 只在连接失败与 404 时降级，业务错误不换基址。
        "openapi": [
            "https://openapi.qoder.sh",
            "https://center.qoder.sh",
            "https://openapi.qoder.com",
        ],
        "domain": "qoder.com",
    },
}

# 业务路径（双区域相同，只有基址不同）
P_QUOTA = "/api/v2/quota/usage"
P_PLAN = "/api/v2/user/plan"
P_CAMPAIGNS = "/sash/api/v1/me/campaigns"
# ★ 活动列表必须带这两个查询参数（客户端日志 2026-10-30 实证）：
#   clientType=10   桌面端形态
#   forceRefresh=true  绕过服务端缓存、强制重算下发
# 不带 forceRefresh 时服务端返回**缓存视图** —— 明明客户端里能领，接口却只回
# 「2 条已领的营销活动」，这正是「国际版永远待下发」的根因。客户端自己也是
# 缓存 300s + 用 forceRefresh 强制刷新（日志：expiresInMs≈298087）。
P_CAMPAIGNS_QUERY = "?clientType=10&forceRefresh=true"

# 上游失败码 → 中文原因（口径同 qoder-trae-workbuddy-switch 的 FAILURE_REASONS）。
# 这只是**展示**表，不是判据：判据永远是 status / claimStatus / actionType。
FAILURE_REASONS = {
    "REDEMPTION_CODE_OUT_OF_STOCK": "今日名额已发完（每日 10:00 刷新，次日再来）",
    "ACHIEVEMENT_NOT_COMPLETED": "需先完成新人任务（成就未完成）",
    "CAMPAIGN_NOT_ACTIVE": "活动已结束 / 未开始",
    "RISK_BLOCKED": "风控拦截（当前设备 / 网络不可领取）",
    "RISK_DEPENDENCY_UNAVAILABLE": "风控服务不可用，稍后重试",
    "SAME_PERSON_ALREADY_CLAIMED": "同人已领取（同一身份下其他账号本轮已领）",
}
P_CAMPAIGN_CLAIM = "/sash/api/v1/me/campaigns/%s/claim"
P_CHECKIN_STATUS = "/sash/api/v1/me/daily-check-in/status"
P_CHECKIN_CLAIM = "/sash/api/v1/me/daily-check-in/claim"
P_DEVICE_REFRESH = "/api/v1/deviceToken/refresh"
# 设备码登录（PKCE + nonce，与官方 qodercli 同流程）
P_DEVICE_POLL = "/api/v1/deviceToken/poll"
P_USERINFO = "/api/v1/userinfo"
LOGIN_URL = {"cn": "https://qoder.cn/device/selectAccounts", "intl": "https://qoder.com/device/selectAccounts"}

MARGIN_H = int(os.environ.get("QODER_REFRESH_MARGIN_H", "12"))

DEVICE_FIELDS = [
    ("clientType", "Cosy-ClientType", "10"),
    ("machineOS", "Cosy-MachineOS", ""),
    ("machineHostname", "Cosy-MachineHostname", ""),
    ("machineId", "Cosy-MachineId", ""),
    ("machineToken", "Cosy-MachineToken", ""),
    ("machineCode", "Cosy-MachineCode", ""),
    ("machineType", "Cosy-MachineType", ""),
    ("version", "Cosy-Version", ""),
]


def realm_of(cred: dict) -> str:
    """账号所属区域。老库凭据没有 realm 字段，按国内版处理（与历史行为一致）。"""
    value = str(cred.get("realm") or "").lower()
    return value if value in REALMS else "cn"


def realm_name(realm: str) -> str:
    return REALMS.get(realm, REALMS["cn"])["name"]


def candidates(cred: dict) -> list[str]:
    """该账号的 openapi 基址候选（按优先级）。

    QODER_BASE_URL 是测试 / 私有部署的统一覆盖（命中即独占，不再试别的）；
    否则 QODER_CN_URL / QODER_INTL_URL 优先，其次内置候选。
    """
    override = os.environ.get("QODER_BASE_URL", "").strip()
    if override:
        return [override.rstrip("/")]
    realm = realm_of(cred)
    env = os.environ.get("QODER_INTL_URL" if realm == "intl" else "QODER_CN_URL", "").strip()
    out = [env.rstrip("/")] if env else []
    for base in REALMS[realm]["openapi"]:
        base = str(base).rstrip("/")
        if base and base not in out:
            out.append(base)
    return out


def _base(cred: dict) -> str:
    """该账号的首选基址（日志与展示用）。"""
    bases = candidates(cred)
    return bases[0] if bases else ""


# 上次成功的基址（按区域记）：国际版主域名偶尔抽风时，下一次直接走能用的那个，
# 不让每个任务都先白等一次超时。
_BASE_CACHE: dict[str, str] = {}

# ---- 自愈状态（进程内）----
# 身份来源偏好：自愈成功换源后记住，下次先用能用的那个
_IDENTITY_PREF: dict[tuple[str, str], str] = {}
# 身份自愈冷却：同一账号 N 秒内只换一次源，避免每轮都双倍请求上游
_FAILOVER_AT: dict[tuple[str, str], float] = {}
FAILOVER_COOLDOWN = 10 * 60


def _intl_proxy() -> str:
    """国际版专用出口（QODER_INTL_PROXY）：按出口 IP 识别地区/风控，机房直连
    可能拿不到每日活动视图，给代理就能走干净出口。国内版/WorkBuddy 不受影响。"""
    return os.environ.get("QODER_INTL_PROXY", "").strip()


def _no_daily_campaign_hint(body: dict) -> bool:
    """上游是否明确表示「这次不展示活动」（showCampaign=false）。

    qoder-trae-workbuddy-switch 的判据：showCampaign=false 通常意味着身份被判定为
    非官方客户端 —— 那种情况换身份有意义；而 showCampaign=true 但列表里没有每日
    活动，多半是「今天还没下发」（活动可能到 11:00 才发），换身份是白费一次请求。
    """
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    if not isinstance(data, dict):
        return False
    return data.get("showCampaign") is False


async def _log_self_heal(realm: str, uid: str, source: str, status: str) -> None:
    from .. import db

    label = "本机客户端身份" if source == "pinned" else "云端组件身份"
    try:
        db.log(
            0,
            f"Qoder 身份自愈：{realm} 账号 {uid[:8]}… 换用{label}后可用（{status}）",
            "INFO",
        )
    except Exception:  # noqa: BLE001
        pass


def _expiry_epoch(value) -> int:
    """把刷新接口的 expires_at（ISO 字符串 / 毫秒 / 秒）统一成秒级时间戳。"""
    if isinstance(value, str) and value.strip():
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except Exception:  # noqa: BLE001
            return 0
    if isinstance(value, (int, float)) and value:
        return int(value / 1000) if value > 1e11 else int(value)
    return 0


async def _api(
    cred: dict,
    path: str,
    method: str = "GET",
    body: dict | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, str]:
    """按候选基址请求，返回 (status, text)。

    只在**连接层失败**与 404/405/410 时换下一个基址：401/403 说明地址是对的、
    只是这次被拒（令牌/出口问题），换基址既无意义，还可能把请求打到另一个区域去。
    全部候选都 404 时返回最后一次的 404——调用方据此判断「本区域没有这个接口」。
    """
    bases = candidates(cred)
    realm = realm_of(cred)
    prefer = _BASE_CACHE.get(realm)
    if prefer and prefer in bases:
        bases = [prefer] + [b for b in bases if b != prefer]
    hdrs = _headers(cred) if headers is None else headers
    proxy = _intl_proxy() if realm == "intl" else ""
    last_status, last_text, last_err = 0, "", ""
    for base in bases:
        try:
            status, text = await request(method, base + path, hdrs, body, proxy=proxy)
        except Exception as exc:  # noqa: BLE001 连接层失败：换下一个基址
            last_err = f"{type(exc).__name__}: {exc}"
            continue
        if status in (404, 405, 410):
            last_status, last_text = status, text
            continue
        _BASE_CACHE[realm] = base
        return status, text
    if last_status:
        return last_status, last_text
    raise ProviderError(
        f"连不上 Qoder 接口（已试 {len(bases)} 个基址）：{last_err}", "retryable"
    )


def _headers(cred: dict) -> dict[str, str]:
    """请求头：桌面端 createAuthorizedHeaders 同款（qoderumid 组装）+ 凭证令牌。

    国际版的「每天领 100 Credits」只对这套完整头下发（X-*/cosy-machine* 六头，
    风控身份来自官方 runtime-info组件）。Authorization 永远现取 —— 令牌轮换后
    不能被身份缓存顶住。组件不可用时退回凭证里的设备字段（老导入路径）。
    """
    realm = realm_of(cred)
    uid = str(cred.get("uid") or "")
    headers = {"Content-Type": "application/json"}
    headers.update(qoderumid.peek(realm, uid))
    token = cred.get("accessToken", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if "cosy-machinetoken" not in headers:
        # 无原生身份时的兜底：凭证里的设备字段（一键导入的完整设备头）
        device = cred.get("device") or {}
        for key, header, default in DEVICE_FIELDS:
            if header in headers:
                continue
            value = device.get(key, "")
            headers[header] = str(value if value not in (None, "") else default)
    return headers


def _loads(status: int, text: str) -> dict:
    if status == 401:
        raise ProviderError("Qoder 令牌失效（401），请重新导出凭证", "need_login")
    if status >= 500:
        raise ProviderError(f"上游返回 {status}", "retryable")
    try:
        return json.loads(text)
    except Exception:
        raise ProviderError(f"响应不是合法 JSON（{status}）：{text[:120]}", "retryable")


def _campaign_list(payload: dict) -> list[dict]:
    data = payload.get("data", payload)
    if isinstance(data, dict):
        for key in ("campaigns", "list", "items", "records"):
            if isinstance(data.get(key), list):
                return data[key]
    if isinstance(data, list):
        return data
    return []


def _claim_id(c: dict) -> str:
    for key in ("id", "campaignId", "cid", "campaign_id"):
        if c.get(key):
            return str(c[key])
    return ""


def _claim_status(c: dict) -> str:
    return str(c.get("claimStatus") or c.get("status") or "").upper()


def _amount(c: dict) -> int:
    """奖励面额：顶层字段在前，其次 benefit/benefits 蓝子（官方字段是 benefit.amount）。"""
    for key in ("credit", "amount", "rewardAmount", "rewardCredits"):
        if c.get(key) is not None:
            try:
                return int(c[key])
            except Exception:
                pass
    for basket in ("benefits", "benefit", "rewards"):
        val = c.get(basket)
        if isinstance(val, list) and val:
            return _amount(val[0]) if isinstance(val[0], dict) else 0
        if isinstance(val, dict):
            return _amount(val)
    return 0


def _num(d: dict, key: str) -> float:
    """数值字段：null / 布尔 / 字符串一律按 0（上游给过 isQuotaExceeded + 全 0 的退化体）。"""
    value = (d or {}).get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


class QoderProvider(Provider):
    key = "qoder"
    name = "Qoder"
    tasks = {"credit": "每日 Credits", "checkin": "每日签到"}

    @staticmethod
    async def _risk(cred: dict) -> None:
        """任务入口统一取一次风控身份（结果进缓存供 _headers 读取）。

        带上「上次自愈成功用过的来源」作为偏好：若之前云端组件身份能领到而固定身份
        领不到，下次直接用组件身份，不必每轮都先撞一次墙。
        """
        try:
            realm = realm_of(cred)
            uid = str(cred.get("uid") or "")
            await qoderumid.prepare(realm, uid, prefer=_IDENTITY_PREF.get((realm, uid), ""))
        except Exception:  # noqa: BLE001  身份失败不阻断任务
            pass

    async def probe(self, cred: dict) -> dict:
        cred, refreshed = await self._ensure_token(cred)
        await self._risk(cred)
        status, text = await _api(cred, P_CAMPAIGNS + P_CAMPAIGNS_QUERY)
        if status == 401 and cred.get("refreshToken") and not refreshed:
            cred, _ = await self._refresh(cred)
            status, text = await _api(cred, P_CAMPAIGNS + P_CAMPAIGNS_QUERY)
        body = _loads(status, text)
        items = _campaign_list(body)
        claimable = [c for c in items if _claim_status(c) == "CLAIMABLE"]
        info = {
            "uid": cred.get("uid") or jwt_sub(cred.get("accessToken", "")),
            "expire_at": cred.get("expire_at") or jwt_exp(cred.get("accessToken", "")),
            "realm": realm_of(cred),
            "realm_name": realm_name(realm_of(cred)),
            "campaigns": len(items),
            "claimable": len(claimable),
            "cred_update": cred if refreshed else None,
        }
        # 顺带把实时余额取回来；查失败不影响凭证校验
        try:
            bal = await self.fetch_credits(cred)
            info["credit_balance"] = bal.get("balance")
        except Exception as exc:  # noqa: BLE001
            info["credit_message"] = f"{type(exc).__name__}: {exc}"
        return info

    async def run(self, cred: dict, task: str) -> TaskResult:
        if task == "credit":
            return await self._credit(cred)
        if task == "checkin":
            return await self._checkin(cred)
        raise ProviderError(f"未知任务 {task}")

    # ---------------------------------------------- 每日领取（活动平台，双区域通用）
    async def _credit(self, cred: dict) -> TaskResult:
        cred, refreshed = await self._ensure_token(cred)
        await self._risk(cred)
        status, text = await _api(cred, P_CAMPAIGNS + P_CAMPAIGNS_QUERY)
        if status == 401 and cred.get("refreshToken") and not refreshed:
            cred, _ = await self._refresh(cred)
            refreshed = True
            status, text = await _api(cred, P_CAMPAIGNS + P_CAMPAIGNS_QUERY)
        body = _loads(status, text)
        return await self._campaign_result(cred, body, refreshed, checkin=False)

    async def _checkin(self, cred: dict) -> TaskResult:
        """每日签到：两个区域都由「每天领 100」活动承接（官方业务日 10:00 UTC+8 刷新）。

        Qoder CN → 每天领 100 积分；Qoder（国际版）→ 每天领 100 Credits（必须带设备
        风控身份，见 qoderumid）。活动平台整体不可达时，国内版回退老的 sash
        daily-check-in（个别老账号仍有效）；国际版没有这套接口（实测 404），不兜底。
        """
        cred, refreshed = await self._ensure_token(cred)
        await self._risk(cred)
        realm = realm_of(cred)
        try:
            status, text = await _api(cred, P_CAMPAIGNS + P_CAMPAIGNS_QUERY)
            if status in (404, 405, 410):
                # 参考项目 issue #66：单次 404 常是网关抖动，而「本区没有活动平台」
                # 是会触发兜底/放弃的强结论 —— 必须原样重发一次才能认。
                await asyncio.sleep(1)
                status, text = await _api(cred, P_CAMPAIGNS + P_CAMPAIGNS_QUERY)
            if status == 401 and cred.get("refreshToken") and not refreshed:
                cred, _ = await self._refresh(cred)
                refreshed = True
                status, text = await _api(cred, P_CAMPAIGNS + P_CAMPAIGNS_QUERY)
            body = _loads(status, text)
        except ProviderError as exc:
            if realm == "cn" and exc.status in ("retryable", "failed"):
                return await self._sash_checkin(cred, refreshed)
            raise

        result = await self._campaign_result(cred, body, refreshed, checkin=True)
        # ---- 自愈：活动没下发/看不到时，换一套身份再试一次 ----
        # 口径同 qoder-trae-workbuddy-switch（showCampaign=false 时 force 重取身份）与
        # qoder2api-hub（身份被判定为非官方客户端就换身份）。我们的特殊之处是
        # 「固定身份 ⇄ 云端组件身份」互为备份，换源成功后记住偏好，下次直接用能用的那个。
        if result.status == "pending" and not _no_daily_campaign_hint(body):
            healed = await self._heal_identity(cred, refreshed)
            if healed is not None:
                return healed
        return result

    async def _heal_identity(self, cred: dict, refreshed: bool) -> TaskResult | None:
        """身份自愈：换另一个身份来源重取活动列表，能领就直接领掉。

        只在「列表里没有可领的每日活动」时触发，且同一账号 10 分钟内只换一次源
        （避免每轮双倍请求上游）。换源成功 → 记住偏好；两边都没有 → 返回 None，
        由调用方保持原来的 pending 结论（绝不谎报）。
        """
        realm = realm_of(cred)
        uid = str(cred.get("uid") or "")
        key = (realm, uid)
        now = time.time()
        if now - _FAILOVER_AT.get(key, 0) < FAILOVER_COOLDOWN:
            return None
        _FAILOVER_AT[key] = now
        try:
            alt = await qoderumid.alternate(realm, uid)
        except Exception:  # noqa: BLE001
            return None
        if not alt:
            return None
        source, ident = alt
        headers = _headers(cred)
        headers.update(qoderumid.headers_for(realm, uid, ident))
        try:
            status, text = await _api(cred, P_CAMPAIGNS + P_CAMPAIGNS_QUERY, headers=headers)
            body = _loads(status, text)
        except Exception:  # noqa: BLE001
            return None
        result = await self._campaign_result(
            cred, body, refreshed, checkin=True, headers=headers
        )
        if result.status in ("success", "skipped"):
            _IDENTITY_PREF[key] = source
            label = "本机客户端身份" if source == "pinned" else "云端组件身份"
            await _log_self_heal(realm, uid, source, result.status)
            result.message += f"（已自愈切换为{label}）"
            result.detail = {**(result.detail or {}), "identity_healed": source}
            return result
        return None

    async def _campaign_result(
        self, cred: dict, body: dict, refreshed: bool, *, checkin: bool,
        headers: dict | None = None,
    ) -> TaskResult:
        """把活动列表变成任务结论（credit / checkin 两个任务共用）。"""
        update = cred if refreshed else None
        realm = realm_of(cred)
        unit = "积分" if (checkin and realm == "cn") else "Credits"
        items = _campaign_list(body)
        if not items:
            return TaskResult(
                "pending",
                "活动列表为空，可能尚未下发（每日活动约 10:00 开放），下轮再试",
                detail={"campaigns": 0, "realm": realm},
                cred_update=update,
            )

        targets = [
            c
            for c in items
            if str(c.get("actionType", "")).upper() == "CLAIM_BENEFIT"
            and _claim_status(c) == "CLAIMABLE"
            and _claim_id(c)
        ]
        if not targets:
            # 「已领」的判据与 qoder2api-hub 同源（issue #25）：官方在领取后会把
            # actionType 降级成 VIEW_DETAILS，只有带奖励面额或仍是可领类型的才算已领。
            # 国际版的一次性营销活动领完只剩 VIEW_DETAILS 且金额为空，那不算「今日已签」。
            already = [
                c
                for c in items
                if _claim_status(c) == "CLAIMED"
                and (_amount(c) > 0 or str(c.get("actionType", "")).upper() in ("", "CLAIM_BENEFIT"))
            ]
            if already:
                total = sum(_amount(c) for c in already)
                message = (
                    f"今日已签到（活动已领取，合计 {total} {unit}，10:00 后刷新）"
                    if checkin
                    else f"今日已领取（{len(already)} 项，合计 {total} Credits）"
                )
                return TaskResult(
                    "skipped",
                    message,
                    credit_snapshot=total or None,
                    detail={"claimed": len(already), "credit": total, "already": True},
                    cred_update=update,
                )
            return TaskResult(
                "pending",
                f"暂无可领取活动（共 {len(items)} 项），下轮再试"
                + (
                    "；国际版每日活动按「设备定向」下发，若客户端里能看到而这里没有，"
                    "多半是设备身份不是账号登录的那台机器"
                    "（本机跑 tools/sync_qoder_identity.bat 同步真实身份）"
                    if realm == "intl"
                    else ""
                ),
                detail={"campaigns": len(items), "realm": realm},
                cred_update=update,
            )

        return await self._claim_targets(
            cred, targets, update, checkin=checkin, unit=unit, headers=headers
        )

    async def _claim_targets(
        self, cred: dict, targets: list[dict], update: dict | None,
        *, checkin: bool, unit: str, headers: dict | None = None,
    ) -> TaskResult:
        """逐个领取，并按官方口径分类结果。

        判据照抄 qoder-trae-workbuddy-switch 的 `interpret_claim`（2026-10-31 实测对齐）：
          · HTTP 409 / errorCode ALREADY_CLAIMED / REPLAYED ⇒ **已领取**，不是失败
          · failureCode SAME_PERSON_ALREADY_CLAIMED 或 status BLOCKED ⇒ **同人已领**
            （服务端按「人/设备」去重：同机其它账号本轮领过，这个号就不给）
          · 已知失败码 ⇒ 带中文原因的**非致命**结果（名额发完 / 成就未完成 / 风控 …）
          · status=CLAIMED 且没有 redemptionCode ⇒ 发放「确认中」
        并且**领取前后各查一次官方余额**：delta 取实际到账，而不是活动面额
        （国际版实测出现过「返回 CLAIMED 但余额没动」，面额不能当到账）。
        """
        ok_count, face, real, errors = 0, 0, 0, []
        already_today, blocked = False, False
        before = await self._balance_quiet(cred)

        for c in targets:
            cid = _claim_id(c)
            label = str(c.get("campaignKey") or cid)[:22]
            st, tx = await _api(cred, P_CAMPAIGN_CLAIM % cid, "POST", {}, headers=headers)
            if st == 401 and cred.get("refreshToken"):
                # 自愈：领取时 401 先就地换令牌再试一次（不把「令牌刚好过期」判成需登录）
                try:
                    cred = await self._refresh(cred)
                    update = cred
                    # 头里带着旧 Authorization，必须用新 cred 重建（保留自愈换来的 cosy-*）
                    new_headers = _headers(cred)
                    if headers:
                        new_headers.update(
                            {k: v for k, v in headers.items()
                             if k.lower().startswith("cosy-")}
                        )
                    headers = new_headers
                    st, tx = await _api(
                        cred, P_CAMPAIGN_CLAIM % cid, "POST", {}, headers=headers
                    )
                except ProviderError:
                    raise ProviderError(
                        "Qoder 令牌失效（401）且刷新失败，请重新导出凭证", "need_login"
                    )
            if st == 401:
                raise ProviderError("Qoder 令牌失效（401），请重新导出凭证", "need_login")
            if st == 409:                     # 上游幂等：今天已经领过
                already_today = True
                await asyncio.sleep(1)
                continue
            try:
                resp = json.loads(tx) if tx.strip() else {}
            except Exception:  # noqa: BLE001
                errors.append(f"{label}: 非 JSON 响应")
                await asyncio.sleep(1)
                continue
            data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
            status = str(data.get("status") or "").upper()
            code = str(
                data.get("failureCode") or resp.get("errorCode") or resp.get("code") or ""
            ).upper()

            # 顺序很重要：SAME_PERSON_ALREADY_CLAIMED 里也含 "ALREADY"，
            # 必须先判「同人已领」，否则会被误判成幂等重放而吞掉原因。
            if code == "SAME_PERSON_ALREADY_CLAIMED" or status == "BLOCKED":
                blocked = True
            elif code in ("ALREADY_CLAIMED", "REPLAYED") or "ALREADY" in code:
                already_today = True
            elif code in FAILURE_REASONS:
                errors.append(f"{label}: {FAILURE_REASONS[code]}")
            elif st == 200 and status in ("CLAIMED", "GRANTED", "SUCCESS"):
                ok_count += 1
                face += _amount(c)
            else:
                errors.append(f"{label}: {data.get('msg') or status or f'HTTP {st}'}")
            await asyncio.sleep(1)

        if ok_count:
            after = await self._balance_quiet(cred)
            # 官方余额差就是「真正到账」；查不到余额时退回面额（不虚报，注明口径）
            real = (after - before) if (before is not None and after is not None) else face
            detail = {
                "claimed": ok_count,
                "face": face,
                "credited": real,
                "errors": errors,
            }
            if errors:
                detail["errors"] = errors
            note = ""
            if before is not None and after is not None and real < face:
                note = f"，官方发放确认中（面额 {face}，当前到账 {real}）"
            message = (
                f"签到成功 +{real} {unit}（每日领取活动）{note}"
                if checkin
                else f"领取 {ok_count} 项活动，+{real} Credits{note}"
            )
            if errors:
                message += f"；{len(errors)} 项失败"
            return TaskResult(
                "success",
                message,
                delta_credit=real,
                detail=detail,
                cred_update=update,
            )
        if blocked and not already_today:
            return TaskResult(
                "skipped",
                "同人已领取（同一身份下的其他账号本轮已领，服务端按人去重；10:00 后刷新）",
                detail={"blocked": True, "errors": errors},
                cred_update=update,
            )
        if already_today or blocked:
            return TaskResult(
                "skipped",
                "今日已签到（活动已领取，10:00 后刷新）" if checkin else "今日已领取（活动平台确认）",
                detail={"already": True, "errors": errors},
                cred_update=update,
            )
        raise ProviderError(f"领取失败：{'; '.join(errors)[:200]}", "retryable")

    async def _balance_quiet(self, cred: dict) -> int | None:
        """查一次官方余额；失败返回 None（不能因为余额查询拖垮领取结论）。"""
        try:
            info = await self.fetch_credits(cred)
            return int(info.get("balance") or 0)
        except Exception:  # noqa: BLE001
            return None

    # ---------------------------------------------- sash 每日签到（老接口，仅国内版兜底）
    async def _sash_checkin(self, cred: dict, refreshed: bool) -> TaskResult:
        """老 sash 签到接口：先查状态，未签则领取。仅在国内版、活动平台不可达时兜底。"""
        update = cred if refreshed else None
        realm = realm_of(cred)

        status, text = await _api(cred, P_CHECKIN_STATUS)
        if status in (404, 405, 410):
            return TaskResult(
                "skipped",
                f"{realm_name(realm)}未开放每日签到接口（HTTP {status}），每日领取由活动承接",
                quiet=True,
                detail={"realm": realm, "unavailable": True},
                cred_update=update,
            )
        if status == 401 and cred.get("refreshToken") and not refreshed:
            cred, _ = await self._refresh(cred)
            update = cred
            status, text = await _api(cred, P_CHECKIN_STATUS)
        body = _loads(status, text)
        st = body.get("data") if isinstance(body.get("data"), dict) else body
        st = st or {}
        flag = str(st.get("status") or "")
        streak = int(st.get("currentStreakDays") or 0)
        reward = int(st.get("rewardCredits") or 0)

        if flag == "CLAIMED" and st.get("lastClaimedAt"):
            last = time.strftime("%Y-%m-%d", time.localtime(int(st["lastClaimedAt"])))
            if last == time.strftime("%Y-%m-%d"):
                return TaskResult(
                    "skipped",
                    f"今日已签到（连续 {streak} 天）",
                    detail={"realm": realm, "streak_days": streak, "already": True},
                    cred_update=update,
                )
        if flag and flag not in ("CLAIMABLE", "CLAIMED"):
            # DISABLED 等：活动批次下线，不发起无意义的 claim
            return TaskResult(
                "skipped",
                f"官方签到活动未开放（status={flag}）",
                detail={"realm": realm, "status": flag},
                cred_update=update,
            )

        status, text = await _api(cred, P_CHECKIN_CLAIM, "POST", {})
        if status == 401:
            raise ProviderError("Qoder 令牌失效（401），请重新导出凭证", "need_login")
        try:
            resp = json.loads(text)
        except Exception:
            raise ProviderError("签到响应不是 JSON", "retryable")
        payload = resp.get("data") if isinstance(resp.get("data"), dict) else resp
        # 上游并发/重复领取返回 409 ALREADY_CLAIMED —— 归一化为「已签」
        if status == 409 or "ALREADY_CLAIMED" in str(payload or "").upper():
            return TaskResult(
                "skipped",
                f"今日已签到（连续 {streak} 天）",
                detail={"realm": realm, "already": True},
                cred_update=update,
            )
        if resp.get("success") is False:
            # 复查一次：上游可能已记账（并发双跑）
            status, text = await _api(cred, P_CHECKIN_STATUS)
            try:
                st2 = (json.loads(text).get("data") or {}) if status < 500 else {}
            except Exception:
                st2 = {}
            if str(st2.get("status") or "") == "CLAIMED":
                return TaskResult(
                    "skipped",
                    f"今日已签到（复查确认，连续 {int(st2.get('currentStreakDays') or 0)} 天）",
                    detail={"realm": realm, "already": True},
                    cred_update=update,
                )
            raise ProviderError(
                f"签到失败：{str(resp.get('error') or resp)[:160]}", "retryable"
            )
        got = int(payload.get("rewardCredits") or reward or 0)
        days = int(payload.get("currentStreakDays") or streak or 0)
        # 注意：不给 credit_snapshot —— 这是同任务的兜底路径，当天可能已经由活动
        # 领取记过账，快照的 MAX 语义会把已累计的额度顶回去。纯增量即可。
        return TaskResult(
            "success",
            f"签到成功 +{got} 积分（连续 {days} 天）" if got else f"签到成功（连续 {days} 天）",
            delta_credit=got,
            detail={"realm": realm, "reward_credits": got, "streak_days": days},
            cred_update=update,
        )

    # ------------------------------------------------------------ 实时余额
    async def fetch_credits(self, cred: dict) -> dict:
        """GET /api/v2/quota/usage → 实时可用余额。

        返回体是扁平结构（没有 code/data 信封）：
            userQuota{remaining, used, total} + addOnQuota{...}
        余额 = 两者 remaining 之和。顶层 expiresAt 的归属未证实（国内号常给
        「永不过期」占位值），不能当套餐到期展示；isQuotaExceeded=true 且两个
        quota 全 0 是退化体，照实返回 0，不臆造。
        """
        cred, refreshed = await self._ensure_token(cred)
        update = cred if refreshed else None

        status, text = await _api(cred, P_QUOTA)
        if status == 401 and cred.get("refreshToken") and not refreshed:
            cred, _ = await self._refresh(cred)
            update = cred
            status, text = await _api(cred, P_QUOTA)
        if status == 401:
            raise ProviderError("Qoder 令牌失效（401），请重新导出凭证", "need_login")
        if status >= 500:
            raise ProviderError(f"上游返回 {status}", "retryable")
        try:
            body = json.loads(text)
        except Exception:
            raise ProviderError("余额响应不是 JSON", "retryable")
        if not isinstance(body, dict):
            raise ProviderError("余额响应结构异常", "retryable")

        uq = body.get("userQuota") or {}
        aq = body.get("addOnQuota") or {}
        balance = int(_num(uq, "remaining") + _num(aq, "remaining"))
        used = int(_num(uq, "used") + _num(aq, "used"))
        total = int(_num(uq, "total") + _num(aq, "total"))
        return {
            "balance": balance,
            "used": used,
            "total": total,
            "realm": realm_of(cred),
            "cred_update": update,
        }

    # ------------------------------------------------------------ 令牌（保活）
    async def refresh_credentials(self, cred: dict) -> dict:
        """刷新 access token（保活）。公开入口，renew 巡检与手动续期共用。"""
        return await self._refresh(cred)

    async def _ensure_token(self, cred: dict) -> tuple[dict, bool]:
        token = cred.get("accessToken", "")
        exp = int(cred.get("expire_at") or 0) or jwt_exp(token)
        if token and exp and exp - time.time() > MARGIN_H * 3600:
            return cred, False
        if not cred.get("refreshToken"):
            if token and (not exp or exp > time.time()):
                return cred, False
            raise ProviderError("缺少 refreshToken 且 accessToken 已失效", "need_login")
        try:
            return await self._refresh(cred), True
        except ProviderError:
            if token and (not exp or exp > time.time()):
                return cred, False  # 刷新失败但旧令牌仍可用，继续本次
            raise

    async def _refresh(self, cred: dict) -> dict:
        rt = cred.get("refreshToken", "")
        if not rt:
            raise ProviderError("缺少 refreshToken", "need_login")
        status, text = await _api(
            cred,
            P_DEVICE_REFRESH,
            "POST",
            {"refresh_token": rt},
            headers=_headers({**cred, "accessToken": ""}),
        )
        if status != 200:
            raise ProviderError(f"令牌刷新失败 HTTP {status}", "need_login")
        try:
            body = json.loads(text)
        except Exception:
            raise ProviderError("刷新响应非 JSON", "need_login")
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        # 官方响应（实测 2026-10-31）：
        #   {"device_token":"dt-…","refresh_token":"drt-…","token_type":"Bearer",
        #    "expires_at":"2026-11-30T02:11:22Z","refresh_token_expires_at":"2027-…"}
        # 注意字段名是 device_token（不是 token），过期时间是 ISO 字符串（不是时间戳）。
        new_token = (
            data.get("device_token")
            or data.get("token")
            or data.get("accessToken")
            or data.get("access_token")
        )
        if not new_token:
            raise ProviderError("刷新响应缺少 token 字段", "need_login")
        new_rt = data.get("refresh_token") or data.get("refreshToken") or rt
        new_cred = dict(cred)
        new_cred["accessToken"] = new_token
        new_cred["refreshToken"] = new_rt
        new_cred["expire_at"] = _expiry_epoch(data.get("expires_at")) or jwt_exp(new_token)
        return new_cred

    # ---------------------------------------------- 设备码登录（手机号+验证码在官方页完成）
    async def start_login(self, realm: str) -> dict:
        """发起设备码登录，返回官方授权页链接。

        与官方 qodercli 同流程（PKCE + nonce）：面板生成 verifier/challenge 与
        machine_id/nonce，拼出 verificationUri；用户在**自己的浏览器**里打开该链接，
        用手机号 + 短信验证码登录（阿里云风控在官方页完成）并选择账号授权。
        面板随后轮询 deviceToken/poll 拿 dt- 令牌（约 30 天有效）。
        """
        import hashlib
        import secrets
        import uuid
        from urllib.parse import urlencode

        def b64url(data: bytes) -> str:
            return base64.urlsafe_b64encode(data).decode().rstrip("=")

        verifier = b64url(secrets.token_bytes(32))
        challenge = b64url(hashlib.sha256(verifier.encode()).digest())
        nonce = str(uuid.uuid4())
        params = urlencode({
            "challenge": challenge,
            "challenge_method": "S256",
            "machine_id": str(uuid.uuid4()),
            "nonce": nonce,
        })
        return {"url": f"{LOGIN_URL.get(realm, LOGIN_URL['cn'])}?{params}",
                "nonce": nonce, "verifier": verifier}

    async def poll_login(self, realm: str, nonce: str, verifier: str) -> dict:
        """轮询设备码登录结果：pending / ready（含 token 与账号资料）。"""
        from urllib.parse import urlencode

        probe_cred = {"realm": realm}
        url = P_DEVICE_POLL + "?" + urlencode({
            "nonce": nonce, "verifier": verifier, "challenge_method": "S256",
        })
        status, text = await _api(
            probe_cred, url,
            headers={"Accept": "application/json", "User-Agent": "Go-http-client/2.0"},
        )
        if status in (202, 404):
            return {"status": "pending"}
        if status != 200:
            raise ProviderError(f"登录轮询失败 HTTP {status}", "retryable")
        try:
            body = json.loads(text)
        except Exception:
            raise ProviderError("登录轮询返回非 JSON", "retryable")
        token = str(body.get("token") or "")
        if not token:
            raise ProviderError("轮询返回 200 但没有 token（上游结构可能变化）", "retryable")

        profile: dict = {}
        try:
            s2, t2 = await _api(
                probe_cred, P_USERINFO,
                headers={"Accept": "application/json", "User-Agent": "Go-http-client/2.0",
                         "Authorization": f"Bearer {token}"},
            )
            if s2 == 200:
                profile = json.loads(t2) or {}
        except Exception:  # noqa: BLE001  资料失败不阻断登录
            pass

        exp = 0
        raw_exp = body.get("expires_at")
        if isinstance(raw_exp, (int, float)) and raw_exp > 0:
            exp = int(raw_exp // 1000) if raw_exp > 10**12 else int(raw_exp)
        if not exp and body.get("expires_in"):
            exp = int(time.time() + float(body["expires_in"]))
        if not exp:
            exp = jwt_exp(token)
        return {
            "status": "ready",
            "uid": str(body.get("user_id") or jwt_sub(token) or ""),
            "nickname": str(profile.get("name") or profile.get("username") or ""),
            "email": str(profile.get("email") or ""),
            "accessToken": token,
            "refreshToken": str(body.get("refresh_token") or body.get("refreshToken") or ""),
            "expire_at": exp,
        }
