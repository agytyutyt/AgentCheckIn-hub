"""全局配置：路径、时区、主密钥、管理员口令。"""
from __future__ import annotations

import os
import secrets
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("CHECKIN_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

def _load_dotenv() -> None:
    """极简 .env 读取，避免额外依赖；已存在的环境变量优先。"""
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


_load_dotenv()

DB_PATH = DATA_DIR / "checkin.db"
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

TZ_NAME = os.environ.get("TZ", "Asia/Shanghai")
try:
    TZ = ZoneInfo(TZ_NAME)
except Exception:  # pragma: no cover - 兜底
    TZ = ZoneInfo("Asia/Shanghai")
    TZ_NAME = "Asia/Shanghai"


def _load_master_key() -> str:
    """优先环境变量；否则落盘到 data/master.key 并复用。"""
    env_key = os.environ.get("MASTER_KEY", "").strip()
    if env_key:
        return env_key
    key_file = DATA_DIR / "master.key"
    if key_file.exists():
        return key_file.read_text(encoding="utf-8").strip()
    key = secrets.token_urlsafe(32)
    key_file.write_text(key, encoding="utf-8")
    try:
        key_file.chmod(0o600)
    except OSError:
        pass
    return key


MASTER_KEY = _load_master_key()

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
# 默认口令；公网部署前请在 .env 里改掉
DEFAULT_ADMIN_PASSWORD = "admin123"
_RAW_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()
_PLACEHOLDERS = {"", "please-change-me", "change-me", "changeme", "admin"}
ADMIN_PASSWORD_FROM_ENV = _RAW_PASSWORD not in _PLACEHOLDERS
ADMIN_PASSWORD = _RAW_PASSWORD if ADMIN_PASSWORD_FROM_ENV else DEFAULT_ADMIN_PASSWORD

# 出站代理，如 http://127.0.0.1:7890；为空则直连
HTTP_PROXY = os.environ.get("CHECKIN_PROXY", "").strip()

# Qoder 令牌提前刷新阈值（小时）。云端单点运行，不与客户端竞争
QODER_REFRESH_MARGIN_H = int(os.environ.get("QODER_REFRESH_MARGIN_H", "12"))

SESSION_COOKIE = "ch_session"
SESSION_TTL = 7 * 24 * 3600

APP_NAME = "CheckinHub"
APP_VERSION = "1.0.0"
