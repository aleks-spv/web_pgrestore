# web_pgrestore

UI для безопасного восстановления PostgreSQL из бэкапов `pg_dump`/`pg_restore`
(сценарий использования — 1C: воссоздание БД из дампа).

Веб-панель поверх `pg_restore`: выбираете базу → выбираете бэкап → запускаете
восстановление. Пайплайн (terminate connections → drop → create → restore)
находится в фоне, а результат стримится в браузер через SSE.

**Запуск:** `python3 app.py` → http://127.0.0.1:5000

---

## Содержание

1. [Возможности](#возможности)
2. [Быстрый старт](#быстрый-старт)
3. [Структура проекта](#структура-проекта)
4. [Интерфейс](#интерфейс)
5. [API](#api)
6. [Restore pipeline](#restore-pipeline)
7. [Безопасность](#безопасность)
8. [Конфигурация](#конфигурация)
9. [Деплой в продакшен](#деплой-в-продакшен)
10. [Тесты](#тесты)
11. [Troubleshooting](#troubleshooting)
12. [Разработка](#разработка)

---

## Возможности

- **Фоновое восстановление** — HTTP-запрос возвращается сразу, `pg_restore`
  крутится в отдельном job'е; повиснуть над запросом невозможно.
- **Live-лог** — каждый шаг восстановления выводится в браузер по SSE
  (`/restore/progress/<job_id>`).
- **Безопасность** — auth (опционально), CSRF на всех POST,
  path containment (дампы хранятся строго под `BACKUP_ROOT`),
  rate-limit на логин, `ProxyFix` только за доверенным nginx.
- **Настройки из UI** — страница `/settings` редактирует allowlisted
  параметры `.env` без доступа к консоли: хост, порт, пути, учётные данные,
  лимиты.
- **Audit** — `restore_audit.log` фиксирует каждый restore (basename путей,
  owner, количество завершённых соединений, результат).

---

## Быстрый старт

```bash
git clone <repo> && cd web_pgrestore
python3 -m venv .venv
. .venv/bin/activate   # или .venv\Scripts\activate на Windows
pip install -r requirements.txt
cp .env.example .env
python3 app.py
```

Откройте http://127.0.0.1:5000

`pg_restore` / `gzip` должны быть в `PATH`; если нет — укажите
`PGPRO_BIN_DIR` в `.env`, например:

```ini
PGPRO_BIN_DIR=/usr/lib/postgresql/16/bin
```

Минимальный `.env` для первого запуска:

```ini
BACKUP_ROOT=/mnt/backups/dump      # где лежат дампы
PGHOST=/var/run/postgresql        # или TCP-хост
PGUSER=postgres
# PGPASSWORD=...                  # если нужен пароль
AUTH_USER=admin                    # включит авторизацию
AUTH_PASS=<любой_пароль>
FLASK_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
```

Структура бэкапов, которую ожидает приложение:

```
$BACKUP_ROOT/
  mydb/
    dump1.backup
    dump2.backup.gz
  otherdb/
    x.backup
```

---

## Структура проекта

```
app.py                      # точка входа (create_app + app.run)
web_pgrestore/
  __init__.py               # create_app(), регистрация blueprint'ов
  config.py                 # Config + update_env_file (.env)
  db.py                     # psycopg2 helpers (owner, drop, create, terminate)
  restore.py                # path validation + pg_restore (+ gzip)
  jobs.py                   # фоновые restore-job'и + логи для SSE
  security.py               # auth, CSRF, rate-limit, ProxyFix, open-mode guard
  routes/
    auth.py                 # /login /logout
    pages.py                # / /settings
    api.py                  # /api/databases /api/backups
    restore_routes.py       # /restore + SSE progress + jobs status
    health.py               # /health
templates/
  index.html                # SPA: restore + live SSE-лог
  login.html
  settings.html             # редактирование .env из UI
tests/
  test_security.py          # path/auth/csrf/jobs/settings unit-тесты
  test_smoke.py             # smoke: routes, health, settings save
requirements.txt
.env.example
```

---

## Интерфейс

### Главная (`/`)

1. **Панель конфигурации** — справочно: хост, `BACKUP_ROOT`, параллельность
   (значения берутся из `.env`).
2. **Выбор БД** — GET `/api/databases`, возвращает не-шаблонные базы.
3. **Выбор бэкапа** — GET `/api/backups?db=X`, возвращает **только basename**
   (`dump1.backup`, `dump2.backup.gz`), полные пути на сервере фронтенду
   не отдаются.
4. **Параметры restore** — владелец (auto / `DEFAULT_OWNER`),
   «Имя восстановленной БД», «Источник бэкапа» (если восстанавливаете
   в другую базу), подтверждение.
5. **Кнопка «Восстановить»** → `POST /restore` → **202**
   `{job_id, progress_url}`.
6. **Блок «Журнал восстановления»** — EventSource на `progress_url`,
   строки лога дописываются live; по завершении — статус ✅/❌ и итоговый JSON
   (exit code, owner).

### Авторизация

- `AUTH_USER`/`AUTH_PASS` заданы → все страницы и API требуют сессию.
- API без сессии → **401 JSON** (фронтенд сам редиректит на `/login`).
- HTML-страницы без сессии → **302** на `/login`.
- Rate-limit на логин: **10 POST** за 15 минут — загрузка/обновление страницы не считается.
- Время жизни сессии: 3600 с; `SameSite=Lax`.

### Настройки (`/settings`)

Форма редактирует allowlisted ключи и перезаписывает их в `.env`
(комментарии и структура файла сохраняются).

Секреты (`PGPASSWORD`, `AUTH_PASS`, `FLASK_SECRET_KEY`):

| Действие | Результат |
|---|---|
| Галка «не менять» + пусто | значение в `.env` не трогается |
| Введено новое значение | перезаписывает ключ |
| `__CLEAR__` | очищает ключ (пустое значение) |

Часть параметров применяется **только после перезапуска** приложения
(`APP_HOST`, `APP_PORT`, `TRUST_PROXY`, `SESSION_COOKIE_SECURE`) —
форма предупреждает об этом.

---

## API

| Маршрут | Метод | Описание |
|---|---|---|
| `/health` | GET | `{"status":"ok"}` |
| `/login` | GET/POST | Авторизация (CSRF, rate-limit 10 POST/15min) |
| `/logout` | GET | Очистка сессии |
| `/favicon.ico` | GET | SVG-заглушка (статика отключена: `static_folder=None`) |
| `/` | GET | UI: выбор БД → бэкап → restore |
| `/settings` | GET/POST | Просмотр/сохранение allowlisted ключей в `.env` |
| `/api/databases` | GET | Список не-шаблонных БД |
| `/api/backups?db=X` | GET | Basenames `*.backup*` под `BACKUP_ROOT/X` |
| `/api/audit-log` | GET | Журнал restore-операций из `restore_audit.log` |
| `/api/pgagent-log?job=…&status=…&limit=…` | GET | Журнал заданий postgresql (pgAgent): задача и шаг одной колонкой, начало, результат, длительность, вывод шага (до 20 000 символов); limit по умолчанию 10; фильтр — отбор по колонке «Задача и шаг» (select из уникальных пар); длительность из `jslduration` (фолбэк `jslend − jslstart`); ходит только в базу `postgres` |
| `/restore` | POST | Запуск restore job → **202** `{job_id, progress_url}` |
| `/restore/progress/<id>` | GET | **SSE**: `event: log`, `event: done` |
| `/restore/jobs/<id>` | GET | JSON-статус job'а (без stdout/stderr) |

### Примеры

```bash
# health
curl -s http://127.0.0.1:5000/health

# логин (получаем сессию + CSRF-токен)
curl -s -c /tmp/cj -b /tmp/cj \
  -d '_csrf=TOKEN' -d 'username=admin' -d 'password=...' \
  http://127.0.0.1:5000/login

# список БД и бэкапов
curl -s -b /tmp/cj http://127.0.0.1:5000/api/databases
curl -s -b /tmp/cj 'http://127.0.0.1:5000/api/backups?db=mydb'

# запуск restore (все POST требуют CSRF)
curl -s -b /tmp/cj -H 'Content-Type: application/json' \
  -H 'X-CSRF-Token: TOKEN' \
  -d '{"dbname":"mydb","backup":"dump1.backup","source_db":"mydb","_csrf":"TOKEN"}' \
  http://127.0.0.1:5000/restore

# live-лог (SSE)
curl -sN -b /tmp/cj http://127.0.0.1:5000/restore/progress/<job_id>

# статус job'а
curl -s -b /tmp/cj http://127.0.0.1:5000/restore/jobs/<job_id>
```

### Коды ответов

- `400` — невалидный JSON, bad dbname, backup вне `BACKUP_ROOT`, нет CSRF;
- `401` — нет сессии (API);
- `409` — restore для этой БД уже идёт (per-DB lock);
- `429` — rate-limit; на `/login` браузер получает **HTML** с формой и текстом
  «Слишком много попыток», JSON-клиенты — JSON;
- `500` — сбой terminate/drop/create/restore (в ответе `step` + `error`).

---

## Restore pipeline

Выполняется **в фоновом job'e** (`web_pgrestore/jobs.py`), а не в HTTP-потоке.

```
POST /restore
    │
    ├─ валидация: CSRF, dbname, backup под BACKUP_ROOT/<source>
    ├─ глобальный слот MAX_CONCURRENT_RESTORES (по умолчанию 2)
    └─ per-DB lock (одну БД нельзя восстанавливать параллельно)
          │
          ├─ 1. Owner: pg_database.datdba → иначе DEFAULT_OWNER
          ├─ 2. pg_terminate_backend (все соединения с целевой БД)
          ├─ 3. DROP DATABASE IF EXISTS
          ├─ 4. CREATE DATABASE ... OWNER ...
          └─ 5. pg_restore --no-owner --no-password
                 (.gz → gzip -dc | pg_restore)
```

Каждый шаг пишет строку в job-лог → SSE → браузер.

**Важно:** после `DROP` база уже недоступна. Если `CREATE` или `pg_restore`
упал — в ответе будет `database_state` («may be missing» /
«created but empty/incomplete»). Восстановление из бэкапа — единственный
путь вернуть данные.

`SUBPROCESS_TIMEOUT` (по умолчанию **300 с**) убивает зависший `pg_restore`.
Для больших дампов 1C поднимите его в UI/`.env` (например `14400`).

---

## Безопасность

| Мера | Как реализовано |
|---|---|
| Auth | `AUTH_USER`/`AUTH_PASS`; сравнение пароля через `hmac.compare_digest` |
| Fail-closed bind | auth-off + не-loopback `APP_HOST` → **старт падает**, нет `ALLOW_UNAUTHENTICATED=1` |
| CSRF | все POST: `_csrf` / `X-CSRF-Token`; токен хранится в session |
| API без сессии | **401 JSON**, а не HTML-redirect |
| Path traversal | `realpath` + containment через `os.sep` (ловит `dump_evil`) |
| SQL injection | `psycopg2.sql.Identifier` + параметры; `shell=False` |
| Rate-limit | in-memory per-IP; на `/login` считаются только POST; `RATE_LIMIT_ENABLED=0` только для тестов |
| ProxyFix | **только** при `TRUST_PROXY=1` (иначе spoofed XFF обходит лимиты) |
| Session | `SameSite=Lax`, lifetime 1 ч; `SESSION_COOKIE_SECURE` за HTTPS |
| Audit | `restore_audit.log`, в логах **только basename** путей |
| Конфиденциальные данные | фронтенду не передаются `PGPASSWORD`, `AUTH_PASS`, `FLASK_SECRET_KEY`, абсолютные пути к `.env` |

Конфиг nginx для прода:

```nginx
location / {
    proxy_pass http://127.0.0.1:5000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

и в `.env`: `TRUST_PROXY=1`, `SESSION_COOKIE_SECURE=1`, `AUTH_USER`/`AUTH_PASS` заданы.

---

## Конфигурация

Все ключи задаются через `.env` (см. `.env.example`) или на `/settings`.

| Ключ | Default | Назначение |
|---|---|---|
| `PGHOST` | `/tmp` | unix-socket или TCP-хост PostgreSQL |
| `PGPORT` | `5432` | порт |
| `PGUSER` | `postgres` | пользователь |
| `PGPASSWORD` | пусто | пароль (libpq / `get_conn`) |
| `PGPRO_BIN_DIR` | пусто | каталог с `pg_restore`/`gzip` |
| `BACKUP_ROOT` | `/mnt/backups/dump` | корень бэкапов |
| `DEFAULT_OWNER` | `usr1cv8` | owner, если нет в `pg_database` |
| `AUTH_USER` / `AUTH_PASS` | пусто | авторизация; пустые = off (только loopback) |
| `FLASK_SECRET_KEY` | random | сессии; **задайте явно** в проде |
| `APP_HOST` | `127.0.0.1` | bind |
| `APP_PORT` | `5000` | порт |
| `TRUST_PROXY` | `0` | `1` — доверять `X-Forwarded-*` |
| `ALLOW_UNAUTHENTICATED` | `0` | `1` — auth-off на не-loopback хосте |
| `SESSION_COOKIE_SECURE` | `0` | `1` за HTTPS |
| `SUBPROCESS_TIMEOUT` | `300` | таймаут `pg_restore`, сек (`0` = ∞) |
| `PGPRO_PARALLEL` | `0` | `pg_restore --parallel` |
| `MAX_CONCURRENT_RESTORES` | `2` | глобальный лимит параллельных job'ов |
| `RATE_LIMIT_ENABLED` | `1` | `0` — только для тестов |

---

## Деплой в продакшен

### gunicorn (рекомендуется)

```bash
gunicorn -w 4 -b 127.0.0.1:5000 --timeout 300 'web_pgrestore:create_app()'
```

- `create_app()` читает `.env` **при старте** каждого воркера.
- Rate-limit **in-memory per-process** — при `N` воркерах суммарный лимит
  ≈ `N × max_calls`. Для общего лимита используйте nginx `limit_req`
  или вынесенный store.
- Job'ы хранятся in-memory — лог job'а теряется при рестарте воркера.

### systemd

`install_service.sh` генерирует unit (User/Group, WorkingDirectory,
`EnvironmentFile` на `.env`, `ExecStart` из `.venv`, `Restart=always`,
`NoNewPrivileges`, `PrivateDevices`, `ProtectSystem=full`).

```bash
sudo ./install_service.sh           # установка / обновление
sudo ./install_service.sh -remove   # удалить то, что создавал скрипт
sudo ./install_service.sh -h        # справка
```

#### Удаление (`-remove`)

| Удаляет | Не трогает |
|---|---|
| сервис `pg_web`: `disable --now`, unit-файл `/etc/systemd/system/pg_web.service`, `daemon-reload`, `reset-failed` | исходники в `APP_DIR` |
| виртуальное окружение `$APP_DIR/.venv` | бэкапы PostgreSQL (`BACKUP_ROOT`) и `restore_audit.log` |
| `$APP_DIR/.env` — в нём `PGPASSWORD`, `AUTH_PASS`, `FLASK_SECRET_KEY` | пакет `python3-venv` (apt), системные пользователи (`www-data`/`pgweb`), права `chown` |

- Ключ разбирается **до** установочных шагов, поэтому по дороге не создаются
  заново venv, `.env` и unit-файл.
- Запуск идемпотентен: повторный `-remove` сообщает, что уже удалено, и выходит с 0.
- При интерактивном запуске скрипт спрашивает `Продолжить? [y/N]`; когда stdin
  не терминал, требуется явный ключ `-y`:
  `sudo ./install_service.sh -remove -y`.
- После удаления повторная установка создаст `.env` из `.env.example` и
  сгенерирует **новый** `FLASK_SECRET_KEY` — все прежние сессии станут
  недействительными.

После смены `.env` через UI параметры `APP_*`, `TRUST_PROXY` применяются
только после `systemctl restart`.

### Чек-лист перед эксплуатацией

- [ ] `AUTH_USER`/`AUTH_PASS` заданы **или** bind только `127.0.0.1`
- [ ] `FLASK_SECRET_KEY` зафиксирован (не random при каждом рестарте)
- [ ] `TRUST_PROXY=1` только за доверенным nginx
- [ ] `SESSION_COOKIE_SECURE=1` при HTTPS
- [ ] `SUBPROCESS_TIMEOUT` под размер ваших дампов
- [ ] `BACKUP_ROOT` доступен приложению (read)
- [ ] Пользователь БД (`PGUSER`) может terminate/drop/create

---

## Тесты

```bash
python3 -m unittest tests.test_security tests.test_smoke -v
```

**32 теста**, PostgreSQL не нужен:

| Модуль | Что проверяет |
|---|---|
| `test_security` | path containment (dumpX/escape/symlink), open-mode guard, 401 API, CSRF, settings `.env` upsert, secret-keep, job pipeline при missing backup |
| `test_smoke` | регистрация всех роутов, `/health`, index без утечки `PGUSER`, settings GET/POST, обновление runtime `Config`, CSRF на `/restore` |

Ожидаемый вывод: `Ran 32 tests ... OK`.

---

## Troubleshooting

| Симптом | Что смотреть |
|---|---|
| «Restore failed to start» | `.env` не читается / нет прав на каталог |
| 401 на API | не залогинены или сессия истекла (1 ч) |
| 400 CSRF | запрос без `_csrf`/`X-CSRF-Token` |
| 409 restore already running | та же БД уже восстанавливается |
| «Файл бэкапа не принадлежит источнику» | backup вне `BACKUP_ROOT/<source_db>/` |
| `pg_restore: command not found` | `PGPRO_BIN_DIR` или PATH |
| auth-off старт падает | задайте auth **или** `ALLOW_UNAUTHENTICATED=1` |
| rate-limit «слишком часто» | на `/login` в счётчике только POST (страницу можно открывать сколько угодно); глубже — nginx `limit_req`, не трогайте `RATE_LIMIT_ENABLED` в проде |
| лог job'а пуст после рестарта | job'ы in-memory; перезапустите сервис |
| Журнал заданий пуст / «отношение pgagent.pga_jobsteplog не существует» | pgAgent не установлен в базе `postgres` (других баз карточка не читает) |
| `pg_restore` killed by timeout | поднимите `SUBPROCESS_TIMEOUT` |
| БД пропала после ошибки | `DROP` уже прошёл — восстанавливайте из бэкапа повторно |

Логи приложения: stdout systemd/journal; audit: `restore_audit.log`
(basename путей, rotation 50 MB × 5).

---

## Разработка

- Файлы `.env`, `restore_audit.log`, `AGENTS.md`, `.omo/` исключены из git.
- Секреты и учётные данные **никогда** не попадают в коммиты.
- Стиль кода: без ORM, `shell=False`, типизация Config.
- Новый ключ в settings требует правок в `EDITABLE_KEYS` (`config.py`),
  `.env.example`, `settings.html`.
- Перед продакшеном прогоняйте smoke-тесты на реальном бэкапе.
