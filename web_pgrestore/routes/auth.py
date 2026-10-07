"""Login / logout."""
from __future__ import annotations

from flask import (
    Blueprint,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from ..security import check_login, rate_limited, session_valid

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["GET", "POST"])
@rate_limited(max_calls=10, period=900)
def login():
    cfg = current_app.config["APP_CFG"]
    need_auth = cfg.auth_enabled
    if request.method == "POST":
        username = str(request.form.get("username") or "")
        password = str(request.form.get("password") or "")
        if check_login(cfg, username, password):
            session["auth"] = True
            session.permanent = True
            if request.accept_mimetypes.best == "application/json" or request.is_json:
                return jsonify({"ok": True})
            return redirect(url_for("pages.index"))
        if request.accept_mimetypes.best == "application/json" or request.is_json:
            return jsonify({"error": "Неверный логин или пароль"}), 401
        return render_template("login.html", error="Неверный логин или пароль"), 401
    if session_valid() and need_auth:
        return redirect(url_for("pages.index"))
    return render_template("login.html", error="", require_auth=need_auth), 200


@auth_bp.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
