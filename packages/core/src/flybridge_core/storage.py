from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
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


def _has_user_objects(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone()
    return row is not None


def _verify_schema(
    connection: sqlite3.Connection,
    path: Path,
    label: str,
    statements: Sequence[str],
    tables: Mapping[str, Sequence[str]],
    indexes: Sequence[str],
) -> None:
    def malformed(detail: str) -> RuntimeError:
        return RuntimeError(f"malformed Flybridge {label} schema: {detail}; remove {path}")

    present_tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    for table, columns in tables.items():
        if table not in present_tables:
            raise malformed(f"table {table} is missing")
        present_columns = {
            str(row[0])
            for row in connection.execute("SELECT name FROM pragma_table_info(?)", (table,))
        }
        missing_columns = sorted(set(columns) - present_columns)
        if missing_columns:
            raise malformed(f"table {table} is missing columns {', '.join(missing_columns)}")
    present_indexes = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    missing_indexes = sorted(set(indexes) - present_indexes)
    if missing_indexes:
        raise malformed(f"missing indexes {', '.join(missing_indexes)}")
    with closing(sqlite3.connect(":memory:")) as expected_connection:
        for statement in statements:
            expected_connection.execute(statement)
        expected_definitions = {
            str(row[0]): " ".join(str(row[1]).lower().split())
            for row in expected_connection.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type IN ('table', 'index') AND sql IS NOT NULL"
            )
        }
    actual_definitions = {
        str(row[0]): " ".join(str(row[1]).lower().split())
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type IN ('table', 'index') AND sql IS NOT NULL"
        )
    }
    required_names = (*tables, *indexes)
    mismatched = [
        name
        for name in required_names
        if actual_definitions.get(name) != expected_definitions.get(name)
    ]
    if mismatched:
        raise malformed(f"definitions differ for {', '.join(mismatched)}")


def ensure_schema(
    connection: sqlite3.Connection,
    path: Path,
    *,
    label: str,
    version: int,
    statements: Sequence[str],
    tables: Mapping[str, Sequence[str]],
    indexes: Sequence[str],
) -> None:
    """Install the schema on an empty database and refuse every other foreign state."""
    connection.execute("BEGIN IMMEDIATE")
    current = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if current == 0 and not _has_user_objects(connection):
        for statement in statements:
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {version:d}")
        current = version
    if current != version:
        raise RuntimeError(
            f"unsupported Flybridge {label} schema "
            f"(found version {current}, expected {version}); remove {path}"
        )
    _verify_schema(connection, path, label, statements, tables, indexes)
