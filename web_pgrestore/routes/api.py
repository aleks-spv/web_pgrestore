"""JSON APIs: databases + backups."""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from .. import db
from ..restore import list_backups

api_bp = Blueprint("api", __name__, url_prefix="/api")


@api_bp.get("/databases")
def databases():
    cfg = current_app.config["APP_CFG"]
    try:
        names = db.get_databases(cfg)
    except Exception:
        current_app.logger.exception("Failed to fetch databases")
        return jsonify({"error": "Internal server error"}), 500
    return jsonify({"databases": names})


@api_bp.get("/backups")
def backups():
    cfg = current_app.config["APP_CFG"]
    dbname = request.args.get("db", "")
    if not dbname:
        return jsonify({"error": "DB name required"}), 400
    files = list_backups(cfg, dbname)
    return jsonify({"database": dbname, "backups": files})
