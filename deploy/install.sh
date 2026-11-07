#!/usr/bin/env bash
#
# Установка бота расписания ВГПУ на Ubuntu 24.04 как службы systemd.
#
# Скрипт можно запускать повторно: он обновит код, зависимости и unit-файл,
# но не тронет уже созданный /etc/vspu-schedule-bot/env (кроме строк, переданных
# через --token и --admin) и базу пользователей.
#
# Запуск из каталога с исходниками, от root:
#   sudo bash deploy/install.sh                  # токен спросит интерактивно
#   sudo bash deploy/install.sh --token XXX      # или передать сразу
#   sudo bash deploy/install.sh --admin 12345    # назначить админов (ID через запятую)
#
set -euo pipefail

APP_NAME="vspu-schedule-bot"
APP_USER="vspubot"
APP_DIR="/opt/${APP_NAME}"
DATA_DIR="/var/lib/${APP_NAME}"
ENV_DIR="/etc/${APP_NAME}"
ENV_FILE="${ENV_DIR}/env"
UNIT_FILE="/etc/systemd/system/${APP_NAME}.service"

TOKEN=""
ADMINS=""
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<USAGE
Использование: sudo bash deploy/install.sh [опции]

  --token <токен>   токен бота от @BotFather (иначе возьмёт из .env рядом
                    с исходниками или спросит интерактивно)
  --admin <id,id>   Telegram ID админов для /stats и /broadcast
                    (свой ID: напишите боту /stats и посмотрите лог)
  -h, --help        эта справка
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --token) TOKEN="${2:-}"; shift 2 ;;
        --admin) ADMINS="${2:-}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "Неизвестный аргумент: $1" ;;
    esac
done

# --------------------------------------------------------------- проверки

[[ $EUID -eq 0 ]] || die "Запускайте через sudo"
[[ -z "$ADMINS" || "$ADMINS" =~ ^[0-9]+(,[0-9]+)*$ ]] || die "--admin: ожидаются числовые ID через запятую, например 12345,67890"
[[ -f "${SRC_DIR}/requirements.txt" && -f "${SRC_DIR}/main.py" ]] \
    || die "Не вижу main.py и requirements.txt — запускайте скрипт из каталога проекта"

if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    [[ "${VERSION_ID:-}" == "24.04" ]] || warn "Скрипт писался под Ubuntu 24.04, у вас ${PRETTY_NAME:-неизвестно}"
fi

# ------------------------------------------------------------- пакеты

log "Ставлю системные пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv rsync curl ca-certificates >/dev/null

# ------------------------------------------------------------- пользователь и каталоги

if ! id -u "$APP_USER" >/dev/null 2>&1; then
    log "Создаю системного пользователя ${APP_USER}"
    useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi

install -d -m 0755 -o root -g root "$APP_DIR"
install -d -m 0750 -o "$APP_USER" -g "$APP_USER" "$DATA_DIR"
install -d -m 0750 -o root -g "$APP_USER" "$ENV_DIR"

# ------------------------------------------------------------- код

log "Копирую код в ${APP_DIR}"
# .env и .env.example на сервер не нужны: настройки приходят из ${ENV_FILE}.
rsync -a --delete \
    --exclude '.git/' \
    --exclude '.venv/' \
    --exclude '__pycache__/' \
    --exclude '.pytest_cache/' \
    --exclude 'data/' \
    --exclude '.env' \
    --exclude '.env.example' \
    "${SRC_DIR}/" "${APP_DIR}/"

log "Собираю виртуальное окружение"
if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then
    python3 -m venv "${APP_DIR}/.venv"
fi
"${APP_DIR}/.venv/bin/pip" install --quiet --upgrade pip
"${APP_DIR}/.venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"
chown -R root:root "$APP_DIR"

# ------------------------------------------------------------- токен и настройки

# Меняет одну строку KEY=... в файле настроек (или дописывает её), остальное не трогает.
set_env() {
    local key="$1" value="$2"
    if grep -q "^${key}=" "$ENV_FILE"; then
        sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
    else
        printf '%s=%s
' "$key" "$value" >> "$ENV_FILE"
    fi
}

if [[ -z "$TOKEN" && ! -f "$ENV_FILE" && -f "${SRC_DIR}/.env" ]]; then
    TOKEN="$(grep -E '^BOT_TOKEN=' "${SRC_DIR}/.env" | head -1 | cut -d= -f2- | tr -d '\r"'"'" || true)"
    if [[ -n "$TOKEN" ]]; then
        log "Беру токен из ${SRC_DIR}/.env"
    fi
fi

# Спрашиваем, только если есть терминал (при запуске из cron/CI его нет).
if [[ -z "$TOKEN" && ! -f "$ENV_FILE" ]] && (exec </dev/tty) 2>/dev/null; then
    # Ввод не отображается на экране и не попадает в историю команд.
    read -rsp "Токен бота от @BotFather (Enter — заполнить позже): " TOKEN </dev/tty || TOKEN=""
    echo
fi

if [[ -n "$TOKEN" ]]; then
    [[ "$TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]] || warn "Токен не похож на токен Telegram (ожидается 123456789:AA...)"

    # Заодно проверяем, что сервер вообще достаёт до api.telegram.org.
    if ME="$(curl -fsS --max-time 15 "https://api.telegram.org/bot${TOKEN}/getMe" 2>/dev/null)"; then
        BOT_NAME="$(printf '%s' "$ME" | grep -o '"username":"[^"]*"' | cut -d'"' -f4)"
        log "Токен рабочий: @${BOT_NAME}"
    else
        warn "Не удалось проверить токен через api.telegram.org: токен неверный или Telegram недоступен с сервера"
    fi

    if [[ -f "$ENV_FILE" ]]; then
        log "Обновляю токен в ${ENV_FILE}"
        set_env BOT_TOKEN "$TOKEN"
    else
        log "Записываю ${ENV_FILE}"
        cat > "$ENV_FILE" <<ENVEOF
BOT_TOKEN=${TOKEN}
DB_PATH=${DATA_DIR}/bot.db
ENVEOF
    fi
elif [[ ! -f "$ENV_FILE" ]]; then
    log "Создаю ${ENV_FILE} с заглушкой"
    cat > "$ENV_FILE" <<ENVEOF
# Впишите токен и перезапустите: systemctl restart ${APP_NAME}
BOT_TOKEN=CHANGEME
DB_PATH=${DATA_DIR}/bot.db
ENVEOF
else
    log "${ENV_FILE} уже есть — оставляю как есть"
fi

if [[ -n "$ADMINS" ]]; then
    log "Админы: ${ADMINS}"
    set_env ADMIN_IDS "$ADMINS"
elif ! grep -q '^ADMIN_IDS=' "$ENV_FILE"; then
    warn "Админы не заданы — /stats и /broadcast недоступны. Добавить: --admin <ваш Telegram ID>"
fi
chown root:"$APP_USER" "$ENV_FILE"
chmod 0640 "$ENV_FILE"

# ------------------------------------------------------------- systemd

log "Пишу ${UNIT_FILE}"
cat > "$UNIT_FILE" <<UNITEOF
[Unit]
Description=Telegram-бот расписания ВГПУ
Wants=network-online.target
After=network-online.target
# Если бот падает сразу после старта (например, неверный токен) — не долбить бесконечно.
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
Environment=PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ExecStart=${APP_DIR}/.venv/bin/python main.py
Restart=always
RestartSec=5

# Песочница: писать можно только в каталог с базой.
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
ReadWritePaths=${DATA_DIR}

[Install]
WantedBy=multi-user.target
UNITEOF

systemctl daemon-reload
systemctl enable "$APP_NAME" >/dev/null 2>&1

if grep -q '^BOT_TOKEN=CHANGEME' "$ENV_FILE"; then
    warn "Токен не задан. Впишите его в ${ENV_FILE} и выполните: systemctl restart ${APP_NAME}"
    exit 0
fi

log "Запускаю службу"
systemctl reset-failed "$APP_NAME" 2>/dev/null || true
systemctl restart "$APP_NAME"
sleep 5

if systemctl is-active --quiet "$APP_NAME"; then
    log "Готово, бот работает"
else
    warn "Служба не запустилась. Последние строки лога:"
fi
journalctl -u "$APP_NAME" -n 15 --no-pager || true

cat <<DONE

Полезные команды:
  systemctl status ${APP_NAME}        — состояние
  journalctl -u ${APP_NAME} -f        — лог в реальном времени
  systemctl restart ${APP_NAME}       — перезапуск (например, после правки ${ENV_FILE})
  sudo bash deploy/install.sh         — обновить бота из новой версии исходников
DONE
