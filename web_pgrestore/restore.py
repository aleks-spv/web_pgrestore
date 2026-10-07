"""Restore pipeline: path validation + terminate/drop/create/pg_restore."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from shutil import which

from . import db
from .config import Config

_DBNAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def is_valid_dbname(name: str) -> bool:
    return bool(name) and len(name) <= 63 and bool(_DBNAME_RE.match(name))


def find_bin(cfg: Config, name: str) -> str | None:
    if cfg.PGPRO_BIN_DIR:
        candidate = os.path.join(cfg.PGPRO_BIN_DIR, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return which(name)


def resolve_backup_path(
    backup_root: str,
    dbname: str,
    source_db: str,
    backup_arg: str,
) -> str:
    """Resolve backup file path with strict containment.

    Accepts:
      - basename only: "dump1.backup" → BACKUP_ROOT/<source|db>/dump1.backup
      - relative path under source root
      - absolute path (must stay inside BACKUP_ROOT/<source|db>)

    Returns realpath or raises ValueError.
    """
    if not is_valid_dbname(dbname):
        raise ValueError("Invalid database name")

    src = source_db or dbname
    if src != dbname and not is_valid_dbname(src):
        raise ValueError("Invalid source database name")

    abs_root = os.path.realpath(backup_root)
    abs_source = os.path.realpath(os.path.join(abs_root, src))
    if not (
        abs_source == abs_root
        or abs_source.startswith(abs_root + os.sep)
    ):
        raise ValueError("Invalid source directory")

    if not backup_arg or "\x00" in backup_arg:
        raise ValueError("Backup file required")

    candidate = backup_arg
    if not os.path.isabs(candidate):
        candidate = os.path.join(abs_source, candidate)
    abs_backup = os.path.realpath(candidate)

    if not (
        abs_backup == abs_source
        or abs_backup.startswith(abs_source + os.sep)
    ):
        raise ValueError("Файл бэкапа не принадлежит источнику")

    # target DB dir must exist under root (created from dumps layout)
    abs_db_dir = os.path.realpath(os.path.join(abs_root, dbname))
    if not (
        abs_db_dir == abs_root or abs_db_dir.startswith(abs_root + os.sep)
    ):
        raise ValueError("Invalid backup directory path")

    if not os.path.isfile(abs_backup):
        raise ValueError("Backup file does not exist")
    return abs_backup


def list_backups(cfg: Config, db_name: str) -> list[str]:
    """Return basenames of *.backup* under BACKUP_ROOT/<db_name>."""
    if not is_valid_dbname(db_name):
        return []
    base = Path(cfg.BACKUP_ROOT).resolve()
    db_dir = (base / db_name).resolve()
    if not db_dir.is_dir():
        return []
    try:
        db_dir.relative_to(base)
    except ValueError:
        return []
    files = list(db_dir.glob("*.backup*"))
    files = [f for f in files if f.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [f.name for f in files]


def run_pg_restore(cfg: Config, backup_path: str, dbname: str) -> tuple[int, str, str]:
    pg_restore = find_bin(cfg, "pg_restore")
    if not pg_restore:
        raise RuntimeError("pg_restore binary not found in PATH")

    cmd = [
        pg_restore,
        "-h", cfg.PGHOST,
        "-p", str(cfg.PGPORT),
        "-U", cfg.PGUSER,
        "-d", dbname,
        "--no-owner",
        "--no-password",
    ]
    if cfg.PGPRO_PARALLEL > 1 and not backup_path.endswith(".gz"):
        cmd += ["-j", str(cfg.PGPRO_PARALLEL)]

    env = None
    if cfg.PGPASSWORD:
        env = dict(os.environ, PGPASSWORD=cfg.PGPASSWORD)

    timeout = cfg.SUBPROCESS_TIMEOUT if cfg.SUBPROCESS_TIMEOUT > 0 else None

    if backup_path.endswith(".gz"):
        gzip_bin = find_bin(cfg, "gzip")
        if not gzip_bin:
            raise RuntimeError("gzip binary not found in PATH")
        gzip_proc = subprocess.Popen(
            [gzip_bin, "-dc", backup_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            pg_proc = subprocess.run(
                cmd,
                stdin=gzip_proc.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                env=env,
            )
        except Exception:
            try:
                gzip_proc.kill()
            except Exception:
                pass
            raise
        finally:
            if gzip_proc.stdout is not None:
                gzip_proc.stdout.close()
        gzip_stderr = b""
        try:
            gzip_stderr = gzip_proc.stderr.read() if gzip_proc.stderr else b""
        finally:
            try:
                gzip_proc.wait(timeout=5)
            except Exception:
                try:
                    gzip_proc.kill()
                except Exception:
                    pass
        err = pg_proc.stderr.decode(errors="replace")
        if gzip_stderr:
            err = (err + "\n" + gzip_stderr.decode(errors="replace")).strip()
        return pg_proc.returncode, pg_proc.stdout.decode(errors="replace"), err

    cmd.append(backup_path)
    result = subprocess.run(
        cmd,
        capture_output=True,
        timeout=timeout,
        env=env,
    )
    return (
        result.returncode,
        result.stdout.decode(errors="replace"),
        result.stderr.decode(errors="replace"),
    )


def resolve_owner(cfg: Config, dbname: str, conn) -> str:
    current_owner = db.get_database_owner(cfg, dbname, conn)
    if current_owner and db.role_exists(cfg, current_owner, conn):
        return current_owner
    return cfg.DEFAULT_OWNER
