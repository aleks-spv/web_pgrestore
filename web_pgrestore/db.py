"""PostgreSQL helpers (psycopg2, no ORM)."""
from __future__ import annotations

from contextlib import closing

import psycopg2
from psycopg2 import sql

from .config import Config


def get_conn(cfg: Config, dbname: str = "postgres"):
    if cfg.PGPASSWORD:
        return psycopg2.connect(
            host=cfg.PGHOST,
            port=int(cfg.PGPORT),
            user=cfg.PGUSER,
            dbname=dbname,
            password=cfg.PGPASSWORD,
        )
    return psycopg2.connect(
        host=cfg.PGHOST,
        port=int(cfg.PGPORT),
        user=cfg.PGUSER,
        dbname=dbname,
    )


def get_databases(cfg: Config) -> list[str]:
    with closing(get_conn(cfg)) as conn:
        conn.autocommit = True
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT datname FROM pg_database "
                "WHERE datistemplate = false AND datallowconn = true "
                "ORDER BY datname;"
            )
            return [r[0] for r in cur.fetchall()]
        finally:
            cur.close()


def get_database_owner(cfg: Config, dbname: str, conn=None) -> str | None:
    should_close = conn is None
    if conn is None:
        conn = get_conn(cfg)
        conn.autocommit = True
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT pg_catalog.pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s;",
            (dbname,),
        )
        row = cur.fetchone()
        return row[0] if row else None
    except Exception:
        return None
    finally:
        cur.close()
        if should_close:
            conn.close()


def role_exists(cfg: Config, role_name: str, conn=None) -> bool:
    should_close = conn is None
    if conn is None:
        conn = get_conn(cfg)
        conn.autocommit = True
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s;",
            (role_name,),
        )
        return cur.fetchone() is not None
    except Exception:
        return False
    finally:
        cur.close()
        if should_close:
            conn.close()


def terminate_connections(cfg: Config, dbname: str, conn=None) -> int:
    should_close = conn is None
    if conn is None:
        conn = get_conn(cfg)
        conn.autocommit = True
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT COUNT(*) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid();",
            (dbname,),
        )
        row = cur.fetchone()
        count = int(row[0]) if row is not None else 0
        if count > 0:
            cur2 = conn.cursor()
            try:
                cur2.execute(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid();",
                    (dbname,),
                )
            except Exception:
                # best-effort; some backends may refuse
                pass
            finally:
                cur2.close()
        return count
    finally:
        cur.close()
        if should_close:
            conn.close()


def drop_database(cfg: Config, dbname: str, conn=None) -> None:
    should_close = conn is None
    if conn is None:
        conn = get_conn(cfg)
        conn.autocommit = True
    cur = conn.cursor()
    try:
        cur.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} ").format(sql.Identifier(dbname))
        )
    finally:
        cur.close()
        if should_close:
            conn.close()


def create_database(cfg: Config, dbname: str, owner: str, conn=None) -> None:
    should_close = conn is None
    if conn is None:
        conn = get_conn(cfg)
        conn.autocommit = True
    cur = conn.cursor()
    try:
        cur.execute(
            sql.SQL("CREATE DATABASE {} OWNER {} ").format(
                sql.Identifier(dbname),
                sql.Identifier(owner),
            )
        )
    finally:
        cur.close()
        if should_close:
            conn.close()
