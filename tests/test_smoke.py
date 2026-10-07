"""Smoke test: create_app routes + settings save without touching real .env."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from web_pgrestore import create_app
from web_pgrestore.config import Config


class TestSmoke(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.env_path = root / "runtime.env"
        self.backup_root = root / "backups"
        self.cfg = Config(
            ENV_PATH=self.env_path,
            BACKUP_ROOT=str(self.backup_root),
            RATE_LIMIT_ENABLED=False,
            FLASK_SECRET_KEY="smoke",
            AUTH_USER="",
            AUTH_PASS="",
            APP_HOST="127.0.0.1",
        )
        self.app = create_app(self.cfg)
        self.client = self.app.test_client()

    def tearDown(self):
        self._tmp.cleanup()

    def test_routes_registered(self):
        rules = {str(r) for r in self.app.url_map.iter_rules()}
        for expected in (
            "/health",
            "/",
            "/login",
            "/logout",
            "/settings",
            "/api/databases",
            "/api/backups",
            "/restore",
            "/restore/progress/<job_id>",
            "/restore/jobs/<job_id>",
        ):
            self.assertIn(expected, rules)

    def test_health_and_index(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["status"], "ok")
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("/settings", body)
        self.assertNotIn("PGUSER", body)

    def test_settings_get_and_post(self):
        with self.client.session_transaction() as s:
            s["_csrf"] = "t"
        r = self.client.get("/settings")
        self.assertEqual(r.status_code, 200)
        self.assertIn("BACKUP_ROOT", r.get_data(as_text=True))
        new_root = str(Path(self._tmp.name) / "bk2")
        r = self.client.post(
            "/settings",
            data={"_csrf": "t", "BACKUP_ROOT": new_root, "PGPORT": "5433"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("Сохранено", body)
        text = self.env_path.read_text(encoding="utf-8")
        self.assertIn(f"BACKUP_ROOT={new_root}", text)
        self.assertIn("PGPORT=5433", text)
        self.assertEqual(self.cfg.BACKUP_ROOT, new_root)
        self.assertEqual(self.cfg.PGPORT, 5433)

    def test_csrf_on_restore(self):
        with self.client.session_transaction() as s:
            s["_csrf"] = "tok"
        r = self.client.post("/restore", json={"dbname": "x", "backup": "a.backup"})
        self.assertEqual(r.status_code, 400)

    def test_api_401_when_auth_on(self):
        app = create_app(
            Config(
                ENV_PATH=self.env_path,
                BACKUP_ROOT=str(self.backup_root),
                RATE_LIMIT_ENABLED=False,
                FLASK_SECRET_KEY="smoke",
                AUTH_USER="admin",
                AUTH_PASS="secret",
            )
        )
        c = app.test_client()
        r = c.get("/api/databases")
        self.assertEqual(r.status_code, 401)
        r = c.get("/")
        self.assertEqual(r.status_code, 302)


if __name__ == "__main__":
    unittest.main(verbosity=2)
