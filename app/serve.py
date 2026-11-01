"""服务启动入口。

注意：调度器在进程内运行，必须保持单 worker，否则会重复执行任务。
"""
from __future__ import annotations

import sys

import uvicorn

from .config import HOST, LOG_DIR, PORT


def _ensure_streams() -> None:
    """无控制台启动（pythonw / 计划任务 / nohup）时 stdout、stderr 是 None。

    此时 uvicorn 的日志句柄和未捕获异常的回溯都无处可写，服务看起来"起来了但什么都没记"。
    统一重定向到 data/logs/panel.log，至少事后能查到启动失败的原因。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stream = open(LOG_DIR / "panel.log", "a", encoding="utf-8", buffering=1)
    except OSError:
        return
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def main() -> None:
    _ensure_streams()
    uvicorn.run("app.main:app", host=HOST, port=PORT, workers=1, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
