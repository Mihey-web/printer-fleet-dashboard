import os

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "10"))
# Bind address/port for the built-in web server. Behind the reverse proxy the
# default 127.0.0.1 is safest; set WEB_HOST=0.0.0.0 to expose it directly.
WEB_HOST = os.environ.get("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.environ.get("WEB_PORT", "8000"))

# Парк принтеров живёт в БД — таблица printers в printer_history.db,
# редактируется из админки (вкладка «Администрирование» → «Принтеры»).
# Списки PRINTERS/CREALITY_PRINTERS/KLIPPER_PRINTERS/MKS_PRINTERS удалены:
# они были разовым сидом миграции и больше не читаются.

# Telegram и прокси живут в БД — таблица settings в printer_history.db,
# редактируются из вкладки «Настройки» (секции Telegram и Прокси, только админ).
# Константы TELEGRAM_*/PROXY_* удалены: они были разовым сидом миграции
# и больше не читаются.

# --- Auth & Security ---
# No secret is stored in this file. The JWT signing key is resolved at startup by
# app/api/main.py in this order:
#   1. env FORGE_JWT_SECRET, if set;
#   2. a secret previously generated and persisted in users.db (app_secrets);
#   3. a fresh secrets.token_hex(32), generated and persisted on first boot.
# JWT_SECRET is left empty here and populated at runtime with the resolved value
# (JWTAuthMiddleware reads config.JWT_SECRET). Set FORGE_JWT_SECRET to pin/rotate
# it explicitly — changing it invalidates every issued token at once.
JWT_SECRET = os.environ.get("FORGE_JWT_SECRET", "")
JWT_ACCESS_EXPIRES = 15 * 60       # 15 minutes
JWT_REFRESH_EXPIRES = 7 * 24 * 3600 # 7 days

# Cookies are flagged Secure (browser sends them only over HTTPS). The dashboard
# is served behind nginx TLS, so this is correct. Set COOKIE_SECURE=0 only if you
# access the app directly over plain HTTP without the TLS proxy.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1") not in ("0", "false", "False", "")

# First-run admin bootstrap (see app/api/main.py). Username defaults to 'admin'.
# If FORGE_ADMIN_PASSWORD is unset, a random password is generated and logged once
# on first boot — no default password is baked into the code.
ADMIN_USERNAME = os.environ.get("FORGE_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("FORGE_ADMIN_PASSWORD", "")