from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from pathlib import Path
from typing import TYPE_CHECKING

from .storage import ensure_schema, prepare_private_database
from .types import QueueEvent, QueueRequestStatus, WorkflowStatus

if TYPE_CHECKING:
    from .workflows import WorkflowStore

MAX_QUEUE_EVENTS = 10_000
SCHEMA_VERSION = 1
_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE requests (
        id TEXT PRIMARY KEY, resource TEXT NOT NULL, owner TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('waiting','leased','released','cancelled'))
    )
    """,
    """
    CREATE TABLE events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
        resource TEXT NOT NULL, request_id TEXT NOT NULL, event TEXT NOT NULL
    )
    """,
    "CREATE INDEX request_order ON requests(resource, status, created_at, id)",
    "CREATE UNIQUE INDEX one_resource_lease ON requests(resource) WHERE status = 'leased'",
    """
    CREATE UNIQUE INDEX one_active_owner_request
        ON requests(resource, owner) WHERE status IN ('waiting', 'leased')
    """,
)
_SCHEMA_TABLES = {
    "requests": ("id", "resource", "owner", "created_at", "updated_at", "status"),
    "events": ("sequence", "created_at", "resource", "request_id", "event"),
}
_SCHEMA_INDEXES = ("request_order", "one_resource_lease", "one_active_owner_request")


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class AcquireResult:
    request_id: str
    lease_id: str | None
    position: int
    granted: bool


class ResourceQueue:
    """A durable FIFO lease queue. No policy decision is delegated to an agent."""

    def __init__(self, state_dir: Path) -> None:
        self.path = prepare_private_database(state_dir, "queue.sqlite3")
        with closing(self._connect()) as connection, connection:
            ensure_schema(
                connection,
                self.path,
                label="queue",
                version=SCHEMA_VERSION,
                statements=_SCHEMA_STATEMENTS,
                tables=_SCHEMA_TABLES,
                indexes=_SCHEMA_INDEXES,
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @staticmethod
    def _event(connection: sqlite3.Connection, resource: str, request_id: str, event: str) -> None:
        connection.execute(
            "INSERT INTO events(created_at, resource, request_id, event) VALUES (?, ?, ?, ?)",
            (_now(), resource, request_id, event),
        )
        connection.execute(
            """
            DELETE FROM events
            WHERE sequence <= (SELECT MAX(sequence) - ? FROM events)
            """,
            (MAX_QUEUE_EVENTS,),
        )

    @staticmethod
    def _promote(connection: sqlite3.Connection, resource: str) -> str | None:
        next_request = connection.execute(
            """
            SELECT id FROM requests
            WHERE resource = ? AND status = ?
            ORDER BY created_at, id LIMIT 1
            """,
            (resource, QueueRequestStatus.WAITING.value),
        ).fetchone()
        if next_request is None:
            return None
        request_id = str(next_request["id"])
        connection.execute(
            "UPDATE requests SET status = ?, updated_at = ? WHERE id = ?",
            (QueueRequestStatus.LEASED.value, _now(), request_id),
        )
        ResourceQueue._event(connection, resource, request_id, QueueEvent.LEASED.value)
        return request_id

    def acquire(self, resource: str, owner: str) -> AcquireResult:
        if (
            not isinstance(resource, str)
            or not isinstance(owner, str)
            or not resource.strip()
            or not owner.strip()
        ):
            raise ValueError("resource and owner are required")
        resource = resource.strip()
        owner = owner.strip()
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT id, status, created_at FROM requests
                WHERE resource = ? AND owner = ? AND status IN ('waiting', 'leased')
                """,
                (resource, owner),
            ).fetchone()
            if existing is not None:
                request_id = str(existing["id"])
                granted = existing["status"] == QueueRequestStatus.LEASED.value
                position = (
                    0
                    if granted
                    else connection.execute(
                        """
                        SELECT COUNT(*) FROM requests
                        WHERE resource = ? AND status = 'waiting'
                          AND (created_at < ? OR (created_at = ? AND id <= ?))
                        """,
                        (
                            resource,
                            existing["created_at"],
                            existing["created_at"],
                            request_id,
                        ),
                    ).fetchone()[0]
                )
                return AcquireResult(
                    request_id,
                    request_id if granted else None,
                    position,
                    granted,
                )
            request_id = str(uuid.uuid4())
            active = connection.execute(
                "SELECT id FROM requests WHERE resource = ? AND status = ?",
                (resource, QueueRequestStatus.LEASED.value),
            ).fetchone()
            status = QueueRequestStatus.WAITING if active else QueueRequestStatus.LEASED
            connection.execute(
                "INSERT INTO requests VALUES (?, ?, ?, ?, ?, ?)",
                (request_id, resource, owner, now, now, status.value),
            )
            self._event(
                connection,
                resource,
                request_id,
                QueueEvent.LEASED.value
                if status == QueueRequestStatus.LEASED
                else QueueEvent.QUEUED.value,
            )
            position = connection.execute(
                """
                SELECT COUNT(*) FROM requests
                WHERE resource = ? AND status = 'waiting'
                  AND (created_at < ? OR (created_at = ? AND id <= ?))
                """,
                (resource, now, now, request_id),
            ).fetchone()[0]
        return AcquireResult(
            request_id,
            request_id if status == QueueRequestStatus.LEASED else None,
            position,
            status == QueueRequestStatus.LEASED,
        )

    def release(
        self,
        lease_id: str,
        *,
        resource: str | None = None,
        owner: str | None = None,
    ) -> str | None:
        if not isinstance(lease_id, str) or not lease_id.strip():
            raise ValueError("lease identifier is required")
        if resource is not None and (not isinstance(resource, str) or not resource.strip()):
            raise ValueError("resource must be a non-empty string")
        if owner is not None and (not isinstance(owner, str) or not owner.strip()):
            raise ValueError("owner must be a non-empty string")
        lease_id = lease_id.strip()
        resource = resource.strip() if resource is not None else None
        owner = owner.strip() if owner is not None else None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute(
                "SELECT * FROM requests WHERE id = ?", (lease_id,)
            ).fetchone()
            if lease is None or lease["status"] != QueueRequestStatus.LEASED.value:
                raise ValueError("active lease was not found")
            if resource is not None and lease["resource"] != resource:
                raise ValueError("lease does not belong to the requested resource")
            if owner is not None and lease["owner"] != owner:
                raise ValueError("lease does not belong to the requested owner")
            connection.execute(
                "UPDATE requests SET status = ?, updated_at = ? WHERE id = ?",
                (QueueRequestStatus.RELEASED.value, _now(), lease_id),
            )
            self._event(connection, lease["resource"], lease_id, QueueEvent.RELEASED.value)
            return self._promote(connection, str(lease["resource"]))

    def cancel(self, request_id: str) -> str | None:
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request identifier is required")
        request_id = request_id.strip()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM requests WHERE id = ?", (request_id,)
            ).fetchone()
            if row is None or row["status"] not in {
                QueueRequestStatus.WAITING.value,
                QueueRequestStatus.LEASED.value,
            }:
                raise ValueError("active request was not found")
            connection.execute(
                "UPDATE requests SET status = ?, updated_at = ? WHERE id = ?",
                (QueueRequestStatus.CANCELLED.value, _now(), request_id),
            )
            self._event(connection, row["resource"], request_id, QueueEvent.CANCELLED.value)
            if row["status"] != QueueRequestStatus.LEASED.value:
                return None
            return self._promote(connection, str(row["resource"]))

    def cancel_owner(self, owner: str) -> list[str]:
        """Cancel every active request for one workflow owner."""
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("owner is required")
        owner = owner.strip()
        promoted: list[str] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT id, resource, status FROM requests
                WHERE owner = ? AND status IN ('waiting', 'leased')
                ORDER BY created_at, id
                """,
                (owner,),
            ).fetchall()
            leased_resources: set[str] = set()
            for row in rows:
                request_id, resource = str(row["id"]), str(row["resource"])
                connection.execute(
                    "UPDATE requests SET status = ?, updated_at = ? WHERE id = ?",
                    (QueueRequestStatus.CANCELLED.value, _now(), request_id),
                )
                self._event(connection, resource, request_id, QueueEvent.CANCELLED.value)
                if row["status"] == QueueRequestStatus.LEASED.value:
                    leased_resources.add(resource)
            for resource in sorted(leased_resources):
                next_id = self._promote(connection, resource)
                if next_id:
                    promoted.append(next_id)
        return promoted

    def recover_stale(self, max_age_seconds: float) -> list[str]:
        """Cancel stale leases only when an operator explicitly requests recovery."""
        if (
            isinstance(max_age_seconds, bool)
            or not isinstance(max_age_seconds, (int, float))
            or not isfinite(max_age_seconds)
            or max_age_seconds <= 0
        ):
            raise ValueError("max_age_seconds must be a finite positive number")
        cutoff = (datetime.now(UTC) - timedelta(seconds=max_age_seconds)).isoformat()
        promoted: list[str] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            stale = connection.execute(
                """
                SELECT id, resource FROM requests
                WHERE status = ? AND updated_at < ?
                ORDER BY resource, updated_at, id
                """,
                (QueueRequestStatus.LEASED.value, cutoff),
            ).fetchall()
            for row in stale:
                request_id, resource = str(row["id"]), str(row["resource"])
                connection.execute(
                    "UPDATE requests SET status = ?, updated_at = ? WHERE id = ?",
                    (QueueRequestStatus.CANCELLED.value, _now(), request_id),
                )
                self._event(connection, resource, request_id, QueueEvent.RECOVERED.value)
                next_id = self._promote(connection, resource)
                if next_id:
                    promoted.append(next_id)
        return promoted

    def status(self, resource: str | None = None) -> list[dict[str, object]]:
        if resource is not None and (not isinstance(resource, str) or not resource.strip()):
            raise ValueError("resource must be a non-empty string")
        resource = resource.strip() if resource is not None else None
        query = "SELECT resource, status, COUNT(*) AS count FROM requests"
        params: tuple[str, ...] = ()
        if resource:
            query += " WHERE resource = ?"
            params = (resource,)
        query += " GROUP BY resource, status ORDER BY resource, status"
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(query, params)]

    def inspect(self, request_id: str, *, owner: str | None = None) -> dict[str, object]:
        """Return one request's durable state so a waiter can detect promotion."""
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request identifier is required")
        if owner is not None and (not isinstance(owner, str) or not owner.strip()):
            raise ValueError("owner must be a non-empty string")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id AS request_id, resource, owner, status, created_at, updated_at
                FROM requests WHERE id = ?
                """,
                (request_id.strip(),),
            ).fetchone()
        if row is None:
            raise ValueError("queue request was not found")
        if owner is not None and row["owner"] != owner.strip():
            raise ValueError("queue request does not belong to the requested owner")
        result = dict(row)
        result["lease_id"] = (
            result["request_id"] if result["status"] == QueueRequestStatus.LEASED.value else None
        )
        return result

    def active_owners(self) -> list[str]:
        """Return only owners that still have waiting or leased requests."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT owner FROM requests
                WHERE status IN ('waiting', 'leased')
                ORDER BY owner
                """
            ).fetchall()
        return [str(row["owner"]) for row in rows]

    def reconcile_terminal_workflows(self, workflows: WorkflowStore) -> None:
        """Cancel active requests whose durable workflow owner is terminal."""
        for owner in self.active_owners():
            try:
                workflow = workflows.get(owner)
            except ValueError:
                self.cancel_owner(owner)
                continue
            if workflow.status in {
                WorkflowStatus.COMPLETED,
                WorkflowStatus.FAILED,
                WorkflowStatus.CANCELLED,
            }:
                self.cancel_owner(owner)

    def events(self, after: int = 0, *, limit: int = 1000) -> list[dict[str, object]]:
        if after < 0 or isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("event cursor must not be negative and limit must be positive")
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM events WHERE sequence > ? ORDER BY sequence LIMIT ?",
                    (after, limit),
                )
            ]
