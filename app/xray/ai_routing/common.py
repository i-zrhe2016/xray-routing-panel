"""Shared low-level helpers for the AI routing package."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from app.xray.envfile import load_env_file as shared_load_env_file
from app.xray.envfile import read_env_or_file as shared_read_env_or_file
from app.xray.file_io import write_text_atomic

TIMESTAMP_RE = re.compile(r"^(?P<date>\d{4}/\d{2}/\d{2}) (?P<time>\d{2}:\d{2}:\d{2}(?:\.\d+)?) ")
TARGET_RE = re.compile(r" accepted (?P<proto>[a-z]+):(?P<target>\S+)")
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$")
PLACEHOLDER_RE = re.compile(r"__([A-Z0-9_]+)__")

SQLITE_BUSY_TIMEOUT_MS = 30000
SQLITE_LOCK_RETRY_ATTEMPTS = 4
SQLITE_LOCK_RETRY_DELAY_SECONDS = 0.25


def env_int(name, default):
    raw = str(os.environ.get(name, default)).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def env_bool(name, default):
    raw = str(os.environ.get(name, default)).strip().lower()
    return raw not in {"0", "false", "no", "off", ""}


def load_env_file_values(path):
    if not path or not Path(path).is_file():
        return {}
    return shared_load_env_file(Path(path))


def read_env_or_file(name, default="", env_file_values=None):
    return shared_read_env_or_file(name, default=default, env_file_values=env_file_values)


def parse_positive_float(raw, field_name):
    try:
        value = float(str(raw).strip())
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a positive number") from exc
    if value <= 0:
        raise ValueError(f"{field_name} must be a positive number")
    return value


def utc_now():
    return datetime.now(timezone.utc)


def format_timestamp(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def load_json(path, default):
    path = Path(path)
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps(payload, indent=2, ensure_ascii=True) + "\n")


def connect_panel_db(panel_db_path):
    conn = sqlite3.connect(panel_db_path, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    return conn


def run_with_sqlite_lock_retry(operation):
    last_error = None
    for attempt in range(SQLITE_LOCK_RETRY_ATTEMPTS):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "database is locked" not in message and "database is busy" not in message:
                raise
            last_error = exc
            if attempt + 1 < SQLITE_LOCK_RETRY_ATTEMPTS:
                time.sleep(SQLITE_LOCK_RETRY_DELAY_SECONDS * (attempt + 1))
    raise last_error
