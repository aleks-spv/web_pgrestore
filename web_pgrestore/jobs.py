"""In-memory restore jobs + background pipeline runner."""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from . import db
from .config import Config
from .restore import resolve_backup_path, resolve_owner, run_pg_restore

logger = logging.getLogger(__name__)
_audit_logger = logging.getLogger("restore_audit")

_MAX_JOBS = 100


@dataclass
class RestoreJob:
    id: str
    dbname: str
    source_db: str
    backup_name: str
    status: str = "queued"  # queued|running|success|failed
    logs: list[str] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    ip: str = ""
    _cond: threading.Condition = field(default_factory=threading.Condition, repr=False)

    def log(self, line: str) -> None:
        with self._cond:
            self.logs.append(line)
            self._cond.notify_all()

    def set_status(self, status: str, result: dict | None = None) -> None:
        with self._cond:
            self.status = status
            if result is not None:
                self.result = result
            if status in {"success", "failed"}:
                self.finished_at = time.time()
            self._cond.notify_all()

    def snapshot_logs(self, start: int = 0) -> list[str]:
        with self._cond:
            return list(self.logs[start:])

    @property
    def done(self) -> bool:
        return self.status in {"success", "failed"}


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, RestoreJob] = {}
        self._order: list[str] = []
        self._guard = threading.Lock()
        self._sem: threading.Semaphore | None = None
        self._sem_size: int | None = None
        self._db_locks: dict[str, threading.Lock] = {}
        self._db_locks_guard = threading.Lock()

    def db_lock(self, dbname: str) -> threading.Lock:
        with self._db_locks_guard:
            if dbname not in self._db_locks:
                self._db_locks[dbname] = threading.Lock()
            return self._db_locks[dbname]

    def _semaphore(self, size: int) -> threading.Semaphore:
        if self._sem is None or self._sem_size != size:
            self._sem = threading.BoundedSemaphore(max(1, size))
            self._sem_size = size
        return self._sem

    def get(self, job_id: str) -> RestoreJob | None:
        with self._guard:
            return self._jobs.get(job_id)

    def create(self, dbname: str, source_db: str, backup_name: str) -> RestoreJob:
        job = RestoreJob(
            id=secrets.token_hex(8),
            dbname=dbname,
            source_db=source_db,
            backup_name=backup_name,
        )
        with self._guard:
            self._jobs[job.id] = job
            self._order.append(job.id)
            while len(self._order) > _MAX_JOBS:
                old = self._order.pop(0)
                old_job = self._jobs.get(old)
                if old_job and old_job.done:
                    self._jobs.pop(old, None)
        return job

    def try_start(self, job: RestoreJob, cfg: Config) -> bool:
        sem = self._semaphore(cfg.MAX_CONCURRENT_RESTORES)
        if not sem.acquire(blocking=False):
            return False
        job.set_status("running")
        t = threading.Thread(
            target=self._run,
            args=(job, cfg, sem),
            daemon=True,
            name=f"restore-{job.id}",
        )
        t.start()
        return True

    def _run(self, job: RestoreJob, cfg: Config, sem: threading.Semaphore) -> None:
        try:
            self._pipeline(job, cfg)
        except Exception as exc:  # noqa: BLE001 — job boundary
            logger.exception("Restore job %s crashed", job.id)
            job.log(f"ERROR: {exc}")
            job.set_status("failed", {"ok": False, "error": str(exc)})
        finally:
            sem.release()

    @staticmethod
    def _audit(job: "RestoreJob", event: str, **fields) -> None:
        """Structured audit event — basename-only, never raise."""
        try:
            payload = {
                "event": event,
                "job_id": job.id,
                "target_db": job.dbname,
                "source_db": job.source_db,
                "backup_file": os.path.basename(job.backup_name),
                "ip": job.ip or "",
            }
            payload.update(fields)
            _audit_logger.info(json.dumps(payload, ensure_ascii=False))
        except Exception:  # noqa: BLE001 — never break a restore for audit failures
            pass

    def _pipeline(self, job: "RestoreJob", cfg: Config) -> None:
        dbname = job.dbname
        job.log(f"Job {job.id}: restore {dbname} from backup '{job.backup_name}'")
        try:
            abs_backup = resolve_backup_path(
                cfg.BACKUP_ROOT, dbname, job.source_db, job.backup_name
            )
        except ValueError as exc:
            job.log(f"ERROR: {exc}")
            job.set_status("failed", {"ok": False, "error": str(exc)})
            self._audit(job, "failed", error=str(exc), step="resolve")
            return

        job.log(f"Resolved backup file: {abs_backup}")
        self._audit(job, "started")
        lock = self.db_lock(dbname)
        if not lock.acquire(timeout=1):
            job.log("ERROR: another restore for this database is already running")
            job.set_status(
                "failed",
                {"ok": False, "error": "restore already running for this database"},
            )
            self._audit(job, "failed", error="restore already running for this database")
            return

        try:
            conn = db.get_conn(cfg)
            conn.autocommit = True
            try:
                final_owner = resolve_owner(cfg, dbname, conn)
                job.log(f"Owner: {final_owner}")
                job.result["owner"] = final_owner
                self._audit(job, "owner", owner=final_owner)
                job.log("Terminating connections...")
                terminated = db.terminate_connections(cfg, dbname, conn)
                job.log(f"Terminated {terminated} connection(s)")
                job.result["terminated"] = terminated
                self._audit(job, "terminated", terminated=terminated)
                job.log("Dropping database...")
                db.drop_database(cfg, dbname, conn)
                job.log("Database dropped")
                job.log(f"Creating database with owner {final_owner}...")
                db.create_database(cfg, dbname, final_owner, conn)
                job.log("Database created")
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Restore pre-step failed for %s", dbname)
            job.log(f"ERROR: {exc}")
            job.set_status(
                "failed",
                {
                    "ok": False,
                    "error": str(exc),
                    "step": "terminate/drop/create",
                    "database_state": "may be missing - restore did not finish",
                },
            )
            self._audit(
                job, "failed", error=str(exc), step="terminate/drop/create", ok=False
            )
            return

        job.log("Running pg_restore...")
        try:
            ret, out, err = run_pg_restore(cfg, abs_backup, dbname)
        except Exception as exc:  # noqa: BLE001
            logger.exception("pg_restore failed for %s", dbname)
            job.log(f"ERROR: {exc}")
            job.set_status(
                "failed",
                {
                    "ok": False,
                    "error": str(exc),
                    "step": "restore",
                    "database_state": "created but empty/incomplete",
                },
            )
            self._audit(job, "failed", error=str(exc), step="restore", ok=False)
            return

        if out.strip():
            job.log("--- pg_restore stdout ---")
            for line in out.strip().splitlines():
                job.log(line)
        if err.strip():
            job.log("--- pg_restore stderr ---")
            for line in err.strip().splitlines():
                job.log(line)

        job.log(f"pg_restore exit code: {ret}")
        result = {
            "ok": ret == 0,
            "exit_code": ret,
            "owner": job.result.get("owner"),
            "terminated": job.result.get("terminated"),
            "stdout": out.strip(),
            "stderr": err.strip(),
            "backup": job.backup_name,
            "dbname": dbname,
        }
        if ret == 0:
            job.log("OK: Restore finished successfully")
            job.set_status("success", result)
            self._audit(job, "success", exit_code=ret, ok=True)
        else:
            job.log("FAIL: Restore failed (pg_restore non-zero exit)")
            result["database_state"] = "created but restore incomplete"
            job.set_status("failed", result)
            self._audit(job, "failed", exit_code=ret, ok=False)


manager = JobManager()
