#!/bin/bash
set -euo pipefail

# Папка, где лежит скрипт (и приложение)
APP_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
echo "Папка приложения: $APP_DIR"

# Скрипт трогает /etc/systemd и chown — нужен root
if [ "$(id -u)" -ne 0 ]; then
    echo "ОШИБКА: запусти от root: sudo $0"
    exit 1
fi

[ -f "$APP_DIR/app.py" ] || { echo "ОШИБКА: app.py нет в $APP_DIR"; exit 1; }

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
    if [ -f "$APP_DIR/.env.example" ]; then
        cp "$APP_DIR/.env.example" "$APP_DIR/.env"
        echo ".env создан из .env.example — проверь пути/доступы к PostgreSQL"
    else
        echo "ОШИБКА: нет ни .env, ни .env.example в $APP_DIR"
        exit 1
    fi
fi

# Генерируем секретный ключ, если он пустой или отсутствует
if ! grep -qE '^FLASK_SECRET_KEY=..+' "$APP_DIR/.env" 2>/dev/null; then
    KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
    if grep -q '^FLASK_SECRET_KEY=' "$APP_DIR/.env" 2>/dev/null; then
        sed -i "s|^FLASK_SECRET_KEY=.*|FLASK_SECRET_KEY=$KEY|" "$APP_DIR/.env"
    else
        echo "FLASK_SECRET_KEY=$KEY" >> "$APP_DIR/.env"
    fi
    echo "FLASK_SECRET_KEY сгенерирован в .env"
fi

# Виртуальное окружение + зависимости приложения
VENV="$APP_DIR/.venv"
if [ ! -x "$VENV/bin/python" ]; then
    if ! python3 -m venv "$VENV"; then
        echo "ОШИБКА: не удалось создать venv (на Debian/Ubuntu: apt install python3-venv)"
        exit 1
    fi
    echo "Создано venv: $VENV"
fi
"$VENV/bin/python" -m pip install --quiet -r "$APP_DIR/requirements.txt"
echo "Зависимости из requirements.txt установлены в venv"

# ⚠️ Безопасность: доступ к PostgreSQL
# Для работы без ввода пароля настройте один из вариантов:
#   1) Файл ~/.pgpass для пользователя $SVC_USER (рекомендуется):
#      echo "hostname:port:database:username:password" > ~svc/.pgpass && chmod 0600 ~svc/.pgpass
#   2) PGPASSWORD в $APP_DIR/.env (передаётся pg_restore через env).
# Без этого pg_restore запросит пароль интерактивно и restore завершится ошибкой.
# Подробности см. в README.md в разделе "Производство".

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
    echo "Сервис $SVC_NAME запущен от $SVC_USER ($VENV/bin/python)."
    echo "Проверка: curl -fsS http://127.0.0.1:5000/health"
else
    echo "ОШИБКА: сервис $SVC_NAME не поднялся. Последние строки лога:"
    journalctl -u "$SVC_NAME" -n 30 --no-pager || true
    exit 1
fi
