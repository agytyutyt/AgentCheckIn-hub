"""通过 SSH 把部署包传到服务器并执行部署（一次性运维脚本）。

密码从环境变量 SSH_PASSWORD 读取，不落盘、不出现在命令行里。

    python deploy/remote_deploy.py check
    python deploy/remote_deploy.py upload
    python deploy/remote_deploy.py deploy
    python deploy/remote_deploy.py all
"""
from __future__ import annotations

import argparse
import os
import posixpath
import sys
import time

import paramiko

HOST = os.environ.get("SSH_HOST", "")
USER = os.environ.get("SSH_USER", "ubuntu")
PORT = int(os.environ.get("SSH_PORT", "22"))
PASSWORD = os.environ.get("SSH_PASSWORD", "")
REMOTE_DIR = os.environ.get("REMOTE_DIR", "")
ARCHIVE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "deploy", "checkin-hub.tar.gz")


def connect() -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        HOST,
        port=PORT,
        username=USER,
        password=PASSWORD,
        timeout=20,
        banner_timeout=30,
        auth_timeout=30,
        look_for_keys=False,
        allow_agent=False,
    )
    return client


def run(client, cmd: str, timeout: int = 120, sudo: bool = False) -> tuple[int, str]:
    if sudo:
        cmd = f"sudo -S -p '' bash -lc {quote(cmd)}"
        stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout, get_pty=False)
        stdin.write(PASSWORD + "\n")
        stdin.flush()
    else:
        stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout, get_pty=False)

    chunks = []
    for line in iter(stdout.readline, ""):
        chunks.append(line)
        sys.stdout.write(line)
        sys.stdout.flush()
    err = stderr.read().decode("utf-8", "ignore")
    if err.strip():
        sys.stdout.write(err)
    code = stdout.channel.recv_exit_status()
    return code, "".join(chunks)


def quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


def remote_home(client) -> str:
    _, out = run(client, "echo $HOME")
    return out.strip().splitlines()[-1].strip()


def step_check(client) -> int:
    print("== 环境检查 ==")
    code, _ = run(
        client,
        "whoami; uname -a | cut -c1-80; "
        "docker --version 2>&1; docker compose version 2>&1 | head -1; "
        "(docker info >/dev/null 2>&1 && echo DOCKER_OK) || echo DOCKER_NEED_SUDO; "
        "(sudo -n true 2>/dev/null && echo SUDO_NOPASS) || echo SUDO_NEEDS_PASS",
        timeout=60,
    )
    return code


def step_upload(client) -> str:
    home = remote_home(client)
    target = REMOTE_DIR or posixpath.join(home, "checkin-hub")
    print(f"== 上传到 {target} ==")
    size = os.path.getsize(ARCHIVE)
    print(f"本地包大小: {size/1024/1024:.2f} MB")

    sftp = client.open_sftp()
    remote_tmp = "/tmp/checkin-hub.tar.gz"
    t0 = time.time()
    sftp.put(ARCHIVE, remote_tmp, callback=lambda done, total: None)
    print(f"上传完成，用时 {time.time()-t0:.1f}s")
    sftp.close()

    cmd = (
        f"mkdir -p {target} && tar -xzf {remote_tmp} -C {target} && "
        f"echo '--- 解压后 ---' && ls {target} && "
        f"du -sh {target}"
    )
    code, _ = run(client, cmd, timeout=180)
    if code != 0:
        raise SystemExit("解压失败")
    return target


def step_deploy(client, target: str) -> int:
    print("== 执行 deploy.sh（首次构建约 2-4 分钟）==")
    code, _ = run(client, f"cd {target} && bash deploy/deploy.sh", timeout=900)
    return code


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["check", "upload", "deploy", "all"])
    ap.add_argument("--dir", default="", help="远端安装目录")
    args = ap.parse_args()

    global REMOTE_DIR
    if args.dir:
        REMOTE_DIR = args.dir

    if not HOST or not PASSWORD:
        print("需要环境变量 SSH_HOST / SSH_PASSWORD", file=sys.stderr)
        return 2
    if not os.path.exists(ARCHIVE):
        print(f"找不到部署包：{ARCHIVE}，请先运行 package.bat", file=sys.stderr)
        return 2

    client = connect()
    try:
        if args.action == "check":
            return step_check(client)
        if args.action == "upload":
            step_upload(client)
            return 0
        target = step_upload(client)
        if args.action == "all":
            return step_deploy(client, target)
        if args.action == "deploy":
            return step_deploy(client, target)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
