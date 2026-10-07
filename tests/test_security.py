"""Unit tests for web_pgrestore (path, auth, csrf, jobs, settings, open-mode)."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from web_pgrestore import create_app  # noqa: E402
from web_pgrestore.config import Config, update_env_file  # noqa: E402
from web_pgrestore.jobs import JobManager, manager  # noqa: E402
from web_pgrestore.restore import (  # noqa: E402
    is_valid_dbname,
    list_backups,
    resolve_backup_path,
)
from web_pgrestore.security import enforce_open_mode_guard  # noqa: E402


def make_cfg(**kwargs) -> Config:
    base = dict(
        PGHOST="/tmp",
        PGPORT=5432,
        PGUSER="postgres",
        PGPASSWORD="",
        BACKUP_ROOT="/mnt/backups/dump",
        DEFAULT_OWNER="usr1cv8",
        AUTH_USER="",
        AUTH_PASS="",
        FLASK_SECRET_KEY="test-secret",
        APP_HOST="127.0.0.1",
        APP_PORT=5000,
        SUBPROCESS_TIMEOUT=30,
        MAX_CONCURRENT_RESTORES=2,
        TRUST_PROXY=False,
        ALLOW_UNAUTHENTICATED=False,
        RATE_LIMIT_ENABLED=False,
    )
    base.update(kwargs)
    return Config(**base)


class TestPathValidation(unittest.TestCase):
    def test_dumpx_escape_blocked(self):
        root = "/mnt/backups/dump"
        evil = "/mnt/backups/dump_evil/x.backup"
        self.assertTrue(evil.startswith(root))
        self.assertFalse(evil == root or evil.startswith(root + os.sep))

    def test_resolve_basename_inside_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mydb"
            db.mkdir()
            (db / "x.backup").write_text("data")
            path = resolve_backup_path(tmp, "mydb", None, "x.backup")
            self.assertTrue(path.endswith("x.backup"))
            self.assertTrue(os.path.isfile(path))

    def test_resolve_escape_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp).parent / "evil.backup"
            try:
                outside.write_text("x")
                with self.assertRaises(ValueError):
                    resolve_backup_path(tmp, "mydb", None, str(outside))
            finally:
                if outside.exists():
                    outside.unlink()

    def test_resolve_dotdot_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mydb"
            db.mkdir()
            with self.assertRaises(ValueError):
                resolve_backup_path(tmp, "mydb", None, "../other/x.backup")

    def test_list_backups_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "dump"
            root.mkdir()
            evil = root / "evil_link"
            evil.symlink_to(Path(tmp).parent)
            cfg = make_cfg(BACKUP_ROOT=str(root))
            self.assertEqual(list_backups(cfg, "evil_link"), [])

    def test_list_backups_basenames(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "mydb"
            db.mkdir()
            (db / "b1.backup").write_text("1")
            (db / "b2.backup.gz").write_text("2")
            cfg = make_cfg(BACKUP_ROOT=str(tmp))
            files = list_backups(cfg, "mydb")
            self.assertEqual(sorted(files), ["b1.backup", "b2.backup.gz"])

    def test_invalid_dbname(self):
        self.assertFalse(is_valid_dbname("../etc"))
        self.assertFalse(is_valid_dbname(""))
        self.assertTrue(is_valid_dbname("db_1-2"))


class TestOpenModeGuard(unittest.TestCase):
    def test_loopback_without_auth_ok(self):
        enforce_open_mode_guard(make_cfg(APP_HOST="127.0.0.1"))

    def test_non_loopback_without_auth_raises(self):
        with self.assertRaises(RuntimeError):
            enforce_open_mode_guard(make_cfg(APP_HOST="0.0.0.0"))

    def test_non_loopback_with_allow_ok(self):
        enforce_open_mode_guard(
            make_cfg(APP_HOST="0.0.0.0", ALLOW_UNAUTHENTICATED=True)
        )

    def test_auth_enabled_ok(self):
        enforce_open_mode_guard(
            make_cfg(APP_HOST="0.0.0.0", AUTH_USER="a", AUTH_PASS="b")
        )


class TestAppSecurity(unittest.TestCase):
    def setUp(self):
        self.app = create_app(make_cfg())
        self.client = self.app.test_client()

    def _csrf_session(self, token="tok123"):
        with self.client.session_transaction() as s:
            s["_csrf"] = token

    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["status"], "ok")

    def test_api_unauthorized_without_session_when_auth_on(self):
        app = create_app(make_cfg(AUTH_USER="admin", AUTH_PASS="secret"))
        c = app.test_client()
        r = c.get("/api/databases")
        self.assertEqual(r.status_code, 401)

    def test_csrf_required_on_restore(self):
        self._csrf_session("tok123")
        r = self.client.post(
            "/restore",
            json={"dbname": "x", "backup": "a.backup"},
        )
        self.assertEqual(r.status_code, 400)

    def test_restore_with_csrf_invalid_backup_name(self):
        self._csrf_session("tok123")
        r = self.client.post(
            "/restore",
            json={"dbname": "bad/name", "backup": "a.backup", "_csrf": "tok123"},
            headers={"X-CSRF-Token": "tok123"},
        )
        self.assertEqual(r.status_code, 400)

    def test_index_has_no_pguser_leak(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertNotIn("PGUSER", body)
        self.assertIn("/settings", body)

    def test_login_required_redirect_html(self):
        app = create_app(make_cfg(AUTH_USER="admin", AUTH_PASS="secret"))
        c = app.test_client()
        r = c.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers.get("Location", ""))


class TestJobs(unittest.TestCase):
    def test_create_and_get(self):
        m = JobManager()
        job = m.create("db1", "db1", "a.backup")
        self.assertIs(m.get(job.id), job)
        self.assertEqual(job.status, "queued")

    def test_pipeline_missing_backup_fails(self):
        m = JobManager()
        cfg = make_cfg(BACKUP_ROOT=tempfile.mkdtemp())
        job = m.create("nodb", "nodb", "missing.backup")
        m._pipeline(job, cfg)
        self.assertEqual(job.status, "failed")
        self.assertFalse(job.result.get("ok"))
        self.assertTrue(job.logs)

    def test_global_manager_exists(self):
        self.assertIsInstance(manager, JobManager)


class TestSettingsEnvFile(unittest.TestCase):
    def test_update_env_file_upsert(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("PGUSER=postgres\n# comment\nBACKUP_ROOT=/old\n", encoding="utf-8")
            update_env_file(path, {"BACKUP_ROOT": "/new", "PGPORT": "5433"})
            text = path.read_text(encoding="utf-8")
            self.assertIn("BACKUP_ROOT=/new", text)
            self.assertIn("PGPORT=5433", text)
            self.assertIn("# comment", text)

    def test_settings_page_get(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("PGUSER=postgres\n", encoding="utf-8")
            cfg = make_cfg(ENV_PATH=env_path)
            app = create_app(cfg)
            c = app.test_client()
            with c.session_transaction() as s:
                s["_csrf"] = "t"
            r = c.get("/settings")
            self.assertEqual(r.status_code, 200)
            self.assertIn("BACKUP_ROOT", r.get_data(as_text=True))

    def test_settings_post_saves_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("PGUSER=postgres\n", encoding="utf-8")
            cfg = make_cfg(ENV_PATH=env_path)
            app = create_app(cfg)
            c = app.test_client()
            with c.session_transaction() as s:
                s["_csrf"] = "t"
            r = c.post(
                "/settings",
                data={
                    "_csrf": "t",
                    "BACKUP_ROOT": "/tmp/backups",
                    "PGPORT": "5433",
                    "TRUST_PROXY": "0",
                },
            )
            self.assertEqual(r.status_code, 200)
            body = r.get_data(as_text=True)
            self.assertIn("Сохранено", body)
            text = env_path.read_text(encoding="utf-8")
            self.assertIn("BACKUP_ROOT=/tmp/backups", text)
            self.assertIn("PGPORT=5433", text)
            self.assertEqual(cfg.BACKUP_ROOT, "/tmp/backups")
            self.assertEqual(cfg.PGPORT, 5433)

    def test_settings_secret_keep(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("PGPASSWORD=oldsecret\n", encoding="utf-8")
            cfg = make_cfg(ENV_PATH=env_path, PGPASSWORD="oldsecret")
            app = create_app(cfg)
            c = app.test_client()
            with c.session_transaction() as s:
                s["_csrf"] = "t"
            r = c.post(
                "/settings",
                data={
                    "_csrf": "t",
                    "PGPASSWORD": "",
                    "_PGPASSWORD_keep": "1",
                },
            )
            self.assertEqual(r.status_code, 200)
            self.assertEqual(cfg.PGPASSWORD, "oldsecret")
            self.assertIn("PGPASSWORD=oldsecret", env_path.read_text(encoding="utf-8"))


class TestRestoreRouteValidation(unittest.TestCase):
    def setUp(self):
        self.app = create_app(make_cfg())
        self.client = self.app.test_client()
        with self.client.session_transaction() as s:
            s["_csrf"] = "tok"

    def test_invalid_json(self):
        r = self.client.post("/restore", data="not-json", content_type="text/plain")
        self.assertEqual(r.status_code, 400)

    def test_missing_backup(self):
        r = self.client.post(
            "/restore",
            json={"dbname": "db1", "_csrf": "tok"},
            headers={"X-CSRF-Token": "tok"},
        )
        self.assertEqual(r.status_code, 400)

    def test_progress_unknown_job(self):
        r = self.client.get("/restore/progress/doesnotexist")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
