"""HTML pages: index + settings."""
from __future__ import annotations

import datetime
import json
import os
import re
import decimal
from contextlib import closing
from pathlib import Path

import psycopg2
from flask import (
    Blueprint,
    Response,
    current_app,
    jsonify,
    render_template,
    request,
)

from ..config import ROOT, EDITABLE_KEYS
from ..config import (
    BOOL_KEYS,
    INT_KEYS,
    SECRET_KEYS,
    update_env_file,
)
from ..db import get_conn
from ..security import rate_limited

pages_bp = Blueprint("pages", __name__)


# The app ships with static_folder=None, so /static/* and Flask's default
# favicon route do not exist. Without this stub a browser falls back to
# /favicon.ico, login_required_api answers 302 -> /login, and that extra
# request used to rotate the CSRF token of an already-open form and burn a
# slot in the /login rate limit.
_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="14" fill="#2f6feb"/>'
    '<path d="M32 16c9.4 0 17 7.6 17 17s-7.6 17-17 17S15 42.4 15 33" '
    'fill="none" stroke="#fff" stroke-width="6" stroke-linecap="round"/>'
    '<circle cx="32" cy="33" r="6" fill="#fff"/>'
    "</svg>"
)


@pages_bp.route("/favicon.ico")
def favicon() -> Response:
    """Plain inline SVG placeholder — no icon file is bundled with the app."""
    return Response(_FAVICON_SVG, mimetype="image/svg+xml")


def _env_path_display(env_path) -> str:
    """Show only the env file name — full path is never exposed to the client."""
    if env_path is None:
        return ".env"
    return os.path.basename(str(env_path))


# ---------- Audit log parsing (mirrors _setup_logging path) ----------

_AUDIT_LOG_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{1,6})\s+"
    r"(?P<level>\S+)\s+"
    r"(?P<payload>.*)$",
    re.S,
)


def _parse_audit_line(line: str) -> dict:
    """Parse one audit log line into {ts, level, ...payload}.

    New format: ``<timestamp> <LEVEL> <JSON>``
    Old/manual format: ``<timestamp> | <JSON>``
    """
    line = line.strip()
    if not line:
        return {}

    # Old '|' separated manual lines (e.g. the historical hand-written entries)
    if "|" in line and line.index("|") > 10:
        ts_end = line.index("|")
        ts = line[:ts_end].strip()
        rest = line[ts_end + 1:].strip()
        try:
            payload = json.loads(rest)
        except json.JSONDecodeError:
            payload = {"raw": rest}
        return {"ts": ts, "level": "-", **payload}

    m = _AUDIT_LOG_RE.match(line)
    if m:
        payload: dict = {}
        try:
            payload = json.loads(m.group("payload"))
        except json.JSONDecodeError:
            payload = {"raw": m.group("payload")}
        return {
            "ts": m.group("ts"),
            "level": m.group("level"),
            **payload,
        }
    return {"ts": "", "level": "-", "raw": line}


def _audit_log_path() -> Path:
    """Location of restore_audit.log — mirrors _setup_logging."""
    cfg = current_app.config["APP_CFG"]
    candidate: Path | None = None
    env_path = cfg.ENV_PATH
    if env_path:
        candidate = Path(env_path).parent / "restore_audit.log"
    cwd = Path.cwd() / "restore_audit.log"
    # prefer the project-root location (where the RotatingFileHandler writes)
    if candidate and candidate != cwd and candidate.exists():
        return candidate
    return cwd


@pages_bp.get("/api/audit-log")
def audit_log_view():
    """Read-only API: returns parsed audit log entries (used by /settings)."""
    log_path = _audit_log_path()
    try:
        with log_path.open(encoding="utf-8") as fh:
            entries = [_parse_audit_line(l) for l in fh.readlines()]
    except OSError:
        entries = []
    return jsonify({"entries": entries})


# pgAgent's step log carries only a step id; the UI wants the human names of
# the job and the step, plus the printed output (pg_dump/pg_restore/REINDEX
# logs). jsloutput is trimmed to 20k chars — a single step can print megabytes
# and the card must stay usable.
_PGAGENT_SQL = """
SELECT j.jobname AS job,
       s.jstname AS step,
       {cols}
  FROM pgagent.pga_jobsteplog l
  JOIN pgagent.pga_jobstep s ON s.jstid = l.jsljstid
  JOIN pgagent.pga_job     j ON j.jobid = s.jstjobid
 WHERE (%s = '' OR j.jobname ILIKE '%%' || %s || '%%')
   AND (%s = '' OR l.jslstatus::text = %s)
 ORDER BY l.jslstart DESC NULLS LAST, l.jslid DESC
 LIMIT %s
"""


def _pgagent_columns(cur):
    """Probe the schema and build the SELECT list, plus `has_end`.

    Duration lives in `jslduration` on this pgAgent and in `jslend` on some
    other releases; if neither exists the column is kept and is NULL, because
    dropping it would shift every table cell one position left. The UI then
    renders an em dash.
    """
    cur.execute(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema = 'pgagent'"
        "   AND table_name = 'pga_jobsteplog'"
    )
    have = {row[0] for row in cur.fetchall()}

    started = "l.jslstart AS started" if "jslstart" in have else "NULL AS started"

    # Duration: this pgAgent stores it straight away as `jslduration` (interval).
    # Fall back to `jslend - jslstart` on releases that lack it, and to NULL
    # rather than dropping the column — otherwise every table cell would shift
    # one position left. Seconds come back as a plain number for the UI.
    if "jslduration" in have:
        dur_src = "l.jslduration"
    elif "jslend" in have and "jslstart" in have:
        dur_src = "(l.jslend - l.jslstart)"
    else:
        dur_src = "NULL::interval"

    return (
        "l.jslstatus::text AS status, "
        "l.jslresult AS result, "
        f"{started}, "
        f"EXTRACT(EPOCH FROM {dur_src}) AS duration, "
        "left(l.jsloutput, 20000) AS output"
    )


def _jsonable(value):
    """psycopg2 hands back datetime/Decimal — not JSON-serialisable as-is."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return str(value)
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", "replace")
    return str(value)


@pages_bp.get("/api/pgagent-log")
def pgagent_log_view():
    """Read-only pgAgent job log (used by /settings).

    pgAgent is installed in the `postgres` database on this host, and there
    is deliberately no database picker — one less thing to get wrong. A
    missing `pgagent` schema (or an unreachable database) comes back as
    `error` inside a 200 payload so the page can show a readable message
    instead of a 500.
    """
    job = (request.args.get("job") or "").strip()[:200]
    status = (request.args.get("status") or "").strip()
    if status not in ("", "s", "f", "d"):
        return jsonify({"entries": [], "error": f"Неизвестный статус: {status}"}), 400

    try:
        limit = int(request.args.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, 1000))

    cfg = current_app.config["APP_CFG"]
    try:
        with closing(get_conn(cfg, dbname="postgres")) as conn:
            conn.autocommit = True
            with closing(conn.cursor()) as cur:
                sql_cols = _pgagent_columns(cur)
                sql = _PGAGENT_SQL.format(cols=sql_cols)
                cur.execute(sql, (job, job, status, status, limit))
                columns = [d[0] for d in cur.description]
                rows = cur.fetchall()
    except psycopg2.Error as exc:
        detail = str(getattr(exc, "pgerror", None) or exc).strip().splitlines()
        return jsonify({
            "entries": [],
            "limit": limit,
            "error": f"postgres: {detail[0] if detail else 'нет доступа к базе'}",
        }), 200

    entries = [
        {k: _jsonable(v) for k, v in zip(columns, row)} for row in rows
    ]
    return jsonify({"entries": entries, "limit": limit})


_SECRET_SENTINEL = "__KEEP__"
_SECRET_CLEAR = "__CLEAR__"


@pages_bp.route("/")
def index():
    cfg = current_app.config["APP_CFG"]
    return render_template("index.html", auth_disabled_banner=not cfg.auth_enabled)


@pages_bp.route("/settings", methods=["GET", "POST"])
@rate_limited(max_calls=20, period=60)
def settings():
    cfg = current_app.config["APP_CFG"]
    error = None
    saved = False
    changed: list[str] = []

    if request.method == "POST":
        updates: dict[str, str] = {}
        try:
            for key in EDITABLE_KEYS:
                if key not in request.form:
                    continue
                raw = request.form.get(key, "")
                if key in SECRET_KEYS:
                    if raw == _SECRET_CLEAR:
                        updates[key] = ""
                        continue
                    if raw == _SECRET_SENTINEL or raw == "":
                        continue  # keep existing
                    updates[key] = raw
                    continue
                if raw == "":
                    continue
                value = cfg.coerce(key, raw)
                if isinstance(value, bool):
                    updates[key] = "1" if value else "0"
                else:
                    updates[key] = str(value)
                # runtime apply after validation of all keys
                setattr(cfg, key, value)
        except ValueError as exc:
            error = str(exc)
            updates = {}

        if error is None and updates:
            try:
                for key, raw_val in updates.items():
                    if key in SECRET_KEYS:
                        setattr(cfg, key, raw_val)
                    elif key in BOOL_KEYS:
                        setattr(cfg, key, raw_val in {"1", "true", "yes", "on"})
                    elif key in INT_KEYS:
                        setattr(cfg, key, int(raw_val))
                    else:
                        setattr(cfg, key, raw_val)
                update_env_file(cfg.ENV_PATH, updates)
                # refresh live Flask secret so sessions keep working after key change
                current_app.secret_key = cfg.FLASK_SECRET_KEY
                changed = sorted(updates.keys())
                saved = True
            except Exception as exc:  # noqa: BLE001
                error = f"Failed to save .env: {exc}"

    view = {}
    for key in EDITABLE_KEYS:
        val = getattr(cfg, key, "")
        if key in SECRET_KEYS:
            view[key] = {"is_secret": True, "set": bool(val), "value": ""}
        elif key in BOOL_KEYS:
            view[key] = {
                "is_secret": False,
                "is_bool": True,
                "value": "1" if val else "0",
            }
        elif key in INT_KEYS:
            view[key] = {"is_secret": False, "is_int": True, "value": str(val)}
        else:
            view[key] = {"is_secret": False, "value": str(val)}

    return render_template(
        "settings.html",
        keys=EDITABLE_KEYS,
        view=view,
        error=error,
        saved=saved,
        changed=changed,
        env_path=_env_path_display(cfg.ENV_PATH),
        auth_disabled_banner=not cfg.auth_enabled,
        secret_sentinel=_SECRET_SENTINEL,
        secret_clear=_SECRET_CLEAR,
    )


@pages_bp.route("/api/settings", methods=["GET"])
def api_settings_meta():
    cfg = current_app.config["APP_CFG"]
    return jsonify(
        {
            "auth_enabled": cfg.auth_enabled,
            "trust_proxy": cfg.TRUST_PROXY,
            "allow_unauthenticated": cfg.ALLOW_UNAUTHENTICATED,
            "max_concurrent_restores": cfg.MAX_CONCURRENT_RESTORES,
            "env_path": _env_path_display(cfg.ENV_PATH),
        }
    )
