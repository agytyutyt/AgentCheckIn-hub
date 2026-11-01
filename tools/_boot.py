"""让 tools/ 下的脚本在任何 Python 下都能跑：缺依赖时自动切到项目虚拟环境。

为什么需要：面板依赖（httpx / fastapi …）只装在 `.venv` 里。用户经常直接敲
`python tools\\import_qoder.py --save`，用的是系统 Python → 报
「无法加载面板模块：No module named 'httpx'」，而真正的原因跟面板没关系，
只是解释器选错了。这个错误信息完全不指向解法，所以这里在导入面板模块之前
先自检：缺依赖就用 `.venv` 里的解释器**原地重跑自己**（os.execv），
用户不需要记住任何额外命令。

只在需要落库（`--save`）时才调用：单纯导出 JSON 不需要 httpx，不该为此换解释器。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_GUARD = "CH_TOOLS_REEXEC"  # 防止「切过去还缺依赖」时无限重跑


def venv_python() -> Path | None:
    """项目虚拟环境的解释器（Windows / POSIX 都认）。"""
    for rel in ("Scripts/python.exe", "bin/python3", "bin/python"):
        candidate = ROOT / ".venv" / rel
        if candidate.exists():
            return candidate
    return None


def _missing(modules: tuple[str, ...]) -> list[str]:
    out = []
    for name in modules:
        try:
            __import__(name)
        except Exception:  # noqa: BLE001
            out.append(name)
    return out


def ensure_deps(*modules: str) -> None:
    """当前解释器缺 `modules` 时，切到 .venv 重跑本脚本（只切一次）。

    正常情况（依赖都在）直接返回，不做任何事。
    """
    lack = _missing(modules)
    if not lack:
        return

    exe = venv_python()
    hint = (
        f"当前 Python（{sys.executable}）缺少依赖：{', '.join(lack)}。\n"
    )
    if exe is None:
        raise SystemExit(
            hint + "项目虚拟环境 .venv 不存在 —— 先双击一次 start.bat 建好环境，再重试。"
        )
    if os.environ.get(_GUARD) == "1":
        # 已经切过一次还缺：说明 .venv 本身没装全，别再无意义重试
        raise SystemExit(
            hint + f".venv\\ 里也没有这些模块 —— 执行一次：\n  \"{exe}\" -m pip install -r requirements.txt"
        )

    print(
        f"[提示] 当前解释器缺少 {', '.join(lack)}，自动切换到项目虚拟环境重试…",
        file=sys.stderr,
    )
    os.environ[_GUARD] = "1"
    script = Path(sys.argv[0])
    if not script.is_absolute():
        script = Path.cwd() / script
    # execv 原地替换进程：退出码、stdout/stderr 都与原调用一致
    os.execv(str(exe), [str(exe), str(script), *sys.argv[1:]])
