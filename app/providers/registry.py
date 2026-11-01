"""平台注册表：新增平台只需在此登记。"""
from __future__ import annotations

from .base import Provider, ProviderError, TaskResult
from .qoder import QoderProvider
from .workbuddy import WorkBuddyProvider

_instances = [WorkBuddyProvider(), QoderProvider()]
PROVIDERS: dict[str, Provider] = {p.key: p for p in _instances}


def get(platform: str) -> Provider:
    provider = PROVIDERS.get(platform)
    if not provider:
        raise ProviderError(f"未支持的平台：{platform}")
    return provider


def platforms() -> list[dict]:
    return [
        {
            "key": p.key,
            "name": p.name,
            "tasks": p.tasks,
        }
        for p in PROVIDERS.values()
    ]
