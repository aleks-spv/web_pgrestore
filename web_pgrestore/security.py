"""Auth, CSRF, rate limiting, ProxyFix, open-mode guard."""
from __future__ import annotations

import functools
import hmac
import secrets
import time
from urllib.parse import urlparse

from flask import (
    Response,
    current_app,
    jsonify,
    make_response,
    redirect,
    render_template,
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

# Shown when CSRF validation fails on the login form. It deliberately contains
# "CSRF" so it stays greppable by tests; the page is re-rendered with a FRESH
# token, which makes the failure self-healing instead of a JSON dead-end.
_LOGIN_CSRF_ERROR = (
    "CSRF-токен недействителен или страница устарела. "
    "Форма обновлена — введите логин и пароль ещё раз."
)


def _get_csrf_cookie() -> str | None:
    """Read the dedicated CSRF token from the httpOnly cookie."""
    return request.cookies.get(_CSRF_COOKIE_NAME)


def _write_csrf_cookie(resp, token: str) -> None:
    """Write the dedicated CSRF cookie (persistent, httpOnly, SameSite=Lax).

    `max_age` ~1 year mirrors Django's CSRF_COOKIE_AGE: without persistence a
    browser restart / new session drops the cookie and POSTs lose the token.
    `samesite="Lax"` matches Django's CSRF_COOKIE_SAMESITE default and Flask's
    documented recommendation; `secure` follows SESSION_COOKIE_SECURE so an
    HTTPS deployment never sends the token over plaintext.
    """
    resp.set_cookie(
        _CSRF_COOKIE_NAME,
        token,
        httponly=True,
        samesite="Lax",
        secure=bool(get_cfg().SESSION_COOKIE_SECURE),
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


def _digest(value) -> bytes:
    """UTF-8 bytes for constant-time comparison.

    `hmac.compare_digest()` raises TypeError on non-ASCII str, which would turn
    a corrupted/forged token into a 500 instead of a 400. Django compares bytes
    for exactly this reason.
    """
    return str(value).encode("utf-8", "replace")


def _token_matches(submitted, expected) -> bool:
    return hmac.compare_digest(_digest(submitted), _digest(expected))


def _strip_default_port(scheme: str, netloc: str) -> str:
    netloc = (netloc or "").lower()
    if scheme == "http" and netloc.endswith(":80"):
        return netloc[:-3]
    if scheme == "https" and netloc.endswith(":443"):
        return netloc[:-4]
    return netloc


def _request_origin() -> str:
    return f"{request.scheme}://{_strip_default_port(request.scheme, request.host)}"


def _trusted_origins() -> list[str]:
    raw = getattr(get_cfg(), "CSRF_TRUSTED_ORIGINS", "") or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


def _origin_matches(origin: str) -> bool:
    """True when `origin` is this request's own origin or an allow-listed one.

    Mirrors Django's `CsrfViewMiddleware` Origin check. `Origin: null`
    (sandboxed context) never matches, and the scheme must match too, so an
    http page cannot authorise an https request and vice versa.
    """
    origin = (origin or "").strip()
    if not origin or origin == "null":
        return False
    parsed = urlparse(origin)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    origin_norm = f"{parsed.scheme}://{_strip_default_port(parsed.scheme, parsed.netloc)}"
    if origin_norm == _request_origin():
        return True
    for allowed in _trusted_origins():
        if "://" not in allowed:
            allowed = f"{request.scheme}://{allowed}"
        other = urlparse(allowed)
        if other.scheme == parsed.scheme and other.netloc:
            if _strip_default_port(other.scheme, other.netloc) == _strip_default_port(
                parsed.scheme, parsed.netloc
            ):
                return True
    return False


def _origin_ok() -> bool:
    """Validate the browser-supplied Origin, falling back to Referer.

    No Origin AND no Referer means a non-browser client (curl, test client):
    there is no cross-origin guarantee to check, so we rely on the token only.
    That is what keeps CLI usage and the unit tests working.
    """
    origin = request.headers.get("Origin")
    if origin is not None:
        return _origin_matches(origin)
    referer = request.headers.get("Referer")
    if referer:
        parsed = urlparse(referer)
        if parsed.scheme and parsed.netloc:
            return _origin_matches(f"{parsed.scheme}://{parsed.netloc}")
    return True


def _csrf_rejected() -> Response:
    """Refuse a request that failed CSRF validation.

    On `/login` we re-render the form with a FRESH token instead of returning
    raw JSON: Django answers with a rendered CSRF_FAILURE_VIEW, and a JSON body
    here left the user stuck on a form carrying a dead token.
    """
    if request.path == "/login":
        session.pop("_csrf", None)
        get_csrf_token()  # fresh token for the re-rendered form
        return make_response(render_template("login.html", error=_LOGIN_CSRF_ERROR), 400)
    return make_response(jsonify({"error": "CSRF token missing or invalid"}), 400)


def validate_csrf() -> Response | None:
    """Verify Origin + CSRF token; return a response when the request is refused.

    Match-against-either is deliberate:
      * session["_csrf"] is server-signed (Flask signed cookie) — strong;
      * `_csrf_token` httpOnly cookie is the durable fallback when the
        session cookie is lost — fixes the lockout without disabling CSRF.
    An attacker still cannot forge the request: they must know the token value
    to place it in the form/header, and the cookie is HttpOnly + SameSite=Lax.
    Returns None when the request passes, so the caller can continue.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    if not _origin_ok():
        return _csrf_rejected()
    token = _get_token()
    expected_session = session.get("_csrf") or ""
    expected_cookie = _get_csrf_cookie() or ""
    if not token or (
        not _token_matches(token, expected_session)
        and not _token_matches(token, expected_cookie)
    ):
        return _csrf_rejected()
    return None


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
    /favicon.ico is public too: otherwise it answers with a 302 to /login,
    which both rotates the CSRF token and counts against the /login rate
    limit (that is where the phantom `GET /login 429` came from).
    """
    if session_valid():
        return None
    # Drop everything BUT the CSRF token. Clearing the whole session here made
    # get_csrf_token() mint a brand-new token on every unauthenticated
    # response, and after_request mirrors it into _csrf_token — so any stray
    # request between opening the login form and submitting it left the page
    # holding a token the server no longer knew: 400.
    # Rotation still happens where it is wanted: login and logout both call
    # rotate_csrf_token().
    csrf = session.get("_csrf")
    session.clear()
    if csrf:
        session["_csrf"] = csrf
    path = request.path
    if path in {"/login", "/logout", "/health", "/favicon.ico"}:
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
        csrf_response = validate_csrf()
        if csrf_response is not None:
            return csrf_response
        return login_required_api()

    @app.context_processor
    def _inject():
        cfg = get_cfg()
        return {
            "csrf_token": get_csrf_token(),
            "auth_enabled": cfg.auth_enabled,
            "auth_disabled_banner": not cfg.auth_enabled,
            # Who is signed in — used by the "only mine" filters in the UI.
            "username": session.get("username") or "",
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
        # F3: this response carries the CSRF token (in the Set-Cookie and,
        # for pages, in the HTML form). Never let the browser or an upstream
        # proxy cache it — a page replayed from the back/forward cache holds
        # a token that has since rotated, and the submit then fails with
        # "токен устарел".
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    @app.after_request
    def _security_headers(resp: Response) -> Response:
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp


# ---------- Rate limiting (in-memory, pruned) ----------


def rate_limited(
    max_calls: int = 5,
    period: int = 60,
    methods: tuple[str, ...] | None = None,
    html_template: str | None = None,
):
    """Simple per-IP rate limit with periodic prune of stale keys.

    methods — only these HTTP methods are counted and checked. A browser page
        must not lock a user out just by being opened: GET stays out of the
        counter so 10 refreshes of /login cannot beat 10 real login attempts.
    html_template — render this template (with `error=`) for a navigation
        request instead of a bare JSON body, so a real browser gets a page it
        can act on. JSON/HTML is chosen the same way auth.login does it.
    """

    call_history: dict[str, list[float]] = {}

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                if not get_cfg().RATE_LIMIT_ENABLED:
                    return func(*args, **kwargs)
            except Exception:
                pass
            if methods is not None and request.method.upper() not in methods:
                return func(*args, **kwargs)
            ip = request.remote_addr or "unknown"
            now = time.time()
            if len(call_history) > 512:
                for key in list(call_history):
                    stamps = call_history[key]
                    if not stamps or now - stamps[-1] > period:
                        del call_history[key]
            timestamps = [t for t in call_history.get(ip, []) if now - t < period]
            if len(timestamps) >= max_calls:
                wants_html = (
                    html_template
                    and not request.is_json
                    and request.accept_mimetypes.best != "application/json"
                )
                if wants_html:
                    return (
                        render_template(
                            html_template,
                            error="Слишком много попыток. Подождите пару минут.",
                        ),
                        429,
                    )
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
