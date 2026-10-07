"""Flask application factory for web_pgrestore."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask

from .config import ROOT, Config
from .security import enforce_open_mode_guard, register_security

__all__ = ["create_app", "ROOT"]


def _setup_logging(app: Flask) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    audit_path = Path.cwd() / "restore_audit.log"
    try:
        handler = RotatingFileHandler(
            audit_path, maxBytes=50_000_000, backupCount=5, encoding="utf-8"
        )
        handler.setLevel(logging.INFO)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        audit_logger = logging.getLogger("restore_audit")
        audit_logger.setLevel(logging.INFO)
        audit_logger.addHandler(handler)
        audit_logger.propagate = False
    except Exception:
        app.logger.warning("Audit logger unavailable")


def create_app(config: Config | None = None) -> Flask:
    cfg = config or Config.from_env()
    enforce_open_mode_guard(cfg)

    app = Flask(
        __name__,
        template_folder=str(ROOT / "templates"),
        static_folder=None,
    )
    app.config["APP_CFG"] = cfg
    app.secret_key = cfg.FLASK_SECRET_KEY
    app.config["JSON_SORT_KEYS"] = False

    _setup_logging(app)
    register_security(app)

    from .routes.auth import auth_bp
    from .routes.health import health_bp
    from .routes.pages import pages_bp
    from .routes.api import api_bp
    from .routes.restore_routes import restore_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(health_bp)
    app.register_blueprint(pages_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(restore_bp)

    return app
