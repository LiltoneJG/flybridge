from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def _chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        if os.name != "nt":
            raise


def symlinked_component(path: Path) -> Path | None:
    """Return the first symbolic link found in a path, starting at its leaf."""
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            return candidate
    return None


def prepare_private_database(state_dir: Path, filename: str) -> Path:
    """Create or repair private permissions for one SQLite state file."""
    linked = symlinked_component(state_dir)
    if linked is not None:
        raise OSError(f"state directory must not be a symbolic link: {linked}")
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _chmod(state_dir, 0o700)
    path = state_dir / filename
    if path.is_symlink():
        raise OSError(f"state database must not be a symbolic link: {path}")
    flags = os.O_CREAT | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    os.close(descriptor)
    _chmod(path, 0o600)
    return path


def configure_sqlite_connection(
    connection: sqlite3.Connection, path: Path, *, foreign_keys: bool = False
) -> None:
    """Apply the mandatory local-state connection policy.

    Flybridge has multiple short-lived CLI processes sharing each state file.
    WAL is therefore a correctness requirement rather than an optional
    performance tuning: silently falling back to rollback journaling makes
    contention behaviour depend on the filesystem.
    """
    try:
        row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
    except sqlite3.Error as exc:
        raise RuntimeError(f"Flybridge state requires SQLite WAL mode: {path}: {exc}") from exc
    mode = str(row[0]).lower() if row else ""
    if mode != "wal":
        raise RuntimeError(f"Flybridge state requires SQLite WAL mode: {path} returned {mode!r}")
    if foreign_keys:
        connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
