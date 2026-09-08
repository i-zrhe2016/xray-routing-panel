"""Small SQLite connection and transaction boundary abstraction."""

import sqlite3
import threading
from contextlib import contextmanager
from importlib import import_module


class SQLiteDatabase:
    """Create configured SQLite connections for the panel database.

    The path is resolved when the object is constructed instead of being
    captured in a function default.  Importing the current config module here
    also keeps the application's reload-based test contract intact when the
    storage package itself remains loaded between cases.
    """

    def __init__(self, path=None, write_lock=None):
        configured_path = import_module("app.config").DB_PATH
        self.path = configured_path if path is None else path
        self.write_lock = write_lock or threading.Lock()

    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def get_state(self, conn, key, default=None):
        row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        return row["value"]

    def set_state(self, conn, key, value):
        conn.execute(
            """
            INSERT INTO app_state (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, str(value)),
        )

    def apply_state_update(self, operation):
        """Run a small SQLite-only mutation under the shared write lock."""
        with self.transaction() as conn:
            return operation(conn)

    @contextmanager
    def transaction(self):
        """Yield a write transaction and roll it back when it fails."""
        with self.write_lock:
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
