#!/bin/bash
set -euo pipefail

# Папка, где лежит скрипт (и приложение)
APP_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
echo "Папка приложения: $APP_DIR"

# ---------- Общие константы ----------
SVC_NAME="pg_web"
UNIT_FILE="/etc/systemd/system/${SVC_NAME}.service"
VENV="$APP_DIR/.venv"
MODE="install"
CONFIRM=0

usage() {
    cat <<EOF
Использование: sudo $0 [опции]

  (без аргументов)     установка/обновление systemd-сервиса
  -remove, --remove    удалить то, что создавал скрипт:
                         * сервис $SVC_NAME (stop + disable + unit-файл)
                         * виртуальное окружение $VENV
                         * $APP_DIR/.env  (PGPASSWORD, AUTH_PASS, FLASK_SECRET_KEY!)
                       НЕ удаляет: исходники, бэкапы PostgreSQL,
                       пакет python3-venv, системных пользователей.
  -y, --yes            не спрашивать подтверждение (обязателен, когда stdin не терминал)
  -h, --help           эта справка
EOF
}

do_remove() {
    echo
    echo "=== Удаление web_pgrestore ==="
    echo "Будет удалено:"
    echo "  1) systemd-сервис '$SVC_NAME' — stop, disable, $UNIT_FILE"
    echo "  2) виртуальное окружение — $VENV"
    echo "  3) конфигурация с секретами — $APP_DIR/.env"
    echo "НЕ будет удалено:"
    echo "  - исходники в $APP_DIR"
    echo "  - бэкапы PostgreSQL (BACKUP_ROOT) и restore_audit.log"
    echo "  - пакет python3-venv и системные пользователи"
    echo

    if [ "$CONFIRM" -ne 1 ]; then
        if [ -t 0 ]; then
            A=""
            read -rp "Продолжить? [y/N]: " A || A=""
            case "$A" in
                y|Y|д|Д|yes|YES|да|Да) ;;
                *) echo "Отменено."; exit 0 ;;
            esac
        else
            echo "ОШИБКА: неинтерактивный запуск требует явного согласия:"
            echo "        sudo $0 -remove -y"
            exit 1
        fi
    fi

    # 1) сервис: остановить, отключить, удалить unit.
    #    systemctl может отсутствовать (контейнер/WSL) — без проверки
    #    `set -e` оборвёт скрипт на daemon-reload.
    HAVE_SYSTEMCTL=0
    command -v systemctl >/dev/null 2>&1 && HAVE_SYSTEMCTL=1
    UNIT_EXISTS=0
    [ -f "$UNIT_FILE" ] && UNIT_EXISTS=1
    if [ "$HAVE_SYSTEMCTL" -eq 1 ] && { [ "$UNIT_EXISTS" -eq 1 ] \
        || systemctl is-enabled --quiet "$SVC_NAME" 2>/dev/null \
        || systemctl is-active --quiet "$SVC_NAME" 2>/dev/null; }; then
        systemctl disable --now "$SVC_NAME" >/dev/null 2>&1 || true
        rm -f "$UNIT_FILE"
        systemctl daemon-reload || true
        systemctl reset-failed "$SVC_NAME" >/dev/null 2>&1 || true
        echo "Сервис '$SVC_NAME' остановлен, отключён, unit-файл удалён."
    elif [ "$HAVE_SYSTEMCTL" -eq 1 ]; then
        echo "Сервис '$SVC_NAME' не установлен — ок."
    else
        [ "$UNIT_EXISTS" -eq 1 ] && rm -f "$UNIT_FILE" \
            && echo "systemctl не найден — убран только $UNIT_FILE" \
            || echo "systemctl не найден, сервис не установлен — ок."
    fi

    # 2) виртуальное окружение
    if [ -d "$VENV" ]; then
        rm -rf "$VENV"
        echo "Удалено venv: $VENV"
    else
        echo "venv не найден — ок."
    fi

    # 3) .env с секретами
    if [ -f "$APP_DIR/.env" ]; then
        rm -f "$APP_DIR/.env"
        echo "Удалён $APP_DIR/.env (PGPASSWORD, AUTH_PASS, FLASK_SECRET_KEY)"
    else
        echo ".env не найден — ок."
    fi

    echo
    echo "Удаление завершено. Исходники и бэкапы сохранены."
    echo "Повторный запуск $0 -remove безопасен (всё уже удалено)."
}

# ---------- Разбор аргументов ----------
for arg in "$@"; do
    case "$arg" in
        -remove|--remove) MODE="remove" ;;
        -y|--yes) CONFIRM=1 ;;
        -h|--help) usage; exit 0 ;;
        *)
            echo "ОШИБКА: неизвестный аргумент: $arg"
            echo
            usage
            exit 1
            ;;
    esac
done

# Скрипт трогает /etc/systemd, chown и apt — нужен root.
# Проверка стоит ПОСЛЕ разбора аргументов, чтобы -h работал без root.
if [ "$(id -u)" -ne 0 ]; then
    echo "ОШИБКА: запусти от root: sudo $0"
    exit 1
fi

# Режим удаления обрабатывается ДО любых установочных шагов, иначе по дороге
# создались бы заново venv, .env и unit-файл.
if [ "$MODE" = "remove" ]; then
    do_remove
    exit 0
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

    V=$(get_env APP_HOST); V="${V:-127.0.0.1}"
    read -rp "Адрес для слушания (127.0.0.1 = только этот сервер, 0.0.0.0 = все интерфейсы) [$V]: " A || A=""
    set_env APP_HOST "${A:-$V}"

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
create_venv() { python3 -m venv "$VENV" >/dev/null 2>&1; }

if [ ! -x "$VENV/bin/python" ] || ! "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
    [ -d "$VENV" ] && { echo "Удаляю неполный/сломанный venv"; rm -rf "$VENV"; }
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

# Имя сервиса и путь к unit заданы вверху скрипта (общие для install/remove)

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
