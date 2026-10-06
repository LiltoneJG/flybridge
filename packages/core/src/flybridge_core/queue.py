from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from pathlib import Path
from typing import TYPE_CHECKING

from .database import SCHEMA_VERSION as DATABASE_SCHEMA_VERSION
from .database import connect_database, prepare_database
from .types import QueueEvent, QueueRequestStatus, WorkflowStatus

if TYPE_CHECKING:
    from .workflows import WorkflowStore

MAX_QUEUE_EVENTS = 10_000
SCHEMA_VERSION = DATABASE_SCHEMA_VERSION


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
        self.path = prepare_database(state_dir)

    def _connect(self) -> sqlite3.Connection:
        return connect_database(self.path)

    @staticmethod
    def _event(connection: sqlite3.Connection, resource: str, request_id: str, event: str) -> None:
        connection.execute(
            "INSERT INTO queue_events(created_at, resource, request_id, event) VALUES (?, ?, ?, ?)",
            (_now(), resource, request_id, event),
        )
        connection.execute(
            """
            DELETE FROM queue_events
            WHERE sequence <= (SELECT MAX(sequence) - ? FROM queue_events)
            """,
            (MAX_QUEUE_EVENTS,),
        )

    @staticmethod
    def _promote(connection: sqlite3.Connection, resource: str) -> str | None:
        if connection.execute(
            "SELECT 1 FROM queue_resource_blocks WHERE resource = ?", (resource,)
        ).fetchone():
            return None
        next_request = connection.execute(
            """
            SELECT id FROM queue_requests
            WHERE resource = ? AND status = ?
            ORDER BY created_at, id LIMIT 1
            """,
            (resource, QueueRequestStatus.WAITING.value),
        ).fetchone()
        if next_request is None:
            return None
        request_id = str(next_request["id"])
        connection.execute(
            "UPDATE queue_requests SET status = ?, updated_at = ? WHERE id = ?",
            (QueueRequestStatus.LEASED.value, _now(), request_id),
        )
        ResourceQueue._event(connection, resource, request_id, QueueEvent.LEASED.value)
        return request_id

    def acquire(
        self,
        resource: str,
        owner: str,
        *,
        job_argv: list[str] | None = None,
        cleanup_check: str | None = None,
        worktree_path: str | None = None,
    ) -> AcquireResult:
        if (
            not isinstance(resource, str)
            or not isinstance(owner, str)
            or not resource.strip()
            or not owner.strip()
        ):
            raise ValueError("resource and owner are required")
        resource = resource.strip()
        owner = owner.strip()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = _now()
            finishing = connection.execute(
                "SELECT 1 FROM workflow_lifecycle_operations WHERE workflow_id = ?", (owner,)
            ).fetchone()
            if finishing is not None:
                raise ValueError("queue owner has a lifecycle operation in progress")
            existing = connection.execute(
                """
                SELECT id, status, created_at FROM queue_requests
                WHERE resource = ? AND owner = ? AND status IN ('waiting', 'leased')
                """,
                (resource, owner),
            ).fetchone()
            if existing is not None:
                request_id = str(existing["id"])
                job = connection.execute(
                    "SELECT argv_json, cleanup_check, worktree_path FROM queue_jobs "
                    "WHERE request_id=?",
                    (request_id,),
                ).fetchone()
                if job_argv is not None:
                    if job is None or (
                        job["argv_json"],
                        job["cleanup_check"],
                        job["worktree_path"],
                    ) != (json.dumps(job_argv), cleanup_check, worktree_path):
                        raise ValueError("owner already has a different active resource request")
                elif job is not None:
                    raise ValueError("owner already has a queued execution for this resource")
                granted = existing["status"] == QueueRequestStatus.LEASED.value
                position = (
                    0
                    if granted
                    else connection.execute(
                        """
                        SELECT COUNT(*) FROM queue_requests
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
                "SELECT id FROM queue_requests WHERE resource = ? AND status = ?",
                (resource, QueueRequestStatus.LEASED.value),
            ).fetchone()
            blocked = connection.execute(
                "SELECT 1 FROM queue_resource_blocks WHERE resource = ?", (resource,)
            ).fetchone()
            status = QueueRequestStatus.WAITING if active or blocked else QueueRequestStatus.LEASED
            connection.execute(
                "INSERT INTO queue_requests VALUES (?, ?, ?, ?, ?, ?)",
                (request_id, resource, owner, now, now, status.value),
            )
            if status == QueueRequestStatus.WAITING:
                connection.execute(
                    "INSERT INTO queue_grant_notifications(request_id) VALUES (?)", (request_id,)
                )
            if job_argv is not None:
                if not job_argv or not all(isinstance(arg, str) and arg for arg in job_argv):
                    raise ValueError("job command must be a non-empty argument vector")
                if not cleanup_check or not worktree_path:
                    raise ValueError("cleanup check and worktree path are required")
                connection.execute(
                    "INSERT INTO queue_jobs(request_id, argv_json, cleanup_check, worktree_path, status) "
                    "VALUES (?, ?, ?, ?, 'queued')",
                    (request_id, json.dumps(job_argv), cleanup_check, worktree_path),
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
                SELECT COUNT(*) FROM queue_requests
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
                "SELECT * FROM queue_requests WHERE id = ?", (lease_id,)
            ).fetchone()
            if lease is None or lease["status"] != QueueRequestStatus.LEASED.value:
                raise ValueError("active lease was not found")
            if resource is not None and lease["resource"] != resource:
                raise ValueError("lease does not belong to the requested resource")
            if owner is not None and lease["owner"] != owner:
                raise ValueError("lease does not belong to the requested owner")
            if connection.execute(
                "SELECT 1 FROM queue_jobs WHERE request_id=?", (lease_id,)
            ).fetchone():
                raise ValueError("queued job lease is released by its cleanup check")
            connection.execute(
                "UPDATE queue_requests SET status = ?, updated_at = ? WHERE id = ?",
                (QueueRequestStatus.RELEASED.value, _now(), lease_id),
            )
            self._event(connection, lease["resource"], lease_id, QueueEvent.RELEASED.value)
            return self._promote(connection, str(lease["resource"]))

    @staticmethod
    def _block_lease(
        connection: sqlite3.Connection, request_id: str, resource: str, reason: str
    ) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO queue_resource_blocks VALUES (?, ?, ?, ?)",
            (resource, request_id, reason, _now()),
        )

    def cancel(self, request_id: str, *, cleanup_confirmed: bool = False) -> str | None:
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request identifier is required")
        request_id = request_id.strip()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM queue_requests WHERE id = ?", (request_id,)
            ).fetchone()
            if row is None or row["status"] not in {
                QueueRequestStatus.WAITING.value,
                QueueRequestStatus.LEASED.value,
            }:
                raise ValueError("active request was not found")
            job = connection.execute(
                "SELECT status FROM queue_jobs WHERE request_id=?", (request_id,)
            ).fetchone()
            if job is not None and job["status"] == "running":
                raise ValueError("running job cannot be cancelled before it stops")
            connection.execute(
                "UPDATE queue_requests SET status = ?, updated_at = ? WHERE id = ?",
                (QueueRequestStatus.CANCELLED.value, _now(), request_id),
            )
            self._event(connection, row["resource"], request_id, QueueEvent.CANCELLED.value)
            if job is not None and job["status"] == "queued":
                connection.execute(
                    "UPDATE queue_jobs SET status='failed', finished_at=?, error='request cancelled' "
                    "WHERE request_id=?",
                    (_now(), request_id),
                )
            if row["status"] != QueueRequestStatus.LEASED.value:
                return None
            if not cleanup_confirmed:
                self._block_lease(
                    connection, request_id, str(row["resource"]), "lease_cancelled_without_cleanup"
                )
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
                SELECT id, resource, status FROM queue_requests
                WHERE owner = ? AND status IN ('waiting', 'leased')
                ORDER BY created_at, id
                """,
                (owner,),
            ).fetchall()
            for row in rows:
                request_id, resource = str(row["id"]), str(row["resource"])
                connection.execute(
                    "UPDATE queue_requests SET status = ?, updated_at = ? WHERE id = ?",
                    (QueueRequestStatus.CANCELLED.value, _now(), request_id),
                )
                self._event(connection, resource, request_id, QueueEvent.CANCELLED.value)
                connection.execute(
                    "UPDATE queue_jobs SET status='failed', finished_at=?, error='owner stopped' "
                    "WHERE request_id=? AND status='queued'",
                    (_now(), request_id),
                )
                if row["status"] == QueueRequestStatus.LEASED.value:
                    self._block_lease(connection, request_id, resource, "owner_stopped")
        return promoted

    def leased_requests(self, *, owners: Iterable[str] | None = None) -> list[dict[str, object]]:
        """Return leased requests, optionally limited to the given owners."""
        owner_filter = tuple(dict.fromkeys(owners)) if owners is not None else None
        if owner_filter == ():
            return []
        query = """
            SELECT id AS request_id, resource, owner, status, created_at, updated_at
            FROM queue_requests
            WHERE status = ?
        """
        params: list[object] = [QueueRequestStatus.LEASED.value]
        if owner_filter is not None:
            query += f" AND owner IN ({','.join('?' for _ in owner_filter)})"
            params.extend(owner_filter)
        query += " ORDER BY resource, updated_at, id"
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(query, params)]

    def pending_grants(self, owner: str) -> list[dict[str, object]]:
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT q.id AS request_id, q.id AS lease_id, q.resource, q.owner,
                           q.status, n.sent_at, n.delivered_at, n.acknowledged_at,
                           n.attempts, n.last_error
                    FROM queue_requests q
                    JOIN queue_grant_notifications n ON n.request_id = q.id
                    WHERE q.owner = ? AND q.status = 'leased' AND n.acknowledged_at IS NULL
                    ORDER BY q.created_at, q.id
                    """,
                    (owner,),
                )
            ]

    def mark_grant_delivered(self, request_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE queue_grant_notifications SET delivered_at = COALESCE(delivered_at, ?), "
                "sent_at = ?, attempts = attempts + 1, last_error = NULL "
                "WHERE request_id = ? AND acknowledged_at IS NULL",
                (_now(), _now(), request_id),
            )

    def note_grant_error(self, request_id: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE queue_grant_notifications SET attempts = attempts + 1, sent_at = ?, "
                "last_error = ? WHERE request_id = ? AND acknowledged_at IS NULL",
                (_now(), error, request_id),
            )

    def acknowledge(self, request_id: str, lease_id: str, owner: str) -> None:
        if request_id != lease_id:
            raise ValueError("request and lease identifiers must match")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, owner FROM queue_requests WHERE id = ?", (request_id,)
            ).fetchone()
            if row is None or row["status"] != "leased" or row["owner"] != owner:
                raise ValueError("active lease does not belong to the owner")
            notice = connection.execute(
                "SELECT request_id FROM queue_grant_notifications WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if notice is None:
                raise ValueError("lease has no pending grant notification")
            connection.execute(
                "UPDATE queue_grant_notifications SET acknowledged_at = COALESCE(acknowledged_at, ?) "
                "WHERE request_id = ?",
                (_now(), request_id),
            )

    def delivery_candidates(self, retry_seconds: float = 30) -> list[dict[str, object]]:
        cutoff = (datetime.now(UTC) - timedelta(seconds=retry_seconds)).isoformat()
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT q.id AS request_id, q.id AS lease_id, q.resource, q.owner, "
                    "n.sent_at, n.attempts "
                    "FROM queue_requests q JOIN queue_grant_notifications n ON n.request_id=q.id "
                    "LEFT JOIN queue_jobs j ON j.request_id=q.id "
                    "WHERE q.status='leased' AND j.request_id IS NULL "
                    "AND n.acknowledged_at IS NULL AND (n.sent_at IS NULL OR n.sent_at < ?) "
                    "ORDER BY q.updated_at, q.id",
                    (cutoff,),
                )
            ]

    def blocks(self, resource: str | None = None) -> list[dict[str, object]]:
        with self._connect() as connection:
            if resource is None:
                rows = connection.execute(
                    "SELECT b.*, q.owner, q.status AS request_status "
                    "FROM queue_resource_blocks b JOIN queue_requests q ON q.id=b.request_id "
                    "ORDER BY b.resource"
                )
            else:
                rows = connection.execute(
                    "SELECT b.*, q.owner, q.status AS request_status "
                    "FROM queue_resource_blocks b JOIN queue_requests q ON q.id=b.request_id "
                    "WHERE b.resource=?",
                    (resource,),
                )
            return [dict(row) for row in rows]

    def owner_blocks(self, owners: Iterable[str]) -> list[dict[str, object]]:
        owner_filter = tuple(dict.fromkeys(owners))
        if not owner_filter:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT b.*, q.owner, q.status AS request_status "
                "FROM queue_resource_blocks b JOIN queue_requests q "
                "ON q.id=b.request_id WHERE q.owner IN ("
                + ",".join("?" for _ in owner_filter)
                + ") ORDER BY b.resource",
                owner_filter,
            )
            return [dict(row) for row in rows]

    def resolve(self, request_id: str, *, cleanup_confirmed: bool) -> str | None:
        if not cleanup_confirmed:
            raise ValueError("cleanup confirmation is required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT resource FROM queue_resource_blocks WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None:
                raise ValueError("recovery block was not found for request")
            resource = str(row["resource"])
            connection.execute("DELETE FROM queue_resource_blocks WHERE resource=?", (resource,))
            self._event(connection, resource, request_id, QueueEvent.RECOVERED.value)
            return self._promote(connection, resource)

    def queued_jobs(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT j.*, q.resource, q.owner FROM queue_jobs j "
                    "JOIN queue_requests q ON q.id=j.request_id "
                    "WHERE j.status='queued' AND q.status='leased' "
                    "ORDER BY q.updated_at, q.id"
                )
            ]

    def claim_job(self, request_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT j.*, q.resource, q.owner FROM queue_jobs j "
                "JOIN queue_requests q ON q.id=j.request_id "
                "WHERE j.request_id=? AND j.status='queued' AND q.status='leased' "
                "AND NOT EXISTS (SELECT 1 FROM queue_resource_blocks b WHERE b.resource=q.resource)",
                (request_id,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE queue_jobs SET status='running', started_at=? WHERE request_id=?",
                (_now(), request_id),
            )
            return dict(row)

    def finish_job(
        self,
        request_id: str,
        *,
        command_exit_code: int,
        check_exit_code: int | None,
        error: str | None = None,
    ) -> str | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT q.resource, q.status FROM queue_jobs j "
                "JOIN queue_requests q ON q.id=j.request_id "
                "WHERE j.request_id=? AND j.status='running'",
                (request_id,),
            ).fetchone()
            if row is None:
                raise ValueError("running queue job was not found")
            resource = str(row["resource"])
            safe = check_exit_code == 0 and error is None
            status = (
                "succeeded"
                if safe and command_exit_code == 0
                else ("failed" if safe else "recovery_required")
            )
            connection.execute(
                "UPDATE queue_jobs SET status=?, finished_at=?, command_exit_code=?, "
                "check_exit_code=?, error=? WHERE request_id=?",
                (status, _now(), command_exit_code, check_exit_code, error, request_id),
            )
            connection.execute(
                "INSERT OR IGNORE INTO queue_result_notifications(request_id) VALUES (?)",
                (request_id,),
            )
            if row["status"] != "leased":
                if safe:
                    block = connection.execute(
                        "SELECT request_id FROM queue_resource_blocks WHERE resource=?",
                        (resource,),
                    ).fetchone()
                    if block is not None and block["request_id"] == request_id:
                        connection.execute(
                            "DELETE FROM queue_resource_blocks WHERE resource=?", (resource,)
                        )
                        return self._promote(connection, resource)
                return None
            connection.execute(
                "UPDATE queue_requests SET status=?, updated_at=? WHERE id=?",
                ("released" if safe else "cancelled", _now(), request_id),
            )
            if not safe:
                self._block_lease(connection, request_id, resource, "cleanup_unverified")
                self._event(connection, resource, request_id, QueueEvent.CANCELLED.value)
                return None
            self._event(connection, resource, request_id, QueueEvent.RELEASED.value)
            return self._promote(connection, resource)

    def abandon_running_jobs(self) -> list[str]:
        """Called only after a new dispatcher holds the exclusive process lock."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT j.request_id, q.resource FROM queue_jobs j JOIN queue_requests q "
                "ON q.id=j.request_id WHERE j.status='running'"
            ).fetchall()
            for row in rows:
                request_id, resource = str(row["request_id"]), str(row["resource"])
                connection.execute(
                    "UPDATE queue_jobs SET status='recovery_required', finished_at=?, "
                    "error='dispatcher stopped during execution' WHERE request_id=?",
                    (_now(), request_id),
                )
                connection.execute(
                    "UPDATE queue_requests SET status='cancelled', updated_at=? "
                    "WHERE id=? AND status='leased'",
                    (_now(), request_id),
                )
                self._block_lease(connection, request_id, resource, "dispatcher_stopped")
                connection.execute(
                    "INSERT OR IGNORE INTO queue_result_notifications(request_id) VALUES (?)",
                    (request_id,),
                )
            return [str(row["request_id"]) for row in rows]

    def abandon_job(self, request_id: str, reason: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT q.resource FROM queue_jobs j JOIN queue_requests q "
                "ON q.id=j.request_id WHERE j.request_id=? AND j.status='running'",
                (request_id,),
            ).fetchone()
            if row is None:
                return
            resource = str(row["resource"])
            connection.execute(
                "UPDATE queue_jobs SET status='recovery_required', finished_at=?, error=? "
                "WHERE request_id=?",
                (_now(), reason, request_id),
            )
            connection.execute(
                "UPDATE queue_requests SET status='cancelled', updated_at=? "
                "WHERE id=? AND status='leased'",
                (_now(), request_id),
            )
            self._block_lease(connection, request_id, resource, reason)
            connection.execute(
                "INSERT OR IGNORE INTO queue_result_notifications(request_id) VALUES (?)",
                (request_id,),
            )

    def result_candidates(self, retry_seconds: float = 30) -> list[dict[str, object]]:
        cutoff = (datetime.now(UTC) - timedelta(seconds=retry_seconds)).isoformat()
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT j.*, q.resource, q.owner, n.sent_at, n.attempts "
                    "FROM queue_jobs j JOIN queue_requests q ON q.id=j.request_id "
                    "JOIN queue_result_notifications n ON n.request_id=j.request_id "
                    "WHERE n.acknowledged_at IS NULL AND (n.sent_at IS NULL OR n.sent_at < ?) "
                    "ORDER BY j.finished_at, j.request_id",
                    (cutoff,),
                )
            ]

    def note_result_delivery(self, request_id: str, error: str | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE queue_result_notifications SET attempts=attempts+1, "
                "sent_at=?, last_error=? "
                "WHERE request_id=? AND acknowledged_at IS NULL",
                (_now(), error, request_id),
            )

    def acknowledge_result(self, request_id: str, owner: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT q.owner FROM queue_jobs j JOIN queue_requests q ON q.id=j.request_id "
                "WHERE j.request_id=? AND j.status IN ('succeeded','failed','recovery_required')",
                (request_id,),
            ).fetchone()
            if row is None or row["owner"] != owner:
                raise ValueError("completed job does not belong to owner")
            connection.execute(
                "UPDATE queue_result_notifications SET acknowledged_at=COALESCE(acknowledged_at, ?) "
                "WHERE request_id=?",
                (_now(), request_id),
            )

    def reminder_due(self, request_id: str, interval_seconds: float) -> bool:
        cutoff = (datetime.now(UTC) - timedelta(seconds=interval_seconds)).isoformat()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT last_sent_at FROM queue_lease_reminders WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        return row is None or row["last_sent_at"] is None or row["last_sent_at"] < cutoff

    def mark_reminder_sent(self, request_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO queue_lease_reminders(request_id, last_sent_at) VALUES (?, ?) "
                "ON CONFLICT(request_id) DO UPDATE SET last_sent_at = excluded.last_sent_at",
                (request_id, _now()),
            )

    def recover_stale(self, max_age_seconds: float) -> list[str]:
        """List age-stale leases; age is not proof that cleanup completed."""
        return [str(item["request_id"]) for item in self.expire_stale_leases(max_age_seconds)]

    def expire_stale_leases(
        self, max_age_seconds: float, *, owners: Iterable[str] | None = None
    ) -> list[dict[str, object]]:
        """Inspect stale leases without changing queue ownership."""
        self._require_positive_age(max_age_seconds)
        owner_filter = tuple(dict.fromkeys(owners)) if owners is not None else None
        if owner_filter == ():
            return []
        cutoff = (datetime.now(UTC) - timedelta(seconds=max_age_seconds)).isoformat()
        owner_clause = ""
        params: list[object] = [QueueRequestStatus.LEASED.value, cutoff]
        if owner_filter is not None:
            owner_clause = f" AND owner IN ({','.join('?' for _ in owner_filter)})"
            params.extend(owner_filter)
        with self._connect() as connection:
            stale = connection.execute(
                f"""
                SELECT id, resource, owner FROM queue_requests
                WHERE status = ? AND updated_at < ?
                {owner_clause}
                ORDER BY resource, updated_at, id
                """,
                params,
            ).fetchall()
        return [
            {
                "request_id": str(row["id"]),
                "owner": str(row["owner"]),
                "resource": str(row["resource"]),
            }
            for row in stale
        ]

    def expire_waiting(
        self, max_age_seconds: float, *, owners: Iterable[str] | None = None
    ) -> list[dict[str, object]]:
        """Cancel waiting requests whose created_at is older than max_age_seconds."""
        self._require_positive_age(max_age_seconds)
        owner_filter = tuple(dict.fromkeys(owners)) if owners is not None else None
        if owner_filter == ():
            return []
        cutoff = (datetime.now(UTC) - timedelta(seconds=max_age_seconds)).isoformat()
        owner_clause = ""
        params: list[object] = [QueueRequestStatus.WAITING.value, cutoff]
        if owner_filter is not None:
            owner_clause = f" AND owner IN ({','.join('?' for _ in owner_filter)})"
            params.extend(owner_filter)
        expired: list[dict[str, object]] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            stale = connection.execute(
                f"""
                SELECT id, resource, owner FROM queue_requests
                WHERE status = ? AND created_at < ?
                {owner_clause}
                ORDER BY resource, created_at, id
                """,
                params,
            ).fetchall()
            for row in stale:
                request_id, resource, owner = (
                    str(row["id"]),
                    str(row["resource"]),
                    str(row["owner"]),
                )
                connection.execute(
                    "UPDATE queue_requests SET status = ?, updated_at = ? WHERE id = ?",
                    (QueueRequestStatus.CANCELLED.value, _now(), request_id),
                )
                self._event(connection, resource, request_id, QueueEvent.CANCELLED.value)
                expired.append({"request_id": request_id, "owner": owner, "resource": resource})
        return expired

    @staticmethod
    def _require_positive_age(max_age_seconds: float) -> None:
        if (
            isinstance(max_age_seconds, bool)
            or not isinstance(max_age_seconds, (int, float))
            or not isfinite(max_age_seconds)
            or max_age_seconds <= 0
        ):
            raise ValueError("max_age_seconds must be a finite positive number")

    def status(self, resource: str | None = None) -> list[dict[str, object]]:
        if resource is not None and (not isinstance(resource, str) or not resource.strip()):
            raise ValueError("resource must be a non-empty string")
        resource = resource.strip() if resource is not None else None
        query = "SELECT resource, status, COUNT(*) AS count FROM queue_requests"
        params: tuple[str, ...] = ()
        if resource:
            query += " WHERE resource = ?"
            params = (resource,)
        query += " GROUP BY resource, status ORDER BY resource, status"
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(query, params)]

    def active_requests(
        self,
        *,
        resource: str | None = None,
        owners: Iterable[str] | None = None,
        attention_after_seconds: float,
    ) -> list[dict[str, object]]:
        """Describe active requests without treating age as permission to release."""
        self._require_positive_age(attention_after_seconds)
        if resource is not None and (not isinstance(resource, str) or not resource.strip()):
            raise ValueError("resource must be a non-empty string")
        owner_filter = tuple(dict.fromkeys(owners)) if owners is not None else None
        if owner_filter == ():
            return []
        query = """
            SELECT q.id AS request_id, q.resource, q.owner, q.status, q.created_at,
                   q.updated_at, n.sent_at AS grant_last_attempt_at,
                   n.delivered_at AS grant_delivered_at,
                   n.acknowledged_at AS grant_acknowledged_at,
                   n.attempts AS grant_attempts, n.last_error AS grant_error,
                   j.status AS job_status, b.reason AS recovery_reason
            FROM queue_requests q
            LEFT JOIN queue_grant_notifications n ON n.request_id=q.id
            LEFT JOIN queue_jobs j ON j.request_id=q.id
            LEFT JOIN queue_resource_blocks b ON b.resource=q.resource
            WHERE q.status IN ('waiting', 'leased')
        """
        params: list[object] = []
        if resource is not None:
            query += " AND q.resource = ?"
            params.append(resource.strip())
        if owner_filter is not None:
            query += f" AND q.owner IN ({','.join('?' for _ in owner_filter)})"
            params.extend(owner_filter)
        query += " ORDER BY q.resource, CASE q.status WHEN 'leased' THEN 0 ELSE 1 END, q.created_at, q.id"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        now = datetime.now(UTC)
        result: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            since = item["updated_at"] if item["status"] == "leased" else item["created_at"]
            age_seconds = max(0.0, (now - datetime.fromisoformat(str(since))).total_seconds())
            item["lease_id"] = item["request_id"] if item["status"] == "leased" else None
            item["age_seconds"] = age_seconds
            item["attention_required"] = (
                item["status"] == "leased" and age_seconds >= attention_after_seconds
            )
            result.append(item)
        return result

    def owner_requests(self, owner: str) -> list[dict[str, object]]:
        """Return this owner's waiting and leased requests."""
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("owner is required")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id AS request_id, resource, owner, status, created_at, updated_at
                FROM queue_requests
                WHERE owner = ? AND status IN ('waiting', 'leased')
                ORDER BY created_at, id
                """,
                (owner.strip(),),
            ).fetchall()
        results: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            item["lease_id"] = (
                item["request_id"] if item["status"] == QueueRequestStatus.LEASED.value else None
            )
            results.append(item)
        return results

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
                FROM queue_requests WHERE id = ?
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
        """Return owners waiting for a lease, holding one, or awaiting a job result."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT owner FROM queue_requests
                WHERE status IN ('waiting', 'leased')
                UNION
                SELECT DISTINCT q.owner FROM queue_jobs j
                JOIN queue_requests q ON q.id=j.request_id
                JOIN queue_result_notifications n ON n.request_id=j.request_id
                WHERE n.acknowledged_at IS NULL
                ORDER BY owner
                """
            ).fetchall()
        return [str(row["owner"]) for row in rows]

    def pending_results(self, owners: Iterable[str] | None = None) -> list[dict[str, object]]:
        owner_filter = tuple(dict.fromkeys(owners)) if owners is not None else None
        if owner_filter == ():
            return []
        query = (
            "SELECT j.request_id, q.resource, q.owner, j.status, j.command_exit_code, "
            "j.check_exit_code, j.error, n.sent_at, n.acknowledged_at, n.attempts, "
            "n.last_error FROM queue_jobs j JOIN queue_requests q ON q.id=j.request_id "
            "JOIN queue_result_notifications n ON n.request_id=j.request_id "
            "WHERE n.acknowledged_at IS NULL"
        )
        params: list[str] = []
        if owner_filter is not None:
            query += " AND q.owner IN (" + ",".join("?" for _ in owner_filter) + ")"
            params.extend(owner_filter)
        query += " ORDER BY j.finished_at, j.request_id"
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(query, params)]

    def reconcile_terminal_workflows(self, workflows: WorkflowStore) -> None:
        """Cancel active requests whose durable workflow owner is terminal."""
        for owner in self.active_owners():
            try:
                workflow = workflows.get(owner)
            except ValueError:
                self.cancel_owner(owner)
                continue
            except sqlite3.Error:
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
                    "SELECT * FROM queue_events WHERE sequence > ? ORDER BY sequence LIMIT ?",
                    (after, limit),
                )
            ]
