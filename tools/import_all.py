"""启动前的账号检查：依次尝试导入 WorkBuddy / Qoder，输出友好的一行结论。

由 start.bat 调用，也可以单独运行：

    python tools/import_all.py           # 静默尝试，只打印摘要
    python tools/import_all.py --verbose # 打印完整过程

无论成功失败都以退出码 0 结束——面板该启动还是要启动，
账号问题留给用户在面板里处理。
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

LOG_DIR = Path(os.environ.get("CHECKIN_DATA_DIR", ROOT / "data")) / "logs"


def _run(mod_name: str, label: str) -> tuple[str, str]:
    """执行某个导入器，返回 (状态, 摘要)。状态：ok / skip。"""
    try:
        mod = __import__(mod_name)
    except Exception as exc:  # pragma: no cover
        return "skip", f"{label} 模块加载失败：{exc}"

    buf = io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = mod.main(["--save"])
    except SystemExit as exc:
        code = int(exc.code or 0)
    except Exception as exc:
        code = 1
        buf.write(f"\n{type(exc).__name__}: {exc}")

    output = buf.getvalue()
    ok_line = next((ln.strip() for ln in output.splitlines() if ln.strip().startswith("✅")), "")

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with (LOG_DIR / "import.log").open("a", encoding="utf-8") as fh:
            fh.write(f"\n===== {stamp} {label} (exit={code}) =====\n{output}\n")
    except Exception:
        pass

    if code == 0 and ok_line:
        return "ok", ok_line.lstrip("✅").strip()
    reason = "未检测到客户端或未登录"
    if "解密失败" in output:
        reason = "客户端凭证解密失败"
    elif "未找到 WorkBuddy 登录态" in output or "未找到 Qoder 客户端" in output:
        reason = "未检测到客户端"
    return "skip", f"{label} 跳过（{reason}）"


def main() -> int:
    verbose = "--verbose" in sys.argv
    results = [
        _run("import_workbuddy", "WorkBuddy"),
        _run("import_qoder", "Qoder"),
    ]

    ok = [r for r in results if r[0] == "ok"]
    for status, message in results:
        if status == "ok":
            print(f"  [OK] {message}")
        elif verbose:
            print(f"  [--] {message}")
        else:
            print(f"  [--] {message.split(' 跳过')[0]} 跳过")

    if not ok:
        print("  提示：未导入任何账号。可先在客户端登录后重跑，或在面板里手动添加。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
