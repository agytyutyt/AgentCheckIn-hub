"""Provider 统一契约：各平台只需实现 probe / run。"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

Status = Literal["success", "skipped", "pending", "retryable", "failed", "need_login"]


@dataclass
class TaskResult:
    status: Status
    message: str
    delta_credit: int = 0
    detail: dict = field(default_factory=dict)
    cred_update: dict | None = None
    # 服务端权威的「当天该任务累计积分」。给了就按天取最大值落库（不是累加），
    # 于是「今日已签到」这种没有新领取的轮次也能把当天真实到账的积分记下来，
    # 且同一天重复执行不会把 100 分记成 200。
    credit_snapshot: int | None = None
    # 纯空跑（已签 / 名额已用完 / 宠物还在路上）：只写一行 DEBUG 日志，
    # 日志页默认折叠，避免 30 分钟一次的轮询把真正的签到记录顶出视野。
    quiet: bool = False

    @property
    def ok(self) -> bool:
        return self.status in ("success", "skipped", "pending")

    @property
    def should_retry(self) -> bool:
        return self.status in ("retryable", "failed")


def decode_jwt(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part).decode("utf-8", "ignore"))
    except Exception:
        return {}


def jwt_exp(token: str) -> int:
    exp = decode_jwt(token).get("exp")
    try:
        return int(exp) if exp else 0
    except Exception:
        return 0


def jwt_sub(token: str) -> str:
    payload = decode_jwt(token)
    for k in ("sub", "uid", "user_id", "userId", "id"):
        if payload.get(k):
            return str(payload[k])
    return ""


def _client(proxy: str = "") -> httpx.AsyncClient:
    from .. import config

    # trust_env=False：忽略系统 HTTP_PROXY/HTTPS_PROXY，避免被本机代理劫持；
    # 需要代理时显式设置 CHECKIN_PROXY（或调用方传入 proxy 参数）。
    kwargs: dict[str, Any] = {"timeout": 20.0, "follow_redirects": True, "trust_env": False}
    target = proxy or config.HTTP_PROXY
    if target:
        try:
            return httpx.AsyncClient(proxy=target, **kwargs)
        except TypeError:  # httpx < 0.28
            return httpx.AsyncClient(proxies=target, **kwargs)
    return httpx.AsyncClient(**kwargs)


async def request(
    method: str,
    url: str,
    headers: dict[str, str],
    json_body: dict | None = None,
    proxy: str = "",
) -> tuple[int, str]:
    """统一出站请求；网络异常向上抛，由执行器判定为可重试。

    `proxy` 非空时强制走该出口（优先级高于 CHECKIN_PROXY），供分区专用出口
    （Qoder 国际版按出口 IP 识别地区/风控，机房直连可能拿不到活动视图）。
    """
    async with _client(proxy) as client:
        resp = await client.request(method, url, headers=headers, json=json_body)
        return resp.status_code, resp.text


class Provider:
    key: str = ""
    name: str = ""
    tasks: dict[str, str] = {}

    async def probe(self, cred: dict) -> dict:
        """校验凭证并返回账号信息：{uid, expire_at, ...}；失败抛 ProviderError。"""
        raise NotImplementedError

    async def run(self, cred: dict, task: str) -> TaskResult:
        raise NotImplementedError


class ProviderError(Exception):
    def __init__(self, message: str, status: Status = "failed", detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.detail = detail or {}
