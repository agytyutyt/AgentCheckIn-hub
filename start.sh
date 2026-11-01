#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "[1/2] 创建虚拟环境..."
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi

[ -f .env ] || { cp .env.example .env; echo "已生成 .env，请修改 ADMIN_PASSWORD"; }

set -a; . ./.env; set +a
echo "[2/2] 启动 CheckinHub： http://${HOST:-127.0.0.1}:${PORT:-8000}"
exec .venv/bin/python -m app.serve
