"""Small cross-process locks for Xray runtime operations.

The panel starts the AI domain manager on demand while a resident manager can
be running in parallel.  A process-local ``threading.Lock`` cannot protect the
shared runtime files in that setup, so the manager uses an advisory file lock.
"""

from __future__ import annotations

import errno
import fcntl
from contextlib import contextmanager
from pathlib import Path


class LockBusyError(RuntimeError):
    """Raised when another process currently owns an operation lock."""


@contextmanager
def exclusive_file_lock(path):
    """Acquire an exclusive, non-blocking lock for ``path``.

    The lock file is deliberately kept on the shared runtime volume.  Opening
    it with append semantics creates it once and leaves a stable inode for
    every manager process to use.
    """

    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise LockBusyError(f"AI 域名管理器正在应用配置: {lock_path}") from exc
            raise
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
