"""Qoder 活动平台的「桌面端同款」请求身份 —— 国际版领取活动的必需前置。

结论（2026-10-30 实测，口径 = NextAgentX/qoder-trae-workbuddy-switch 的
`campaign.rs::desktop_headers`，与官方客户端 createAuthorizedHeaders 对齐）：

  国际版活动列表**只**对下面这套完整头下发「每天领 100 Credits」：
    · User-Agent: Qoder、Accept: application/json, text/plain, */*
    · cosy-clienttype: 10、cosy-version: 桌面端版本
    · X-Request-ID / X-Machine-ID / X-Session-ID —— **按 uid MD5 稳定派生**
      （同一账号恒定同一台「虚拟设备」，不同账号互相隔离，多号不会被关联）
    · Origin / Referer —— 该区域官网
    · cosy-machineid —— 同样按 uid 派生（不是 auth.machine-id！）
    · cosy-machinetoken / machinetype / machinecode —— **官方原生风控身份**，
      由 `runtime-info` 生成；缺这三头时活动列表**静默少掉**每日领取条目
    · cosy-machineos: x86_64_win32、cosy-machinehostname: DESKTOP-QODER（固定形）

  身份是机器级的：`runtime-info prod --account-stdin`
  （win/mac 走 stdin 喂 `{"account":"<uid>"} `；linux 只给 prod），
  输出首行 JSON {machineToken, machineCode, machineType}；缓存 30 分钟。

来源优先级：本机客户端 resources/umid（Windows 先查注册表，再 .qoder-versions
新版本目录）→ qodercli 解压组件 → npm 拉官方 `@qoder-ai/qodercli`（sha512 校验）
抽 Linux ELF —— 云服务器面板的路径。组件不可用时回退「派生分支」：照发
X-*/cosy 基础头、不冒充机器身份（活动列表可能不可见，如实少活动）。
"""
from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import io
import json
import os
import platform
import re
import socket
import subprocess
import tarfile
import time
from pathlib import Path

from . import config
from .providers.base import request

WEBSITE = {"cn": "https://qoder.cn", "intl": "https://qoder.com"}
MACHINE_OS = "x86_64_win32"
MACHINE_HOSTNAME = os.environ.get("QODER_MACHINE_HOSTNAME", "DESKTOP-QODER")
DESKTOP_VERSION = os.environ.get("QODER_DESKTOP_VERSION", "0.4.3")
TTL = 30 * 60          # 身份是机器级且由本机种子决定，30 分钟内恒同一份
NEG_TTL = 5 * 60       # 失败缓存短一些（用户可能刚装上客户端/组件）
RUN_TIMEOUT = 25

PKG = "@qoder-ai/qodercli"
REGISTRIES = ["https://registry.npmmirror.com", "https://registry.npmjs.org"]
AUTH_REGISTRY = "https://registry.npmjs.org"
BUNDLE_ENTRY = "package/bundle/qodercli.js"
ELF_MACHINE = {"x64": 62, "arm64": 183}   # 键为 Node 风格；_arch() 归一化

# (realm, uid) -> (取到时刻, 身份dict | None)；None 表示「刚刚确认不可用」
_cache: dict[tuple[str, str], tuple[float, dict | None]] = {}
_lock = asyncio.Lock()


def derive_id(uid: str, salt: str) -> str:
    """`MD5("{salt}:{uid}")` —— 与官方客户端派生逐字节对齐（buddy-switch 钉死）。

    同一 (uid, salt) 恒定；不同 salt 互不相关（机器码/会话码不互相暴露关系）。
    """
    return hashlib.md5(f"{salt}:{uid}".encode("utf-8")).hexdigest()


def _exhausted(realm: str, uid: str) -> bool:
    hit = _cache.get((realm, uid))
    if not hit:
        return False
    ttl = TTL if hit[1] else NEG_TTL
    return (time.time() - hit[0] < ttl) and hit[1] is None


def _fresh_identity(realm: str, uid: str) -> dict | None:
    hit = _cache.get((realm, uid))
    if hit and hit[1] and time.time() - hit[0] < TTL:
        return hit[1]
    return None


# ---------------------------------------------------------------- 本机客户端定位
def _exe_name() -> str:
    return "runtime-info.exe" if platform.system() == "Windows" else "runtime-info"


def _registry_dirs() -> list[str]:
    """Windows 注册表 Uninstall 里的 Qoder 安装目录（自定义路径时唯一线索）。"""
    if platform.system() != "Windows":
        return []
    import winreg

    out: list[str] = []
    for hive, sub in (
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ):
        try:
            key = winreg.OpenKey(hive, sub)
        except OSError:
            continue
        try:
            i = 0
            while True:
                try:
                    item = winreg.OpenKey(key, winreg.EnumKey(key, i))
                except OSError:
                    break
                i += 1
                with item:
                    try:
                        name, _ = winreg.QueryValueEx(item, "DisplayName")
                    except OSError:
                        continue
                    if not str(name).startswith("Qoder") or "IDE" in str(name):
                        continue
                    for field in ("UninstallString", "DisplayIcon", "InstallLocation"):
                        try:
                            val, _ = winreg.QueryValueEx(item, field)
                        except OSError:
                            continue
                        val = str(val or "").strip()
                        if not val:
                            continue
                        m = re.match(r'^\s*"([^"]+)"', val) or re.match(
                            r"^\s*([^,]+?\.exe)", val, re.I
                        )
                        d = (
                            str(Path(m.group(1)).parent)
                            if m
                            else (val.rstrip("\\/") if field == "InstallLocation" else "")
                        )
                        if d and d not in out:
                            out.append(d)
        finally:
            winreg.CloseKey(key)
    return out


def _client_dirs(realm: str) -> list[str]:
    """该区域客户端可能的安装目录。"""
    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    progfiles = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    names = ["Qoder"] if realm == "intl" else ["Qoder CN", "Qoder"]
    dirs: list[str] = []
    for name in names:
        dirs += [
            os.path.join(local, "Programs", name),
            os.path.join(progfiles, name),
            os.path.join(home, name),
        ]
    dirs += _registry_dirs()
    if platform.system() == "Darwin":
        dirs += [os.path.join(home, "Library", "Application Support", f"{n}.app") for n in names]
    else:
        dirs += [os.path.join("/opt", n) for n in names]
    out, seen = [], set()
    for d in dirs:
        k = d.lower().rstrip("\\/")
        if k not in seen and os.path.isdir(d):
            seen.add(k)
            out.append(d)
    return out


def _resource_dirs(realm: str) -> list[str]:
    """客户端 resources 目录级候选：.qoder-versions/新版本 在前，顶层兜底。"""
    out: list[str] = []
    for d in _client_dirs(realm):
        vd = os.path.join(d, ".qoder-versions")
        versions = []
        if os.path.isdir(vd):
            try:
                versions = sorted(
                    (
                        p
                        for p in os.listdir(vd)
                        if os.path.isdir(os.path.join(vd, p))
                        and p.replace(".", "").isdigit()
                    ),
                    key=lambda p: [int(x) for x in p.split(".") if x.isdigit()],
                    reverse=True,
                )
            except OSError:
                versions = []
        out += [os.path.join(vd, p, "resources") for p in versions]
        out.append(os.path.join(d, "resources"))
    return out


def _client_exe(realm: str) -> str | None:
    for r in _resource_dirs(realm):
        cand = os.path.join(r, "umid", _exe_name())
        if os.path.isfile(cand):
            return cand
    return None


def _cli_exe(realm: str):
    """官方 qodercli 解压出来的组件（客户端装过 CLI 后会有）。"""
    homes = [".qoder-cn", ".qoder"] if realm == "cn" else [".qoder", ".qoder-cn"]
    for h in homes:
        bin_dir = Path.home() / h / ".bin"
        if not bin_dir.is_dir():
            continue
        exe = _exe_name()
        for d in sorted(bin_dir.iterdir(), reverse=True):
            if d.name.startswith("umid-") and (d / exe).is_file():
                return str(d / exe)
    return None


def _hostname() -> str:
    return (socket.gethostname() or "").strip()[:96]


# ---------------------------------------------------------------- npm 提取（Linux 云面板路径）
def _arch() -> str:
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return "x64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    return m


def _extract_elf(bundle_text: str, machine: int) -> bytes:
    """在 qodercli bundle 里找内嵌 ELF（base64 字面量），按 e_machine 挑架构。"""
    for m in re.finditer(rb'"(f0VMRgIB[A-Za-z0-9+/=]+)"', bundle_text.encode("utf-8", "ignore")):
        raw = base64.b64decode(m.group(1))
        if len(raw) >= 20 and int.from_bytes(raw[18:20], "little") == machine:
            return raw
    return b""


def _check_integrity(data: bytes, integrity: str) -> bool:
    m = re.match(r"^sha512-(.+)$", str(integrity or "").split()[0])
    return bool(m) and hashlib.sha512(data).digest() == base64.b64decode(m.group(1))


async def _fetch_meta(registry: str) -> dict:
    status, text = await request(
        "GET", f"{registry}/{PKG.replace('/', '%2f')}/latest", {"Accept": "application/json"}
    )
    if status != 200:
        raise RuntimeError(f"{registry} HTTP {status}")
    return json.loads(text)


async def _fetch_bytes(url: str) -> bytes:
    from .providers.base import _client

    async with _client() as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            raise RuntimeError(f"{url} HTTP {resp.status_code}")
        return resp.content


_dir = config.DATA_DIR / "qoder-umid"


async def _install_cli_component(arch_key: str) -> str:
    """从 npm 拉官方 qodercli，抽出本机架构的 runtime-info 落盘并试运行。"""
    machine = ELF_MACHINE.get(arch_key)
    if not machine or platform.system() != "Linux":
        raise RuntimeError(f"npm 提取仅支持 Linux x64/arm64（当前 {platform.system()}/{_arch()}）")
    # 权威 integrity 以 npmjs 为准；取不到时退回镜像自身 integrity。
    authority = None
    try:
        _auth = await _fetch_meta(AUTH_REGISTRY)
        _auth_int = (_auth.get("dist") or {}).get("integrity")
        if _auth_int and _auth.get("version"):
            authority = {"integrity": _auth_int, "version": _auth["version"]}
    except Exception:  # noqa: BLE001
        authority = None
    errors = []
    for registry in REGISTRIES:
        try:
            meta = await _fetch_meta(registry)
            dist = meta.get("dist") or {}
            tarball, integrity = dist.get("tarball"), dist.get("integrity")
            if not tarball or not integrity:
                raise RuntimeError("包元数据缺少 tarball / integrity")
            if authority and registry != AUTH_REGISTRY and meta.get("version") != authority.get("version"):
                raise RuntimeError(
                    f"镜像版本 {meta.get('version')} 与 npmjs {authority.get('version')} 不一致"
                )
            expected = (
                authority["integrity"]
                if authority and authority.get("version") == meta.get("version")
                else integrity
            )
            tgz = await _fetch_bytes(tarball)
            if not _check_integrity(tgz, expected):
                raise RuntimeError("下载内容与 npm integrity 不一致")
            with tarfile.open(fileobj=io.BytesIO(gzip.decompress(tgz))) as tf:
                member = tf.extractfile(BUNDLE_ENTRY)
                if member is None:
                    raise RuntimeError(f"包里没有 {BUNDLE_ENTRY}")
                bundle = member.read().decode("utf-8", "ignore")
            elf = _extract_elf(bundle, machine)
            if not elf:
                raise RuntimeError(f"qodercli {meta.get('version')} 里没有本机架构的 UMID 程序")
            _dir.mkdir(parents=True, exist_ok=True, mode=0o755)
            bin_path = _dir / "runtime-info"
            bin_path.write_bytes(elf)
            os.chmod(bin_path, 0o755)
            _run(bin_path, "probe")   # 试运行验证
            manifest = {
                "version": meta.get("version"),
                "arch": _arch(),
                "sha256": hashlib.sha256(elf).hexdigest(),
                "installed_at": int(time.time()),
            }
            (_dir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            os.chmod(_dir / "manifest.json", 0o644)
            return str(bin_path)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{registry}：{exc}")
    raise RuntimeError("设备身份组件安装失败 —— " + "；".join(errors))


def _run(exe: str, uid: str) -> dict:
    """`runtime-info prod [--account-stdin]`，首行 JSON 取三个身份字段。"""
    args = [str(exe), "prod"]
    stdin = None
    if platform.system() in ("Windows", "Darwin"):
        args.append("--account-stdin")
        stdin = json.dumps({"account": uid}) + " "   # 官方形态：尾随空格（组件按分隔符读）
    proc = subprocess.run(
        args, input=stdin, capture_output=True, text=True, timeout=RUN_TIMEOUT,
        cwd=str(os.path.dirname(str(exe))),
    )
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except Exception:
            continue
        out = {k: str(data.get(k) or "").strip() for k in ("machineToken", "machineCode", "machineType")}
        if all(out.values()):
            # vmInfo 是官方风控的「是否虚拟机」自报（qoder2api-hub 的 _diag_campaign.py
            # 明确用它解释「为什么没有活动」）。只做诊断展示，不冒充头部。
            vm = data.get("vmInfo") or {}
            if isinstance(vm, dict) and vm:
                out["vm"] = {
                    "isVm": bool(vm.get("isVm")),
                    "brand": str(vm.get("brand") or "")[:32],
                }
            return out
        raise RuntimeError("runtime-info 输出缺少 machineToken/Code/Type")
    raise RuntimeError(f"runtime-info 无产出（exit {proc.returncode}）{(proc.stderr or '')[:120]}")


def _pick_runner(realm: str) -> str | None:
    exe = _client_exe(realm)
    if exe:
        return exe
    exe = _cli_exe(realm)
    if exe:
        return exe
    if platform.system() == "Linux":
        bundled = _dir / "runtime-info"
        if bundled.is_file():
            return str(bundled)
    return None


# ---------------------------------------------------------------- 固定身份（设备定向的关键）
def pinned_path(realm: str, uid: str) -> Path:
    """固定身份文件路径：data/qoder-umid/identity-<realm>-<uid>.json"""
    return _dir / f"identity-{realm}-{uid}.json"


def _pinned_identity(realm: str, uid: str) -> dict | None:
    """读固定身份（用户从自己电脑导出的真实客户端身份）。

    为什么需要：国际版每日活动是**设备定向**的 —— 平台只把活动发给账号真正登录的
    那台机器（客户端身份）。云端生成的组件身份拿不到活动（实测：云端 1 条、无每日；
    用 PC 身份同一条请求 2 条且 CLAIMABLE，并能从云端领取成功）。
    ⇒ 把 PC 的身份固定到面板里，云端就能替这台机器签到。
    """
    f = pinned_path(realm, uid)
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    out = {k: str(data.get(k) or "").strip() for k in ("machineToken", "machineCode", "machineType")}
    return out if all(out.values()) else None


def save_pinned(realm: str, uid: str, identity: dict) -> Path:
    """写入固定身份（导出工具与 /api/qauth/identity 用）。"""
    keys = ("machineToken", "machineCode", "machineType")
    out = {k: str(identity.get(k) or "").strip() for k in keys}
    if not all(out.values()):
        raise ValueError("身份缺少 machineToken/machineCode/machineType")
    _dir.mkdir(parents=True, exist_ok=True, mode=0o755)
    f = pinned_path(realm, uid)
    f.write_text(
        json.dumps({**out, "saved_at": int(time.time()), "realm": realm, "uid": uid},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    os.chmod(f, 0o644)
    _cache.pop((realm, uid), None)      # 立刻生效
    return f


def reset(realm: str, uid: str) -> None:
    """清掉该账号的身份缓存（自愈用：下次 prepare 会重新取）。"""
    _cache.pop((realm, uid), None)


def source_of(realm: str, uid: str) -> str:
    """当前生效的身份来源：pinned（用户导出的真实身份）/ component（本机组件）/ none。"""
    hit = _cache.get((realm, uid))
    if hit and hit[1] and time.time() - hit[0] < TTL:
        return "pinned" if hit[1].get("_source") == "pinned" else "component"
    return "none"


async def _component_identity(realm: str, uid: str, *, force: bool = False) -> dict | None:
    """跑官方组件取身份；组件损坏时自动重装一次（自愈）。"""
    from . import db

    runner = _pick_runner(realm)
    if runner is None:
        try:
            runner = await _install_cli_component(_arch())
        except Exception as exc:  # noqa: BLE001
            db.log(0, f"Qoder 风控身份组件不可用：{exc}", "DEBUG")
            return None
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, lambda: _run(runner, uid))
    except Exception as exc:  # noqa: BLE001
        db.log(0, f"Qoder 风控身份生成失败（{runner}）：{exc}", "DEBUG")
        # 自愈：内置组件跑不动（被替换/截断/架构不符）时删掉重装一次再试
        if os.path.dirname(str(runner)) == str(_dir):
            try:
                for name in ("runtime-info", "manifest.json"):
                    p = _dir / name
                    if p.exists():
                        p.unlink()
                runner = await _install_cli_component(_arch())
                return await loop.run_in_executor(None, lambda: _run(runner, uid))
            except Exception as exc2:  # noqa: BLE001
                db.log(0, f"Qoder 身份组件重装后仍失败：{exc2}", "WARNING")
        return None


async def prepare(realm: str, uid: str, *, prefer: str = "", force: bool = False) -> dict | None:
    """取该账号所在机器的身份（带缓存：成功 30 分钟 / 失败 5 分钟）。

    来源优先级：**固定身份**（用户从自己电脑导出的真实客户端身份）→ 本机组件。
    `prefer="component"` 可反转顺序（身份自愈用：固定身份拿不到活动时改试云端组件）。
    `force=True` 忽略缓存重新取。
    失败返回 None：调用方照常发请求（走派生分支，不冒充机器身份），绝不判任务失败。
    """
    from . import db

    if not force:
        hit = _cache.get((realm, uid))
        if hit:
            ttl = TTL if hit[1] else NEG_TTL
            if time.time() - hit[0] < ttl:
                return hit[1]
    async with _lock:
        order = ["component", "pinned"] if prefer == "component" else ["pinned", "component"]
        for src in order:
            if src == "pinned":
                ident = _pinned_identity(realm, uid)
                if ident:
                    ident["_source"] = "pinned"
                    _cache[(realm, uid)] = (time.time(), ident)
                    return ident
            else:
                ident = await _component_identity(realm, uid, force=force)
                if ident:
                    ident["_source"] = "component"
                    _cache[(realm, uid)] = (time.time(), ident)
                    vm = ident.get("vm") or {}
                    if vm.get("isVm"):
                        # 官方规则（qoder2api-hub 的 _diag_campaign.py 明写）：虚拟化环境
                        # 计入风控画像，设备定向活动可能不下发。如实记录便于诊断。
                        db.log(
                            0,
                            "Qoder 设备身份来自虚拟机（isVm=true，平台 "
                            f"{vm.get('brand') or '未知'}）：设备定向活动可能不下发",
                            "DEBUG",
                        )
                    return ident
        _cache[(realm, uid)] = (time.time(), None)
        return None


async def alternate(realm: str, uid: str) -> tuple[str, dict] | None:
    """取「另一个来源」的身份（身份自愈用）。

    当前用固定身份 → 改跑云端组件；当前用组件（或还没有身份）→ 改读固定身份。
    两个来源都拿不到就返回 None（调用方保持原结论）。
    """
    current = source_of(realm, uid)
    if current == "pinned":
        ident = await _component_identity(realm, uid, force=True)
        if ident:
            ident["_source"] = "component"
            return "component", ident
        return None
    pinned = _pinned_identity(realm, uid)
    if pinned:
        pinned["_source"] = "pinned"
        return "pinned", pinned
    ident = await _component_identity(realm, uid, force=True)
    if ident:
        ident["_source"] = "component"
        return "component", ident
    return None


def headers_for(realm: str, uid: str, ident: dict | None) -> dict:
    """按给定身份组装「客户端同款」头（不含 Authorization）。"""
    headers = {
        "Accept": "application/json",
        "User-Agent": "Qoder",
        "Cosy-ClientType": "10",
        "Cosy-Version": DESKTOP_VERSION,
    }
    if ident and ident.get("machineToken"):
        headers.update({
            "Cosy-MachineOS": MACHINE_OS,
            "Cosy-MachineHostname": MACHINE_HOSTNAME,
            "Cosy-MachineId": derive_id(uid, "machine"),
            "Cosy-MachineToken": ident["machineToken"],
            "Cosy-MachineCode": ident["machineCode"],
            "Cosy-MachineType": ident["machineType"],
        })
    return headers


def peek(realm: str, uid: str) -> dict:
    """组装「客户端同款」请求头（不含 Authorization —— 令牌轮换时不能被缓存顶住）。

    集合严格按客户端日志（2026-10-30 `[Campaign] 活动状态请求发出`）：
      Accept / Cosy-ClientType / Cosy-Version / Cosy-MachineOS / MachineHostname /
      MachineId / MachineToken / MachineCode / MachineType / User-Agent: Qoder
    —— 客户端**不发** X-Request-ID / X-Machine-ID / X-Session-ID / Origin / Referer，
    多发反而与官方形态不一致。真正解锁每日活动的是 URL 上的 forceRefresh=true
    （见 providers/qoder.py 的 P_CAMPAIGNS_QUERY）。

    身份缓存命中时带全套 cosy-machine*；否则只给基础头（不冒充机器身份）。
    """
    return headers_for(realm, uid, _fresh_identity(realm, uid))


def host_name() -> str:
    return _hostname()
