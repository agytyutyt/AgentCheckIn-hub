#!/bin/sh
# 以 root 启动 → 修正挂载目录属主 → 降权到 appuser 运行。
# 这样宿主机上 ./data 是 root 建的也不会写不进去。
# 只用基础镜像自带的 setpriv（util-linux），不依赖 apt 安装 gosu。
set -e

DATA_DIR="${CHECKIN_DATA_DIR:-/app/data}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    chown -R appuser:appuser "$DATA_DIR" 2>/dev/null || true

    if command -v setpriv >/dev/null 2>&1; then
        exec setpriv --reuid=10001 --regid=10001 --clear-groups "$@"
    fi
    if command -v gosu >/dev/null 2>&1; then
        exec gosu appuser "$@"
    fi
    echo "警告：未找到 setpriv/gosu，将以 root 运行" >&2
fi

exec "$@"
