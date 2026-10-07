#!/bin/bash
set -e

# Папка, где лежит скрипт (и приложение)
APP_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
echo "Папка приложения: $APP_DIR"

[ -f "$APP_DIR/app.py" ] || { echo "Пиздец: app.py нет в $APP_DIR"; exit 1; }

# Проверка runtime-зависимостей (должны быть в PATH)
for dep in python3 pg_restore gzip; do
    command -v "$dep" >/dev/null 2>&1 || { echo "ОШИБКА: требуемая утилита '$dep' не найдена в PATH"; exit 1; }
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

# Назначаем владельца папки и всех файлов этому пользователю
echo "Настраиваем права..."
chown -R "$SVC_USER":"$SVC_GROUP" "$APP_DIR"

# Генерируем секретный ключ, если нет
if ! grep -q "FLASK_SECRET_KEY" "$APP_DIR/.env" 2>/dev/null; then
    KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
    echo "FLASK_SECRET_KEY=$KEY" >> "$APP_DIR/.env"
    echo "FLASK_SECRET_KEY добавлен в .env"
fi

# ⚠️ Безопасность: доступ к PostgreSQL
# Для работы без ввода пароля настройте один из вариантов:
#   1) Файл ~/.pgpass для пользователя $SVC_USER (рекомендуется):
#      echo "hostname:port:database:username:password" > ~/.pgpass && chmod 0600 ~/.pgpass
#   2) Переменная окружения PGPASSWORD (передаётся pg_restore через env).
# Без этого pg_restore запросит пароль интерактивно и restore завершится ошибкой.
# Подробности см. в README.md в разделе "Производство".

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
ExecStart=$(command -v python3) $APP_DIR/app.py
Restart=always
RestartSec=5
PrivateTmp=true
NoNewPrivileges=true

# Для полной изоляции (опционально)
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$APP_DIR /tmp
NoNewPrivileges=true
PrivateDevices=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SVC_NAME"

echo "Сервис $SVC_NAME запущен от $SVC_USER."