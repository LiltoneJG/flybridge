"""Durable completion state for attach-existing workflow batches."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .database import connect_database, prepare_database


def _now() -> str:
    return datetime.now(UTC).isoformat()


def single_report_current(connection, workflow_id: str, terminal_handle: str | None) -> bool:
    row = connection.execute(
        "SELECT 1 FROM single_reports s JOIN workflows w ON w.id = s.workflow_id "
        "LEFT JOIN single_report_phases p ON p.workflow_id = s.workflow_id "
        "WHERE s.workflow_id = ? AND s.terminal_handle = ? "
        "AND s.terminal_handle = w.terminal_handle AND w.status = 'running' "
        "AND COALESCE(p.final, 1) = 1 "
        "AND (w.activated_at IS NULL OR s.reported_at >= w.activated_at) "
        "AND NOT EXISTS (SELECT 1 FROM queue_requests q WHERE q.owner = w.id "
        "AND q.created_at > s.reported_at) "
        "AND NOT EXISTS (SELECT 1 FROM queue_requests q WHERE q.owner = w.id "
        "AND q.status IN ('waiting', 'leased')) "
        "AND NOT EXISTS (SELECT 1 FROM queue_result_notifications n "
        "JOIN queue_requests q ON q.id = n.request_id "
        "WHERE q.owner = w.id AND n.acknowledged_at IS NULL)",
        (workflow_id, terminal_handle),
    ).fetchone()
    return row is not None


class BatchStore:
    def __init__(self, state_dir: Path) -> None:
        self.path = prepare_database(state_dir)

    def _connect(self):
        return connect_database(self.path)

    def create(self, parent_terminal: str, parent_worktree: str) -> str:
        batch_id = str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO batch_runs(id, parent_terminal, parent_worktree, status, created_at) "
                "VALUES (?, ?, ?, 'starting', ?)",
                (batch_id, parent_terminal, parent_worktree, _now()),
            )
        return batch_id

    def remove_empty(self, batch_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM batch_runs WHERE id = ? AND status = 'starting' "
                "AND NOT EXISTS (SELECT 1 FROM batch_items WHERE batch_id = ?)",
                (batch_id, batch_id),
            )

    def add_item(
        self,
        batch_id: str,
        index: int,
        path: str | None,
        workflow_id: str | None,
        error: str | None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO batch_items(batch_id, item_index, path, workflow_id, error) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(batch_id, item_index) DO UPDATE SET "
                "path = excluded.path, workflow_id = excluded.workflow_id, error = excluded.error",
                (batch_id, index, path, workflow_id, error),
            )

    def seal(self, batch_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE batch_runs SET status = 'watching' WHERE id = ? AND status = 'starting'",
                (batch_id,),
            )

    def set_watcher(self, batch_id: str, handle: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE batch_runs SET watcher_handle = ? WHERE id = ? AND status != 'notified'",
                (handle, batch_id),
            )

    def active_batches_for_workflow(self, workflow_id: str) -> list[str]:
        with self._connect() as connection:
            return [
                str(row["id"])
                for row in connection.execute(
                    "SELECT DISTINCT b.id FROM batch_runs b JOIN batch_items i ON i.batch_id = b.id "
                    "WHERE i.workflow_id = ? AND b.status != 'notified'",
                    (workflow_id,),
                )
            ]

    def active_member(self, workflow_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT b.id FROM batch_runs b JOIN batch_items i ON i.batch_id = b.id
                WHERE i.workflow_id = ? AND b.status != 'notified'
                ORDER BY b.created_at LIMIT 1
                """,
                (workflow_id,),
            ).fetchone()
        return str(row["id"]) if row is not None else None

    def is_member(self, workflow_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM batch_items WHERE workflow_id = ? LIMIT 1", (workflow_id,)
            ).fetchone()
        return row is not None

    def active_path(self, path: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT b.id FROM batch_runs b JOIN batch_items i ON i.batch_id = b.id "
                "WHERE i.path = ? AND b.status != 'notified' ORDER BY b.created_at LIMIT 1",
                (path,),
            ).fetchone()
        return str(row["id"]) if row is not None else None

    def report_single(
        self, workflow_id: str, outcome: str, summary: str, *, final: bool = True
    ) -> None:
        if outcome not in {"done", "blocked"} or not summary.strip():
            raise ValueError("single report requires a done or blocked outcome and summary")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            workflow = connection.execute(
                "SELECT mode, status, terminal_handle FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
            if (
                workflow is None
                or workflow["mode"] != "single"
                or workflow["status"] != "running"
                or not workflow["terminal_handle"]
            ):
                raise ValueError("single report requires a running single workflow")
            finishing = connection.execute(
                "SELECT 1 FROM workflow_lifecycle_operations WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
            if finishing is not None:
                raise ValueError("workflow lifecycle operation is in progress")
            active = connection.execute(
                "SELECT 1 FROM queue_requests WHERE owner = ? AND status IN ('waiting', 'leased')",
                (workflow_id,),
            ).fetchone()
            if final and active is not None:
                raise ValueError("release or cancel active queue requests before reporting")
            pending_result = connection.execute(
                "SELECT 1 FROM queue_jobs j JOIN queue_requests q ON q.id=j.request_id "
                "JOIN queue_result_notifications n ON n.request_id=j.request_id "
                "WHERE q.owner=? AND n.acknowledged_at IS NULL",
                (workflow_id,),
            ).fetchone()
            if final and pending_result is not None:
                raise ValueError("acknowledge queue job results before reporting")
            previous = connection.execute(
                "SELECT s.*, COALESCE(p.final, 1) AS final FROM single_reports s "
                "LEFT JOIN single_report_phases p USING(workflow_id) WHERE s.workflow_id = ?",
                (workflow_id,),
            ).fetchone()
            if (
                previous is not None
                and previous["terminal_handle"] == workflow["terminal_handle"]
                and previous["outcome"] == outcome
                and previous["summary"] == summary.strip()
                and bool(previous["final"]) == final
            ):
                activation = connection.execute(
                    "SELECT activated_at FROM workflows WHERE id = ?", (workflow_id,)
                ).fetchone()[0]
                fresh_activation = activation is None or previous["reported_at"] >= activation
                if fresh_activation and (
                    not final
                    or single_report_current(connection, workflow_id, workflow["terminal_handle"])
                ):
                    return
            connection.execute(
                "INSERT INTO single_report_phases VALUES (?, ?) "
                "ON CONFLICT(workflow_id) DO UPDATE SET final = excluded.final",
                (workflow_id, int(final)),
            )
            connection.execute(
                "INSERT INTO single_reports VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(workflow_id) DO UPDATE SET terminal_handle = excluded.terminal_handle, "
                "outcome = excluded.outcome, summary = excluded.summary, "
                "reported_at = excluded.reported_at",
                (workflow_id, workflow["terminal_handle"], outcome, summary.strip(), _now()),
            )

    def single_reported(self, workflow_id: str, terminal_handle: str | None) -> bool:
        if not terminal_handle:
            return False
        with self._connect() as connection:
            return single_report_current(connection, workflow_id, terminal_handle)

    def single_report(self, workflow_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT s.*, COALESCE(p.final, 1) AS final FROM single_reports s "
                "LEFT JOIN single_report_phases p USING(workflow_id) WHERE s.workflow_id = ?",
                (workflow_id,),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["final"] = bool(result["final"])
            result["current"] = single_report_current(
                connection, workflow_id, row["terminal_handle"]
            )
            return result

    def status(self, batch_id: str) -> dict[str, object]:
        with self._connect() as connection:
            connection.execute("BEGIN")
            batch = connection.execute(
                "SELECT * FROM batch_runs WHERE id = ?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise ValueError("batch was not found")
            items = connection.execute(
                """
                SELECT i.item_index, i.path, i.workflow_id, i.error, w.mode,
                       w.status AS workflow_status, w.terminal_handle,
                       o.status AS orchestration_status, s.outcome AS single_outcome,
                       s.summary AS single_summary, s.terminal_handle AS report_terminal
                FROM batch_items i
                LEFT JOIN workflows w ON w.id = i.workflow_id
                LEFT JOIN orchestration_runs o ON o.root_manager_id = i.workflow_id
                LEFT JOIN single_reports s ON s.workflow_id = i.workflow_id
                WHERE i.batch_id = ? ORDER BY i.item_index
                """,
                (batch_id,),
            ).fetchall()
            current_reports = {
                row["workflow_id"]: single_report_current(
                    connection, str(row["workflow_id"]), row["terminal_handle"]
                )
                for row in items
            }
        result: list[dict[str, object]] = []
        for row in items:
            item = dict(row)
            if item["error"] is not None:
                state = "start_failed"
            elif item["mode"] == "orchestrated":
                state = (
                    str(item["orchestration_status"])
                    if item["orchestration_status"] in {"completed", "blocked", "failed"}
                    else "running"
                )
            elif item["workflow_status"] in {"completed", "failed", "cancelled"}:
                state = str(item["workflow_status"])
            elif current_reports[item["workflow_id"]]:
                state = str(item["single_outcome"])
            else:
                state = "running"
            result.append(
                {
                    "index": item["item_index"],
                    "path": item["path"],
                    "workflow_id": item["workflow_id"],
                    "state": state,
                    "summary": item["single_summary"] if state in {"done", "blocked"} else None,
                    "error": item["error"],
                }
            )
        return {
            "batch_id": batch_id,
            "status": batch["status"],
            "parent_terminal": batch["parent_terminal"],
            "parent_worktree": batch["parent_worktree"],
            "watcher_handle": batch["watcher_handle"],
            "notification_error": batch["notification_error"],
            "notified_at": batch["notified_at"],
            "ready": batch["status"] != "starting"
            and all(item["state"] != "running" for item in result),
            "items": result,
        }

    def note_notification_error(self, batch_id: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE batch_runs SET notification_error = ?, notification_claim = NULL, "
                "notification_claimed_at = NULL WHERE id = ? AND status = 'watching'",
                (error, batch_id),
            )

    def claim_notification(self, batch_id: str, token: str) -> bool:
        stale_before = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                "UPDATE batch_runs SET notification_claim = ?, notification_claimed_at = ? "
                "WHERE id = ? AND status = 'watching' AND "
                "(notification_claim IS NULL OR notification_claimed_at < ?)",
                (token, _now(), batch_id, stale_before),
            )
        return updated.rowcount == 1

    def mark_notified(self, batch_id: str, *, token: str | None = None) -> None:
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE batch_runs SET status = 'notified', notified_at = ?, "
                "notification_error = NULL, notification_claim = NULL, notification_claimed_at = NULL "
                "WHERE id = ? AND status = 'watching' AND "
                "(? IS NULL OR notification_claim = ?)",
                (_now(), batch_id, token, token),
            )
        if updated.rowcount != 1:
            raise ValueError("batch notification claim changed before delivery was recorded")
