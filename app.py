#!/usr/bin/env python3
"""Entry point for web_pgrestore.

Run: python app.py
"""
from web_pgrestore import create_app
from web_pgrestore.config import Config

app = create_app()


if __name__ == "__main__":
    cfg: Config = app.config["APP_CFG"]
    # Werkzeug dev server is fine for a single-admin internal tool.
    # For production prefer gunicorn behind a reverse proxy (see README).
    app.run(host=cfg.APP_HOST, port=cfg.APP_PORT, debug=False, threaded=True)
