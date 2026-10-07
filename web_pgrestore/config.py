"""Application configuration loaded from environment / .env."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field, fields
from pathlib import Path

from dotenv import load_dotenv

# Project root (web_pgrestore/)
ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

SECRET_KEYS = {"AUTH_PASS", "PGPASSWORD", "FLASK_SECRET_KEY"}
INT_KEYS = {"PGPORT", "APP_PORT", "SUBPROCESS_TIMEOUT", "PGPRO_PARALLEL", "MAX_CONCURRENT_RESTORES"}
BOOL_KEYS = {"TRUST_PROXY", "ALLOW_UNAUTHENTICATED", "SESSION_COOKIE_SECURE"}

# Allowlisted keys editable from the Settings UI
EDITABLE_KEYS: tuple[str, ...] = (
    "PGHOST",
    "PGPORT",
    "PGUSER",
    "PGPASSWORD",
    "PGPRO_BIN_DIR",
    "BACKUP_ROOT",
    "DEFAULT_OWNER",
    "AUTH_USER",
    "AUTH_PASS",
    "FLASK_SECRET_KEY",
    "APP_HOST",
    "APP_PORT",
    "SUBPROCESS_TIMEOUT",
    "PGPRO_PARALLEL",
    "MAX_CONCURRENT_RESTORES",
    "TRUST_PROXY",
    "ALLOW_UNAUTHENTICATED",
    "SESSION_COOKIE_SECURE",
)

_KEY_RE = __import__("re").compile(r"^[A-Z][A-Z0-9_]*$")


def _env_str(key: str, default: str = "") -> str:
    val = os.environ.get(key)
    if val is None:
        return default
    return val


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    raw = (os.environ.get(key) or "").strip().lower()
    if raw == "":
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass
class Config:
    """Runtime configuration snapshot (re-read after Settings save)."""

    PGHOST: str = "/tmp"
    PGPORT: int = 5432
    PGUSER: str = "postgres"
    PGPASSWORD: str = ""
    PGPRO_BIN_DIR: str = ""
    BACKUP_ROOT: str = "/mnt/backups/dump"
    DEFAULT_OWNER: str = "usr1cv8"
    AUTH_USER: str = ""
    AUTH_PASS: str = ""
    FLASK_SECRET_KEY: str = ""
    APP_HOST: str = "127.0.0.1"
    APP_PORT: int = 5000
    SUBPROCESS_TIMEOUT: int = 300
    PGPRO_PARALLEL: int = 0
    MAX_CONCURRENT_RESTORES: int = 2
    TRUST_PROXY: bool = False
    ALLOW_UNAUTHENTICATED: bool = False
    SESSION_COOKIE_SECURE: bool = False
    RATE_LIMIT_ENABLED: bool = True
    # computed
    ENV_PATH: Path = field(default_factory=lambda: ENV_PATH)

    @property
    def auth_enabled(self) -> bool:
        return bool(self.AUTH_USER and self.AUTH_PASS)

    @property
    def loopback_host(self) -> bool:
        return self.APP_HOST in {"127.0.0.1", "localhost", "::1"} or self.APP_HOST.startswith("127.")

    def as_public_dict(self) -> dict:
        """Values safe to show in the Settings form (secrets masked)."""
        out = {}
        for f in fields(self):
            if f.name == "ENV_PATH":
                continue
            val = getattr(self, f.name)
            if f.name in SECRET_KEYS:
                out[f.name] = bool(val)
            else:
                out[f.name] = val
        return out

    def coerce(self, key: str, raw: str):
        if key in INT_KEYS:
            try:
                return int(raw)
            except ValueError:
                raise ValueError(f"{key} must be an integer") from None
        if key in BOOL_KEYS:
            v = (raw or "").strip().lower()
            if v in {"1", "true", "yes", "on"}:
                return True
            if v in {"0", "false", "no", "off", ""}:
                return False
            raise ValueError(f"{key} must be a boolean (0/1)")
        return raw

    def apply(self, key: str, value) -> None:
        if key not in EDITABLE_KEYS:
            raise ValueError(f"Key not allowed: {key}")
        setattr(self, key, value)

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv(ENV_PATH)
        secret = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
        return cls(
            PGHOST=_env_str("PGHOST", "/tmp"),
            PGPORT=_env_int("PGPORT", 5432),
            PGUSER=_env_str("PGUSER", "postgres"),
            PGPASSWORD=_env_str("PGPASSWORD", ""),
            PGPRO_BIN_DIR=_env_str("PGPRO_BIN_DIR", ""),
            BACKUP_ROOT=_env_str("BACKUP_ROOT", "/mnt/backups/dump"),
            DEFAULT_OWNER=_env_str("DEFAULT_OWNER", "usr1cv8"),
            AUTH_USER=_env_str("AUTH_USER", ""),
            AUTH_PASS=_env_str("AUTH_PASS", ""),
            FLASK_SECRET_KEY=secret,
            APP_HOST=_env_str("APP_HOST", "127.0.0.1"),
            APP_PORT=_env_int("APP_PORT", 5000),
            SUBPROCESS_TIMEOUT=_env_int("SUBPROCESS_TIMEOUT", 300) or 300,
            PGPRO_PARALLEL=_env_int("PGPRO_PARALLEL", 0),
            MAX_CONCURRENT_RESTORES=_env_int("MAX_CONCURRENT_RESTORES", 2) or 2,
            TRUST_PROXY=_env_bool("TRUST_PROXY", False),
            ALLOW_UNAUTHENTICATED=_env_bool("ALLOW_UNAUTHENTICATED", False),
            SESSION_COOKIE_SECURE=_env_bool("SESSION_COOKIE_SECURE", False),
            RATE_LIMIT_ENABLED=_env_bool("RATE_LIMIT_ENABLED", True),
            ENV_PATH=ENV_PATH,
        )


def update_env_file(path: Path, updates: dict) -> None:
    """Upsert KEY=VALUE lines in a dotenv file, preserving comments/order."""
    for key in updates:
        if not _KEY_RE.match(key):
            raise ValueError(f"Invalid env key: {key}")
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        out.append(line)
    for key, val in updates.items():
        if key not in seen:
            out.append(f"{key}={val}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
