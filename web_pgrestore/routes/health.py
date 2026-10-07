"""Health endpoint."""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify

health_bp = Blueprint("health", __name__)


@health_bp.get("/health")
def health():
    cfg = current_app.config["APP_CFG"]
    return jsonify(
        {
            "status": "ok",
            "auth_enabled": cfg.auth_enabled,
        }
    )
