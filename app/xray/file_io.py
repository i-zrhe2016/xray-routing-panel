"""Small atomic file-writing helpers for shared Xray runtime artifacts."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def write_text_atomic(path: Path, text: str) -> None:
    """Replace ``path`` atomically after fully writing the new text.

    The temporary file is created in the destination directory so ``os.replace``
    remains atomic on the shared runtime volume. Existing file permissions are
    preserved when replacing an already-created artifact.
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = None
    try:
        existing_mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        pass

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, existing_mode if existing_mode is not None else 0o644)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Replace ``path`` atomically after fully writing bytes."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = None
    try:
        existing_mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        pass

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, existing_mode if existing_mode is not None else 0o644)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
