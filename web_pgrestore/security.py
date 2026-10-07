"""Auth, CSRF, rate limiting, ProxyFix, open-mode guard."""
from __future__ import annotations

import functools
import hmac
import secrets
import time

from flask import (
    Response,
    abort,
    current_app,
    jsonify,
    make_response,
    redirect,
    request,
    session,
    url_for,
)

from .config import Config


def get_cfg() -> Config:
    return current_app.config["APP_CFG"]


def rotate_csrf_token(resp) -> None:
    """Issue a fresh CSRF token, invalidating the previous one.

    Called after login/logout so that a token captured before authentication
    cannot be reused afterwards (session-fixation defense).
    """
    session.pop("_csrf", None)
    token = get_csrf_token()
    _write_csrf_cookie(resp, token)


# ---------- ProxyFix (gated) ----------


def apply_proxy_fix(app) -> None:
    """Trust X-Forwarded-* only when TRUST_PROXY is enabled."""
    cfg: Config = app.config["APP_CFG"]
    if cfg.TRUST_PROXY:
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


def enforce_open_mode_guard(cfg: Config) -> None:
    """Refuse non-loopback bind without auth unless explicitly allowed."""
    if cfg.auth_enabled:
        return
    if cfg.loopback_host:
        return
    if cfg.ALLOW_UNAUTHENTICATED:
        return
    raise RuntimeError(
        "AUTH_USER/AUTH_PASS are empty and APP_HOST=%r is not loopback. "
        "Set credentials in .env/Settings, or set ALLOW_UNAUTHENTICATED=1."
        % (cfg.APP_HOST,)
    )


# ---------- Session / CSRF ----------


_CSRF_COOKIE_NAME = "_csrf_token"


def _get_csrf_cookie() -> str | None:
    """Read the dedicated CSRF token from the httpOnly cookie."""
    return request.cookies.get(_CSRF_COOKIE_NAME)


def _write_csrf_cookie(resp, token: str) -> None:
    """Write the dedicated CSRF cookie (persistent, httpOnly, SameSite=Strict).

    `max_age` ~1 year mirrors Django's CSRF_COOKIE_AGE: without persistence a
    browser restart / new session drops the cookie and POSTs lose the token.
    """
    resp.set_cookie(
        _CSRF_COOKIE_NAME,
        token,
        httponly=True,
        samesite="Strict",
        secure=False,   # dev over plain HTTP; tighten to True behind HTTPS
        max_age=31536000,
        path="/",
    )


def get_csrf_token() -> str:
    """Return a per-session CSRF token, mirrored into its own httpOnly cookie.

    The token lives in `session["_csrf"]` (signed by the server) AND is
    mirrored into a *separate* durable httpOnly cookie. Validation accepts a
    match against either one — so a browser that drops/never returns the
    session cookie no longer loses the token. That loss was the actual cause
    of the "CSRF token missing or invalid" lockout on /login.
    """
    token = session.get("_csrf")
    if not token:
        token = secrets.token_hex(32)
        session["_csrf"] = token
    return token


def _get_token() -> str | None:
    """Read the submitted CSRF token from header, form or JSON body."""
    token = request.headers.get("X-CSRF-Token")
    if not token and request.form:
        token = request.form.get("_csrf")
    if not token:
        data = request.get_json(silent=True)
        if isinstance(data, dict):
            token = data.get("_csrf")
    return token


def validate_csrf() -> None:
    """Verify CSRF: submitted token must match session OR the dedicated cookie.

    Match-against-either is deliberate:
      * session["_csrf"] is server-signed (Flask signed cookie) — strong;
      * `_csrf_token` httpOnly cookie is the durable fallback when the
        session cookie is lost — fixes the lockout without disabling CSRF.
    An attacker still cannot forge the request: they must know the token
    value to place it in the form/header, and it is HttpOnly + SameSite=Strict.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    token = _get_token()
    expected_session = session.get("_csrf") or ""
    expected_cookie = _get_csrf_cookie() or ""
    if not token or (
        not hmac.compare_digest(str(token), expected_session)
        and not hmac.compare_digest(str(token), expected_cookie)
    ):
        abort(make_response(jsonify({"error": "CSRF token missing or invalid"}), 400))


def session_valid() -> bool:
    cfg = get_cfg()
    if not cfg.auth_enabled:
        return True
    return session.get("auth") is True


def login_required_api():
    """Return 401 JSON for API-ish paths when unauthenticated.

    /login and /logout are always public: the login form must be reachable,
    and logout just clears the session (idempotent even if unauthenticated).
    /health is public too — monitoring (systemd/nginx checks) must work
    regardless of auth state.
    """
    if session_valid():
        return None
    session.clear()
    path = request.path
    if path in {"/login", "/logout", "/health"}:
        return None
    if path.startswith("/api/") or path.startswith("/restore") or path.startswith("/settings"):
        return jsonify({"error": "Unauthorized"}), 401
    return redirect(url_for("auth.login"))


def register_security(app) -> None:
    apply_proxy_fix(app)

    cfg: Config = app.config["APP_CFG"]
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SECURE"] = bool(cfg.SESSION_COOKIE_SECURE)
    app.config["PERMANENT_SESSION_LIFETIME"] = 3600
    app.secret_key = cfg.FLASK_SECRET_KEY

    @app.before_request
    def _before_request():
        # CSRF on state-changing requests (login included)
        validate_csrf()
        return login_required_api()

    @app.context_processor
    def _inject():
        cfg = get_cfg()
        return {
            "csrf_token": get_csrf_token(),
            "auth_enabled": cfg.auth_enabled,
            "auth_disabled_banner": not cfg.auth_enabled,
        }

    @app.after_request
    def _mirror_csrf_cookie(resp: Response) -> Response:
        """Mirror the per-session CSRF token into its own httpOnly cookie.

        Having the token in a separate durable cookie means a browser that
        drops the session cookie (e.g. SESSION_COOKIE_SECURE with plain HTTP,
        or SameSite restrictions) still receives the token and can submit it —
        eliminating the "CSRF token missing or invalid" lockout on /login.
        """
        _write_csrf_cookie(resp, get_csrf_token())
        return resp

    @app.after_request
    def _security_headers(resp: Response) -> Response:
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp


# ---------- Rate limiting (in-memory, pruned) ----------


def rate_limited(max_calls: int = 5, period: int = 60):
    """Simple per-IP rate limit with periodic prune of stale keys."""

    call_history: dict[str, list[float]] = {}

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                if not get_cfg().RATE_LIMIT_ENABLED:
                    return func(*args, **kwargs)
            except Exception:
                pass
            ip = request.remote_addr or "unknown"
            now = time.time()
            if len(call_history) > 512:
                for key in list(call_history):
                    stamps = call_history[key]
                    if not stamps or now - stamps[-1] > period:
                        del call_history[key]
            timestamps = [t for t in call_history.get(ip, []) if now - t < period]
            if len(timestamps) >= max_calls:
                return jsonify({"error": "Rate limit exceeded. Try later."}), 429
            timestamps.append(now)
            call_history[ip] = timestamps
            return func(*args, **kwargs)

        return wrapper

    return decorator


def check_login(cfg: Config, username: str, password: str) -> bool:
    if not cfg.auth_enabled:
        return True
    if username != cfg.AUTH_USER:
        return False
    return hmac.compare_digest(
        password.encode("utf-8"), cfg.AUTH_PASS.encode("utf-8")
    )
