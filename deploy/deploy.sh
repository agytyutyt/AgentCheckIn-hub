#!/usr/bin/env bash
# 服务器端一键部署 / 更新。在项目根目录执行：
#   bash deploy/deploy.sh
# 幂等：重复执行 = 重新构建并滚动重启，数据不受影响。
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"

info() { printf '\033[36m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

# ---------- 0. 环境检查 ----------
DOCKER="docker"
if ! docker info >/dev/null 2>&1; then
    if command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
        DOCKER="sudo docker"
    else
        die "无法访问 Docker。请用 root 执行，或把当前用户加入 docker 组：sudo usermod -aG docker \$USER && newgrp docker"
    fi
fi
$DOCKER compose version >/dev/null 2>&1 || die "缺少 docker compose 插件（需要 Docker 20.10+ / Compose v2）"

# ---------- 1. 配置 ----------
NEW_PASSWORD=""
if [ ! -f .env ]; then
    info "[1/4] 生成 .env"
    cp .env.example .env
    NEW_PASSWORD="$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 16)"
    # 用 | 作分隔符，避免口令里的特殊字符破坏 sed
    sed -i "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=${NEW_PASSWORD}|" .env
else
    info "[1/4] 已有 .env，保持不变"
fi

ADMIN_PW="$(grep -E '^ADMIN_PASSWORD=' .env | head -1 | cut -d= -f2- || true)"
DOMAIN_VAL="$(grep -E '^DOMAIN=' .env | head -1 | cut -d= -f2- || true)"

if [ -f data/master.key ]; then
    info "      检测到已有 data/master.key，将沿用同一密钥解密历史凭证"
elif [ -n "${MASTER_KEY:-}" ]; then
    info "      使用环境变量 MASTER_KEY"
else
    info "      首次运行，将自动生成 data/master.key（请一并备份）"
fi

# ---------- 2. 构建 ----------
info "[2/4] 构建镜像（首次约 2-4 分钟）"
$DOCKER compose build

# ---------- 3. 启动 ----------
info "[3/4] 启动容器"
$DOCKER compose up -d --remove-orphans

# ---------- 4. 健康检查 ----------
info "[4/4] 等待服务就绪"
READY=0
for _ in $(seq 1 30); do
    if $DOCKER compose exec -T app python -c \
        "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=2).status==200 else 1)" \
        >/dev/null 2>&1; then
        READY=1
        break
    fi
    sleep 2
done

IP="$(curl -fsS --max-time 3 https://api.ipify.org 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')"
if [ -n "$DOMAIN_VAL" ]; then
    URL="https://${DOMAIN_VAL}"
elif [ -n "$IP" ]; then
    URL="http://${IP}"
else
    URL="http://<服务器IP>"
fi

echo
if [ "$READY" = "1" ]; then
    printf '\033[32m✅ 部署完成\033[0m\n'
else
    warn "⚠️  容器已启动但健康检查未通过，查看日志：$DOCKER compose logs -f app"
fi
echo "   访问地址：${URL}"
echo "   用户名  ：admin"
if [ -n "$NEW_PASSWORD" ]; then
    echo "   初始口令：${NEW_PASSWORD}   （已写入 .env，请登录后修改）"
else
    echo "   口令    ：见 .env 的 ADMIN_PASSWORD（为空则为默认 admin123）"
fi
echo
echo "   常用命令："
echo "     $DOCKER compose logs -f app       # 实时日志"
echo "     $DOCKER compose restart app       # 重启"
echo "     $DOCKER compose down              # 停止"
echo
warn "别忘了：腾讯云控制台安全组需放行 80/443（纯 HTTP 只放 80）"
