#!/bin/bash
set -euo pipefail

# Папка, где лежит скрипт (и приложение)
APP_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
echo "Папка приложения: $APP_DIR"

# Скрипт трогает /etc/systemd, chown и apt — нужен root
if [ "$(id -u)" -ne 0 ]; then
    echo "ОШИБКА: запусти от root: sudo $0"
    exit 1
fi

[ -f "$APP_DIR/app.py" ] || { echo "ОШИБКА: app.py нет в $APP_DIR"; exit 1; }
[ -f "$APP_DIR/requirements.txt" ] || { echo "ОШИБКА: requirements.txt нет в $APP_DIR"; exit 1; }

# Проверка runtime-зависимостей (должны быть в PATH)
for dep in python3 pg_restore gzip; do
    command -v "$dep" >/dev/null 2>&1 || { echo "ОШИБКА: требуемая утилия '$dep' не найдена в PATH"; exit 1; }
done

# Выбираем служебного пользователя
if id www-data &>/dev/null; then
    SVC_USER="www-data"
    SVC_GROUP="www-data"
    echo "Используем системного пользователя: $SVC_USER"
else
    SVC_USER="pgweb"
    SVC_GROUP="pgweb"
    echo "www-data не найден, создаём пользователя: $SVC_USER"
    if ! id "$SVC_USER" &>/dev/null; then
        useradd --system --no-create-home --shell /usr/sbin/nologin "$SVC_USER"
    fi
fi

# .env: берём .env.example как основу, если файла нет
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    echo ".env создан из .env.example"
fi

# Безопасная запись значения в .env (KEY=VALUE), через python —
# пароли могут содержать |, &, / и прочие символы
set_env() {
    python3 - "$APP_DIR/.env" "$1" "$2" <<'PYEOF'
import sys
path, key, val = sys.argv[1], sys.argv[2], sys.argv[3]
lines = []
try:
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
except FileNotFoundError:
    pass
new = f"{key}={val}"
done = False
for i, ln in enumerate(lines):
    if ln.split("=", 1)[0].strip() == key:
        lines[i] = new
        done = True
        break
if not done:
    lines.append(new)
with open(path, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines) + "\n")
PYEOF
}

get_env() {
    python3 - "$APP_DIR/.env" "$1" <<'PYEOF'
import sys
path, key = sys.argv[1], sys.argv[2]
try:
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if ln.startswith(key + "="):
                print(ln.split("=", 1)[1])
                break
except FileNotFoundError:
    pass
PYEOF
}

# ---------- Интерактивные вопросы (пропускаются при неинтерактивном запуске) ----------
if [ -t 0 ]; then
    echo
    echo "=== Настройка web_pgrestore (Enter = значение по умолчанию) ==="

    V=$(get_env APP_PORT); V="${V:-5000}"
    read -rp "Порт веб-панели [$V]: " A || A=""
    set_env APP_PORT "${A:-$V}"

    V=$(get_env PGHOST); V="${V:-/var/run/postgresql}"
    read -rp "Хост PostgreSQL (сокет или TCP) [$V]: " A || A=""
    set_env PGHOST "${A:-$V}"

    V=$(get_env PGUSER); V="${V:-postgres}"
    read -rp "Пользователь БД [$V]: " A || A=""
    set_env PGUSER "${A:-$V}"

    V=$(get_env PGPASSWORD)
    read -rsp "Пароль БД (пусто = peer/pgpass)${V:+ [задан]}: " A || A=""
    echo
    [ -n "${A:-}" ] && set_env PGPASSWORD "$A"

    V=$(get_env BACKUP_ROOT); V="${V:-/mnt/backups/dump}"
    read -rp "Папка с бэкапами BACKUP_ROOT [$V]: " A || A=""
    set_env BACKUP_ROOT "${A:-$V}"

    V=$(get_env AUTH_USER)
    read -rp "Логин для входа в веб-панель (пусто = без auth): " A || A=""
    set_env AUTH_USER "${A:-$V}"

    if [ -n "$(get_env AUTH_USER)" ]; then
        read -rsp "Пароль для входа в веб-панель: " A || A=""
        echo
        set_env AUTH_PASS "${A:-$(get_env AUTH_PASS)}"
        if [ -z "$(get_env AUTH_PASS)" ]; then
            echo "ОШИБКА: логин задан, а пароль пустой"
            exit 1
        fi
    else
        echo "ВНИМАНИЕ: auth выключена — панель будет доступна всем, кто достучится до APP_HOST."
    fi
    echo
fi

# Генерируем секретный ключ, если он пустой или отсутствует
if ! grep -qE '^FLASK_SECRET_KEY=..+' "$APP_DIR/.env" 2>/dev/null; then
    KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
    if grep -q '^FLASK_SECRET_KEY=' "$APP_DIR/.env" 2>/dev/null; then
        set_env FLASK_SECRET_KEY "$KEY"
    else
        echo "FLASK_SECRET_KEY=$KEY" >> "$APP_DIR/.env"
    fi
    echo "FLASK_SECRET_KEY сгенерирован в .env"
fi

# ---------- Виртуальное окружение + зависимости ----------
VENV="$APP_DIR/.venv"
create_venv() { python3 -m venv "$VENV" >/dev/null 2>&1; }

if [ ! -x "$VENV/bin/python" ]; then
    if ! create_venv; then
        # На Debian/Ubuntu нет python3-venv (ensurepip) — ставим сами
        if command -v apt-get >/dev/null 2>&1; then
            echo "python3-venv не найден, ставим через apt-get..."
            apt-get update || true
            DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3-venv
            rm -rf "$VENV"
            if ! create_venv; then
                echo "ОШИБКА: venv всё равно не создался. Установи вручную: apt install python3-venv python3.10-venv"
                exit 1
            fi
        else
            echo "ОШИБКА: не удалось создать venv (нет apt-get). Установи python3-venv вручную."
            exit 1
        fi
    fi
    echo "Создано venv: $VENV"
fi
"$VENV/bin/python" -m pip install --quiet -r "$APP_DIR/requirements.txt"
echo "Зависимости из requirements.txt установлены в venv"

# ⚠️ Доступ к PostgreSQL: задай пароль выше или настрой ~/.pgpass для $SVC_USER.
# Подробности — README.md, раздел "Производство".

# Назначаем владельца папки и всех файлов этому пользователю
echo "Настраиваем права..."
chown -R "$SVC_USER":"$SVC_GROUP" "$APP_DIR"

# Имя сервиса
SVC_NAME="pg_web"
UNIT_FILE="/etc/systemd/system/${SVC_NAME}.service"

# Пишем unit
cat > "$UNIT_FILE" <<EOF
[Unit]
Description=PostgreSQL Backup Restore Web App
After=network.target postgresql.service
Wants=postgresql.service

[Service]
User=$SVC_USER
Group=$SVC_GROUP
WorkingDirectory=$APP_DIR
EnvironmentFile=-$APP_DIR/.env
ExecStart=$VENV/bin/python $APP_DIR/app.py
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateDevices=true
# ProtectSystem=full: /usr, /boot, /etc доступны только на чтение,
# /var (сокет PostgreSQL) и /tmp — записываемы (PGHOST=/var/run/postgresql).
ProtectSystem=full

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SVC_NAME"

# Проверяем, что сервис реально поднялся (а не ушёл в crash-loop)
sleep 1
if systemctl is-active --quiet "$SVC_NAME"; then
    APP_HOST=$(get_env APP_HOST); APP_HOST="${APP_HOST:-127.0.0.1}"
    APP_PORT=$(get_env APP_PORT); APP_PORT="${APP_PORT:-5000}"
    echo
    echo "Сервис $SVC_NAME запущен от $SVC_USER."
    echo "Открой: http://$APP_HOST:$APP_PORT  (health: /health)"
    echo "Лог: journalctl -u $SVC_NAME -f"
else
    echo "ОШИБКА: сервис $SVC_NAME не поднялся. Последние строки лога:"
    journalctl -u "$SVC_NAME" -n 30 --no-pager || true
    exit 1
fi
