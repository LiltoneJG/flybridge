from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from pathlib import Path

from .storage import ensure_schema, prepare_private_database
from .types import WorkflowMode, WorkflowRole, WorkflowStatus

TRANSITIONS = {
    WorkflowStatus.REQUESTED: {WorkflowStatus.STARTING, WorkflowStatus.CANCELLED},
    WorkflowStatus.STARTING: {
        WorkflowStatus.RUNNING,
        WorkflowStatus.FAILED,
        WorkflowStatus.CANCELLED,
    },
    WorkflowStatus.RUNNING: {
        WorkflowStatus.COMPLETED,
        WorkflowStatus.FAILED,
        WorkflowStatus.CANCELLED,
    },
    WorkflowStatus.COMPLETED: set(),
    WorkflowStatus.FAILED: set(),
    WorkflowStatus.CANCELLED: set(),
}
_WORKFLOW_COLUMNS = (
    "id, repository, mode, name, objective, role, parent_id, status, adapter_reference, "
    "worktree_path, terminal_handle, error, cleanup_error, external_reconciled_at, "
    "created_at, updated_at"
)
SCHEMA_VERSION = 4
_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE workflows (
        id TEXT PRIMARY KEY,
        repository TEXT NOT NULL,
        mode TEXT NOT NULL CHECK(mode IN ('single', 'orchestrated')),
        name TEXT NOT NULL,
        objective TEXT NOT NULL,
        role TEXT NOT NULL,
        parent_id TEXT REFERENCES workflows(id),
        status TEXT NOT NULL CHECK(status IN (
            'requested', 'starting', 'running', 'completed', 'failed', 'cancelled'
        )),
        adapter_reference TEXT,
        worktree_path TEXT,
        terminal_handle TEXT,
        error TEXT,
        cleanup_error TEXT,
        external_reconciled_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE owned_terminals (
        workflow_id TEXT NOT NULL REFERENCES workflows(id),
        handle TEXT NOT NULL,
        kind TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (workflow_id, handle)
    )
    """,
    """
    CREATE TABLE workflow_handoffs (
        source_workflow_id TEXT PRIMARY KEY REFERENCES workflows(id),
        target_workflow_id TEXT NOT NULL REFERENCES workflows(id),
        summary TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE workflow_lifecycle_operations (
        workflow_id TEXT PRIMARY KEY REFERENCES workflows(id),
        kind TEXT NOT NULL CHECK(kind IN ('activation', 'terminal')),
        target TEXT,
        adapter_reference TEXT NOT NULL,
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX workflow_name ON workflows(name)",
    """
    CREATE UNIQUE INDEX workflow_owned_adapter_reference
        ON workflows(adapter_reference)
        WHERE adapter_reference IS NOT NULL AND external_reconciled_at IS NULL
    """,
    """
    CREATE UNIQUE INDEX workflow_manager_role
        ON workflows(parent_id, role)
        WHERE parent_id IS NOT NULL AND role IN ('worker', 'reviewer')
    """,
    "CREATE UNIQUE INDEX workflow_handoff_target ON workflow_handoffs(target_workflow_id)",
)
_SCHEMA_TABLES = {
    "workflows": tuple(column.strip() for column in _WORKFLOW_COLUMNS.split(",")),
    "owned_terminals": ("workflow_id", "handle", "kind", "created_at"),
    "workflow_handoffs": ("source_workflow_id", "target_workflow_id", "summary", "created_at"),
    "workflow_lifecycle_operations": (
        "workflow_id",
        "kind",
        "target",
        "adapter_reference",
        "error",
        "created_at",
        "updated_at",
    ),
}
_SCHEMA_INDEXES = (
    "workflow_name",
    "workflow_owned_adapter_reference",
    "workflow_manager_role",
    "workflow_handoff_target",
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class WorkflowRecord:
    id: str
    repository: str
    mode: WorkflowMode
    name: str
    objective: str
    role: WorkflowRole
    parent_id: str | None
    status: WorkflowStatus
    adapter_reference: str | None
    worktree_path: str | None
    terminal_handle: str | None
    error: str | None
    cleanup_error: str | None
    external_reconciled_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class WorkflowHandoff:
    source_workflow_id: str
    target_workflow_id: str
    summary: str
    created_at: str


class AdapterReferenceConflict(ValueError):
    """An unreconciled workflow already owns an adapter reference."""


class LifecycleOperationConflict(ValueError):
    """Another process currently owns this workflow's lifecycle callback."""

    def __init__(self, message: str, *, kind: str = "") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class LifecycleClaim:
    workflow: WorkflowRecord
    adapter_reference: str | None
    affected_workflow_ids: tuple[str, ...] = ()


class WorkflowStore:
    """Durable workflow records with explicit, adapter-neutral transitions."""

    def __init__(self, state_dir: Path) -> None:
        self.path = prepare_private_database(state_dir, "workflows.sqlite3")
        with closing(self._connect()) as connection, connection:
            ensure_schema(
                connection,
                self.path,
                label="state",
                version=SCHEMA_VERSION,
                statements=_SCHEMA_STATEMENTS,
                tables=_SCHEMA_TABLES,
                indexes=_SCHEMA_INDEXES,
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @staticmethod
    def _record(row: sqlite3.Row) -> WorkflowRecord:
        values = dict(row)
        values["mode"] = WorkflowMode(values["mode"])
        values["role"] = WorkflowRole(values["role"])
        values["status"] = WorkflowStatus(values["status"])
        return WorkflowRecord(**values)

    def create(
        self,
        repository: Path,
        mode: str | WorkflowMode,
        name: str,
        objective: str,
        *,
        role: str | WorkflowRole = WorkflowRole.SINGLE,
        parent_id: str | None = None,
    ) -> WorkflowRecord:
        try:
            mode = WorkflowMode(mode)
            role = WorkflowRole(role)
        except ValueError as exc:
            raise ValueError("workflow mode or role is invalid") from exc
        if mode not in WorkflowMode:
            raise ValueError("workflow mode must be 'single' or 'orchestrated'")
        if not name.strip() or not objective.strip():
            raise ValueError("workflow name and objective are required")
        objective = objective.strip()
        if mode == WorkflowMode.SINGLE and (role != WorkflowRole.SINGLE or parent_id is not None):
            raise ValueError("single workflows must use the single role without a parent")
        if mode == WorkflowMode.ORCHESTRATED and parent_id is None and role != WorkflowRole.MANAGER:
            raise ValueError("an orchestrated root must use the manager role")
        workflow_id = str(uuid.uuid4())
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if parent_id is not None:
                    parent_row = connection.execute(
                        f"SELECT {_WORKFLOW_COLUMNS} FROM workflows WHERE id = ?", (parent_id,)
                    ).fetchone()
                    if parent_row is None:
                        raise ValueError("workflow was not found")
                    parent = self._record(parent_row)
                    if (
                        parent.mode != WorkflowMode.ORCHESTRATED
                        or parent.role != WorkflowRole.MANAGER
                        or role not in {WorkflowRole.WORKER, WorkflowRole.REVIEWER}
                    ):
                        raise ValueError(
                            "only an orchestrated manager can own worker or reviewer roles"
                        )
                    duplicate_role = connection.execute(
                        "SELECT 1 FROM workflows WHERE parent_id = ? AND role = ?",
                        (parent_id, role.value),
                    ).fetchone()
                    if duplicate_role is not None:
                        raise ValueError(f"manager already has a {role.value} workflow")
                connection.execute(
                    """
                    INSERT INTO workflows(
                        id, repository, mode, name, objective, role, parent_id, status,
                        adapter_reference, worktree_path, terminal_handle, error, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'requested', NULL, NULL, NULL, NULL, ?, ?)
                    """,
                    (
                        workflow_id,
                        str(repository.resolve()),
                        mode.value,
                        name,
                        objective,
                        role.value,
                        parent_id,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if parent_id is not None and role in {WorkflowRole.WORKER, WorkflowRole.REVIEWER}:
                with self._connect() as connection:
                    duplicate_role = connection.execute(
                        "SELECT 1 FROM workflows WHERE parent_id = ? AND role = ?",
                        (parent_id, role.value),
                    ).fetchone()
                if duplicate_role is not None:
                    raise ValueError(f"manager already has a {role.value} workflow") from exc
            raise ValueError(f"workflow name already exists: {name}") from exc
        return self.get(workflow_id)

    def create_orchestrated_plan(
        self, repository: Path, name: str, objective: str
    ) -> list[WorkflowRecord]:
        """Create the complete fixed role plan atomically, before any Orca side effect."""
        if not name.strip() or not objective.strip():
            raise ValueError("workflow name and objective are required")
        objective = objective.strip()
        now = _now()
        manager_id, worker_id, reviewer_id = (str(uuid.uuid4()) for _ in range(3))
        rows = (
            (manager_id, name, WorkflowRole.MANAGER, None),
            (worker_id, f"{name}-worker", WorkflowRole.WORKER, manager_id),
            (reviewer_id, f"{name}-reviewer", WorkflowRole.REVIEWER, manager_id),
        )
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                for workflow_id, workflow_name, role, parent_id in rows:
                    connection.execute(
                        """
                        INSERT INTO workflows(id, repository, mode, name, objective, role, parent_id,
                        status, adapter_reference, worktree_path, terminal_handle, error, created_at, updated_at)
                        VALUES (?, ?, 'orchestrated', ?, ?, ?, ?, 'requested', NULL, NULL, NULL, NULL, ?, ?)
                        """,
                        (
                            workflow_id,
                            str(repository.resolve()),
                            workflow_name,
                            objective,
                            role.value,
                            parent_id,
                            now,
                            now,
                        ),
                    )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"workflow role-plan name already exists: {name}") from exc
        return [self.get(workflow_id) for workflow_id, *_ in rows]

    def reserve_root_plan(
        self,
        repository: Path,
        mode: str | WorkflowMode,
        name: str,
        objective: str,
        *,
        allow_duplicate: bool = False,
    ) -> list[WorkflowRecord]:
        """Atomically create a root plan and claim its root for external startup."""
        try:
            mode = WorkflowMode(mode)
        except ValueError as exc:
            raise ValueError("workflow mode is invalid") from exc
        if not name.strip() or not objective.strip():
            raise ValueError("workflow name and objective are required")
        objective = objective.strip()
        repository_text = str(repository.resolve())
        now = _now()
        manager_id = str(uuid.uuid4())
        if mode == WorkflowMode.ORCHESTRATED:
            worker_id, reviewer_id = str(uuid.uuid4()), str(uuid.uuid4())
            rows = (
                (manager_id, name, WorkflowRole.MANAGER, None, WorkflowStatus.STARTING),
                (
                    worker_id,
                    f"{name}-worker",
                    WorkflowRole.WORKER,
                    manager_id,
                    WorkflowStatus.REQUESTED,
                ),
                (
                    reviewer_id,
                    f"{name}-reviewer",
                    WorkflowRole.REVIEWER,
                    manager_id,
                    WorkflowStatus.REQUESTED,
                ),
            )
        else:
            rows = (
                (
                    manager_id,
                    name,
                    WorkflowRole.SINGLE,
                    None,
                    WorkflowStatus.STARTING,
                ),
            )
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if not allow_duplicate:
                    duplicate = connection.execute(
                        """
                        SELECT 1 FROM workflows
                        WHERE repository = ? AND objective = ? AND parent_id IS NULL
                            AND status IN ('requested', 'starting', 'running')
                        LIMIT 1
                        """,
                        (repository_text, objective),
                    ).fetchone()
                    if duplicate is not None:
                        raise ValueError(
                            "an active root workflow already has this repository and objective; "
                            "use --allow-duplicate to override"
                        )
                for workflow_id, workflow_name, role, parent_id, status in rows:
                    connection.execute(
                        """
                        INSERT INTO workflows(
                            id, repository, mode, name, objective, role, parent_id, status,
                            adapter_reference, worktree_path, terminal_handle, error,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?)
                        """,
                        (
                            workflow_id,
                            repository_text,
                            mode.value,
                            workflow_name,
                            objective,
                            role.value,
                            parent_id,
                            status.value,
                            now,
                            now,
                        ),
                    )
        except sqlite3.IntegrityError as exc:
            detail = (
                "workflow role-plan name" if mode == WorkflowMode.ORCHESTRATED else "workflow name"
            )
            raise ValueError(f"{detail} already exists: {name}") from exc
        return [self.get(workflow_id) for workflow_id, *_ in rows]

    def attach_external(
        self,
        workflow_id: str,
        *,
        adapter_reference: str,
        worktree_path: str,
        terminal_handle: str,
    ) -> WorkflowRecord:
        """Persist Orca ownership before its lifecycle metadata is updated."""
        if not adapter_reference or not worktree_path:
            raise ValueError("adapter did not return complete worktree ownership metadata")
        if not terminal_handle:
            raise ValueError("adapter did not return an owned terminal handle")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
            if row is None:
                raise ValueError("workflow was not found")
            if row["status"] != WorkflowStatus.STARTING.value:
                raise ValueError("external metadata can only be attached while starting")
            owner = connection.execute(
                """
                SELECT id FROM workflows
                WHERE adapter_reference = ? AND external_reconciled_at IS NULL AND id != ?
                """,
                (adapter_reference, workflow_id),
            ).fetchone()
            if owner is not None:
                raise AdapterReferenceConflict(
                    f"Orca worktree reference is already owned by workflow {owner['id']}"
                )
            try:
                connection.execute(
                    """
                    UPDATE workflows
                    SET adapter_reference = ?, worktree_path = ?, terminal_handle = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (adapter_reference, worktree_path, terminal_handle, _now(), workflow_id),
                )
            except sqlite3.IntegrityError as exc:
                raise AdapterReferenceConflict("Orca worktree reference is already owned") from exc
            connection.execute(
                "INSERT OR IGNORE INTO owned_terminals VALUES (?, ?, 'agent', ?)",
                (workflow_id, terminal_handle, _now()),
            )
        return self.get(workflow_id)

    def attach_partial_external(
        self, workflow_id: str, *, adapter_reference: str, worktree_path: str
    ) -> WorkflowRecord:
        """Persist ownership when an adapter failed before returning a terminal."""
        if not adapter_reference:
            raise ValueError("partial external ownership requires an ID")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
            if row is None or row["status"] != WorkflowStatus.STARTING.value:
                raise ValueError("partial external metadata can only attach while starting")
            owner = connection.execute(
                """
                SELECT id FROM workflows
                WHERE adapter_reference = ? AND external_reconciled_at IS NULL AND id != ?
                """,
                (adapter_reference, workflow_id),
            ).fetchone()
            if owner is not None:
                raise AdapterReferenceConflict(
                    f"Orca worktree reference is already owned by workflow {owner['id']}"
                )
            try:
                connection.execute(
                    "UPDATE workflows SET adapter_reference = ?, worktree_path = ?, updated_at = ? "
                    "WHERE id = ?",
                    (adapter_reference, worktree_path, _now(), workflow_id),
                )
            except sqlite3.IntegrityError as exc:
                raise AdapterReferenceConflict("Orca worktree reference is already owned") from exc
        return self.get(workflow_id)

    def add_owned_terminal(
        self, workflow_id: str, handle: str, kind: str, *, require_running: bool = False
    ) -> None:
        if not handle or not kind:
            raise ValueError("owned terminal handle and kind are required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
            if row is None:
                raise ValueError("workflow was not found")
            if require_running:
                operation = connection.execute(
                    "SELECT 1 FROM workflow_lifecycle_operations WHERE workflow_id = ?",
                    (workflow_id,),
                ).fetchone()
                if row["status"] != WorkflowStatus.RUNNING.value or operation is not None:
                    raise LifecycleOperationConflict(
                        "workflow lifecycle changed before terminal ownership was recorded",
                        kind=str(operation["kind"]) if operation is not None else "terminal",
                    )
            connection.execute(
                "INSERT OR IGNORE INTO owned_terminals VALUES (?, ?, ?, ?)",
                (workflow_id, handle, kind, _now()),
            )

    def replace_agent_terminal(
        self, workflow_id: str, expected_handle: str | None, replacement_handle: str
    ) -> WorkflowRecord:
        """Atomically replace only the workflow's agent terminal ownership."""
        if not replacement_handle:
            raise ValueError("replacement agent terminal handle is required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, terminal_handle FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
            if row is None:
                raise ValueError("workflow was not found")
            if row["status"] != WorkflowStatus.RUNNING.value:
                raise ValueError("agent terminal can only be replaced for a running workflow")
            if row["terminal_handle"] != expected_handle:
                raise ValueError("agent terminal ownership changed during resume")
            connection.execute(
                "DELETE FROM owned_terminals WHERE workflow_id = ? AND kind = 'agent'",
                (workflow_id,),
            )
            connection.execute(
                "INSERT INTO owned_terminals VALUES (?, ?, 'agent', ?)",
                (workflow_id, replacement_handle, _now()),
            )
            updated = connection.execute(
                "UPDATE workflows SET terminal_handle = ?, updated_at = ? "
                "WHERE id = ? AND terminal_handle IS ?",
                (replacement_handle, _now(), workflow_id, expected_handle),
            )
            if updated.rowcount != 1:
                raise ValueError("agent terminal ownership changed during resume")
        return self.get(workflow_id)

    def owned_terminal_handles(self, workflow_id: str) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT handle FROM owned_terminals WHERE workflow_id = ? ORDER BY created_at, handle",
                (workflow_id,),
            ).fetchall()
        return [str(row["handle"]) for row in rows]

    def find_by_adapter_reference(self, adapter_reference: str) -> WorkflowRecord:
        """Return the active Flybridge workflow that owns an Orca worktree."""
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_WORKFLOW_COLUMNS} FROM workflows WHERE adapter_reference = ? "
                "AND status IN ('starting', 'running')",
                (adapter_reference,),
            ).fetchone()
        if row is None:
            raise ValueError("active Flybridge workflow for Orca worktree was not found")
        return self._record(row)

    def lifecycle_operation(self, workflow_id: str) -> tuple[str, str | None, str] | None:
        """Return the in-flight lifecycle kind, error, and update time, if any."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT kind, error, updated_at FROM workflow_lifecycle_operations "
                "WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
        if row is None:
            return None
        return (
            str(row["kind"]),
            None if row["error"] is None else str(row["error"]),
            str(row["updated_at"]),
        )

    def get(self, workflow_id: str) -> WorkflowRecord:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_WORKFLOW_COLUMNS} FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
        if row is None:
            raise ValueError("workflow was not found")
        return self._record(row)

    def assert_no_active_root(self, repository: Path, objective: str) -> None:
        with self._connect() as connection:
            duplicate = connection.execute(
                """
                SELECT 1 FROM workflows
                WHERE repository = ? AND objective = ? AND parent_id IS NULL
                    AND status IN ('requested', 'starting', 'running')
                LIMIT 1
                """,
                (str(repository.resolve()), objective.strip()),
            ).fetchone()
        if duplicate is not None:
            raise ValueError(
                "an active root workflow already has this repository and objective; "
                "use --allow-duplicate to override"
            )

    def begin_start(self, workflow_id: str, *, allow_duplicate: bool = False) -> WorkflowRecord:
        """Claim a requested workflow while preventing duplicate active root objectives."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT {_WORKFLOW_COLUMNS} FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
            if row is None:
                raise ValueError("workflow was not found")
            workflow = self._record(row)
            if workflow.status != WorkflowStatus.REQUESTED:
                raise ValueError("only a requested workflow can be started")
            if workflow.parent_id is None and not allow_duplicate:
                duplicate = connection.execute(
                    """
                    SELECT id FROM workflows
                    WHERE repository = ? AND objective = ? AND parent_id IS NULL
                        AND status IN ('requested', 'starting', 'running') AND id != ?
                    LIMIT 1
                    """,
                    (workflow.repository, workflow.objective, workflow.id),
                ).fetchone()
                if duplicate is not None:
                    raise ValueError(
                        "an active root workflow already has this repository and objective; "
                        "use --allow-duplicate to override"
                    )
            updated = connection.execute(
                "UPDATE workflows SET status = 'starting', updated_at = ? "
                "WHERE id = ? AND status = 'requested'",
                (_now(), workflow_id),
            )
            if updated.rowcount != 1:
                raise ValueError("workflow start ownership changed")
        return self.get(workflow_id)

    def mark_resumed(self, workflow_id: str) -> WorkflowRecord:
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE workflows SET updated_at = ? WHERE id = ? AND status = 'running'",
                (_now(), workflow_id),
            )
            if updated.rowcount != 1:
                raise ValueError("only a running workflow can be marked resumed")
        return self.get(workflow_id)

    def transition(
        self,
        workflow_id: str,
        target: str | WorkflowStatus,
        *,
        adapter_reference: str | None = None,
        error: str | None = None,
    ) -> WorkflowRecord:
        try:
            target = WorkflowStatus(target)
        except ValueError as exc:
            raise ValueError(f"unknown workflow state: {target}") from exc
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT {_WORKFLOW_COLUMNS} FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
            if row is None:
                raise ValueError("workflow was not found")
            current = WorkflowStatus(row["status"])
            if target not in TRANSITIONS[current]:
                raise ValueError(f"cannot transition workflow from {current} to {target}")
            connection.execute(
                """
                UPDATE workflows
                SET status = ?, adapter_reference = COALESCE(?, adapter_reference),
                    error = COALESCE(?, error), updated_at = ?
                WHERE id = ?
                """,
                (target.value, adapter_reference, error, _now(), workflow_id),
            )
        return self.get(workflow_id)

    def claim_activation(self, workflow_id: str) -> str:
        """Persist exclusive ownership of an activation callback."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, adapter_reference FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
            if row is None:
                raise ValueError("workflow was not found")
            if row["status"] != WorkflowStatus.STARTING.value or not row["adapter_reference"]:
                raise ValueError("only an attached starting workflow can be activated")
            now = _now()
            try:
                connection.execute(
                    """
                    INSERT INTO workflow_lifecycle_operations(
                        workflow_id, kind, target, adapter_reference, error, created_at, updated_at
                    ) VALUES (?, 'activation', NULL, ?, NULL, ?, ?)
                    """,
                    (workflow_id, str(row["adapter_reference"]), now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise LifecycleOperationConflict(
                    "workflow lifecycle operation is already in progress",
                    kind="activation",
                ) from exc
        return str(row["adapter_reference"])

    def complete_activation(self, workflow_id: str) -> WorkflowRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            operation = connection.execute(
                "SELECT kind FROM workflow_lifecycle_operations WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
            if operation is None or operation["kind"] != "activation":
                raise ValueError("workflow activation ownership was lost")
            updated = connection.execute(
                "UPDATE workflows SET status = 'running', updated_at = ? "
                "WHERE id = ? AND status = 'starting'",
                (_now(), workflow_id),
            )
            if updated.rowcount != 1:
                raise ValueError("workflow activation lost its starting-state ownership")
            connection.execute(
                "DELETE FROM workflow_lifecycle_operations WHERE workflow_id = ?",
                (workflow_id,),
            )
        return self.get(workflow_id)

    def release_activation(self, workflow_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM workflow_lifecycle_operations "
                "WHERE workflow_id = ? AND kind = 'activation'",
                (workflow_id,),
            )

    def claim_terminal_transition(
        self,
        workflow_id: str,
        target: str | WorkflowStatus,
        *,
        error: str | None = None,
    ) -> LifecycleClaim:
        """Claim one terminal outcome and snapshot ownership in the same transaction."""
        target = WorkflowStatus(target)
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT {_WORKFLOW_COLUMNS} FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
            if row is None:
                raise ValueError("workflow was not found")
            workflow = self._record(row)
            operation = connection.execute(
                "SELECT * FROM workflow_lifecycle_operations WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
            if operation is not None:
                if operation["kind"] == "activation":
                    raise LifecycleOperationConflict(
                        "workflow activation is in progress; if its process exited, "
                        "reconcile it with cleanup",
                        kind="activation",
                    )
                if operation["target"] != target.value:
                    raise ValueError("another terminal outcome already owns this workflow")
                if operation["error"] is not None:
                    connection.execute(
                        "UPDATE workflow_lifecycle_operations SET error = NULL, updated_at = ? "
                        "WHERE workflow_id = ?",
                        (now, workflow_id),
                    )
                return LifecycleClaim(
                    workflow=workflow,
                    adapter_reference=str(operation["adapter_reference"]),
                    affected_workflow_ids=(workflow.id,),
                )

            valid_running = workflow.status == WorkflowStatus.RUNNING
            valid_early_cancel = target == WorkflowStatus.CANCELLED and workflow.status in {
                WorkflowStatus.REQUESTED,
                WorkflowStatus.STARTING,
            }
            if not (valid_running or valid_early_cancel):
                raise ValueError("workflow state cannot be finished with the requested target")
            if valid_running and not workflow.adapter_reference:
                raise ValueError("running workflow has no adapter reference")
            adapter_reference = workflow.adapter_reference
            affected_ids: list[str] = [workflow.id]
            if valid_early_cancel:
                connection.execute(
                    "UPDATE workflows SET status = 'cancelled', error = COALESCE(?, error), "
                    "updated_at = ? WHERE id = ?",
                    (error, now, workflow_id),
                )
                if workflow.mode == WorkflowMode.ORCHESTRATED:
                    parent_id = (
                        workflow.id if workflow.role == WorkflowRole.MANAGER else workflow.parent_id
                    )
                    roles = (
                        (WorkflowRole.WORKER.value, WorkflowRole.REVIEWER.value)
                        if workflow.role == WorkflowRole.MANAGER
                        else (WorkflowRole.REVIEWER.value,)
                    )
                    if parent_id:
                        placeholders = ", ".join("?" for _ in roles)
                        successors = connection.execute(
                            f"SELECT id FROM workflows WHERE parent_id = ? "
                            f"AND role IN ({placeholders}) AND status = 'requested'",
                            (parent_id, *roles),
                        ).fetchall()
                        successor_ids = [str(successor["id"]) for successor in successors]
                        if successor_ids:
                            id_placeholders = ", ".join("?" for _ in successor_ids)
                            connection.execute(
                                f"UPDATE workflows SET status = 'cancelled', updated_at = ? "
                                f"WHERE id IN ({id_placeholders})",
                                (now, *successor_ids),
                            )
                            affected_ids.extend(successor_ids)
            if adapter_reference:
                connection.execute(
                    """
                    INSERT INTO workflow_lifecycle_operations(
                        workflow_id, kind, target, adapter_reference, error, created_at, updated_at
                    ) VALUES (?, 'terminal', ?, ?, NULL, ?, ?)
                    """,
                    (workflow_id, target.value, adapter_reference, now, now),
                )
        return LifecycleClaim(
            workflow=self.get(workflow_id),
            adapter_reference=adapter_reference,
            affected_workflow_ids=tuple(affected_ids),
        )

    def fail_terminal_update(self, workflow_id: str, detail: str) -> None:
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE workflow_lifecycle_operations SET error = ?, updated_at = ? "
                "WHERE workflow_id = ? AND kind = 'terminal'",
                (detail, _now(), workflow_id),
            )
            if updated.rowcount != 1:
                raise ValueError("workflow terminal lifecycle ownership was lost")

    def claim_stale_reconciliation(
        self, workflow_id: str, max_age_seconds: float
    ) -> WorkflowRecord:
        """Replace a stale lifecycle claim with exclusive cleanup ownership."""
        if (
            isinstance(max_age_seconds, bool)
            or not isinstance(max_age_seconds, (int, float))
            or not isfinite(max_age_seconds)
            or max_age_seconds <= 0
        ):
            raise ValueError("max_age_seconds must be a finite positive number")
        cutoff = (datetime.now(UTC) - timedelta(seconds=max_age_seconds)).isoformat()
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT {_WORKFLOW_COLUMNS} FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
            if row is None:
                raise ValueError("workflow was not found")
            workflow = self._record(row)
            if (
                not workflow.adapter_reference
                or workflow.external_reconciled_at is not None
                or workflow.status == WorkflowStatus.COMPLETED
            ):
                raise ValueError("workflow is not externally reconcilable")
            operation = connection.execute(
                "SELECT error, updated_at FROM workflow_lifecycle_operations WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
            if (
                operation is not None
                and operation["error"] is None
                and str(operation["updated_at"]) >= cutoff
            ):
                raise LifecycleOperationConflict(
                    "workflow lifecycle operation is in progress",
                    kind="terminal",
                )
            connection.execute(
                "DELETE FROM workflow_lifecycle_operations WHERE workflow_id = ?",
                (workflow_id,),
            )
            connection.execute(
                """
                INSERT INTO workflow_lifecycle_operations(
                    workflow_id, kind, target, adapter_reference, error, created_at, updated_at
                ) VALUES (?, 'terminal', 'cancelled', ?, NULL, ?, ?)
                """,
                (workflow_id, workflow.adapter_reference, now, now),
            )
        return self.get(workflow_id)

    def complete_stale_reconciliation(
        self, workflow_id: str
    ) -> tuple[WorkflowRecord, tuple[str, ...]]:
        """Atomically release external ownership and cancel an active workflow."""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            operation = connection.execute(
                "SELECT kind, target FROM workflow_lifecycle_operations WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
            row = connection.execute(
                f"SELECT {_WORKFLOW_COLUMNS} FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
            if row is None:
                raise ValueError("workflow was not found")
            workflow = self._record(row)
            if (
                operation is None
                or operation["kind"] != "terminal"
                or operation["target"] != WorkflowStatus.CANCELLED.value
                or not workflow.adapter_reference
                or workflow.external_reconciled_at is not None
                or workflow.status == WorkflowStatus.COMPLETED
            ):
                raise ValueError("workflow reconciliation ownership was lost")
            affected_ids = [workflow_id]
            if workflow.status in {
                WorkflowStatus.REQUESTED,
                WorkflowStatus.STARTING,
                WorkflowStatus.RUNNING,
            }:
                connection.execute(
                    "UPDATE workflows SET status = 'cancelled', "
                    "error = 'stale workflow reconciled by explicit cleanup', "
                    "external_reconciled_at = ?, cleanup_error = NULL, updated_at = ? "
                    "WHERE id = ?",
                    (now, now, workflow_id),
                )
                if workflow.mode == WorkflowMode.ORCHESTRATED:
                    parent_id = (
                        workflow.id if workflow.role == WorkflowRole.MANAGER else workflow.parent_id
                    )
                    roles = (
                        (WorkflowRole.WORKER.value, WorkflowRole.REVIEWER.value)
                        if workflow.role == WorkflowRole.MANAGER
                        else (WorkflowRole.REVIEWER.value,)
                    )
                    if parent_id:
                        placeholders = ", ".join("?" for _ in roles)
                        successors = connection.execute(
                            f"SELECT id FROM workflows WHERE parent_id = ? "
                            f"AND role IN ({placeholders}) AND status = 'requested'",
                            (parent_id, *roles),
                        ).fetchall()
                        successor_ids = [str(successor["id"]) for successor in successors]
                        if successor_ids:
                            id_placeholders = ", ".join("?" for _ in successor_ids)
                            connection.execute(
                                f"UPDATE workflows SET status = 'cancelled', updated_at = ? "
                                f"WHERE id IN ({id_placeholders})",
                                (now, *successor_ids),
                            )
                            affected_ids.extend(successor_ids)
            else:
                connection.execute(
                    "UPDATE workflows SET external_reconciled_at = ?, cleanup_error = NULL, "
                    "updated_at = ? WHERE id = ?",
                    (now, now, workflow_id),
                )
            connection.execute(
                "DELETE FROM workflow_lifecycle_operations WHERE workflow_id = ?",
                (workflow_id,),
            )
        return self.get(workflow_id), tuple(affected_ids)

    def complete_terminal_transition(
        self, workflow_id: str, target: str | WorkflowStatus, *, error: str | None = None
    ) -> WorkflowRecord:
        target = WorkflowStatus(target)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            operation = connection.execute(
                "SELECT kind, target FROM workflow_lifecycle_operations WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
            row = connection.execute(
                "SELECT status FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
            if row is None:
                raise ValueError("workflow was not found")
            if operation is None and row["status"] == target.value:
                return self.get(workflow_id)
            if (
                operation is None
                or operation["kind"] != "terminal"
                or operation["target"] != target.value
            ):
                raise ValueError("workflow terminal lifecycle ownership was lost")
            if row["status"] == WorkflowStatus.RUNNING.value:
                connection.execute(
                    "UPDATE workflows SET status = ?, error = COALESCE(?, error), "
                    "cleanup_error = CASE WHEN ? = 'completed' "
                    "THEN 'owned terminal cleanup pending' ELSE cleanup_error END, "
                    "updated_at = ? WHERE id = ? AND status = 'running'",
                    (target.value, error, target.value, _now(), workflow_id),
                )
            elif not (
                target == WorkflowStatus.CANCELLED
                and row["status"] == WorkflowStatus.CANCELLED.value
            ):
                raise ValueError("workflow terminal state changed before lifecycle completion")
            connection.execute(
                "DELETE FROM workflow_lifecycle_operations WHERE workflow_id = ?",
                (workflow_id,),
            )
        return self.get(workflow_id)

    def set_cleanup_error(self, workflow_id: str, error: str | None) -> WorkflowRecord:
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE workflows SET cleanup_error = ?, updated_at = ? WHERE id = ?",
                (error, _now(), workflow_id),
            )
            if updated.rowcount != 1:
                raise ValueError("workflow was not found")
        return self.get(workflow_id)

    def cancel_requested_successors(self, workflow_id: str) -> list[WorkflowRecord]:
        """Cancel role-plan records that cannot run after a terminal predecessor."""
        source = self.get(workflow_id)
        if source.mode != WorkflowMode.ORCHESTRATED:
            return []
        parent_id = source.id if source.role == WorkflowRole.MANAGER else source.parent_id
        if parent_id is None:
            return []
        roles = (
            (WorkflowRole.WORKER.value, WorkflowRole.REVIEWER.value)
            if source.role == WorkflowRole.MANAGER
            else (WorkflowRole.REVIEWER.value,)
        )
        placeholders = ", ".join("?" for _ in roles)
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                f"SELECT id FROM workflows WHERE parent_id = ? AND role IN ({placeholders}) "
                "AND status = 'requested'",
                (parent_id, *roles),
            ).fetchall()
            ids = [str(row["id"]) for row in rows]
            if ids:
                id_placeholders = ", ".join("?" for _ in ids)
                connection.execute(
                    f"UPDATE workflows SET status = 'cancelled', updated_at = ? "
                    f"WHERE id IN ({id_placeholders})",
                    (now, *ids),
                )
        return [self.get(workflow_id) for workflow_id in ids]

    def retry_failed(self, workflow_id: str) -> WorkflowRecord:
        """Return a reconciled failed role to its original requested state."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT {_WORKFLOW_COLUMNS} FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
            if row is None:
                raise ValueError("workflow was not found")
            workflow = self._record(row)
            if workflow.status != WorkflowStatus.FAILED:
                raise ValueError("only a failed workflow can be retried")
            if workflow.adapter_reference and workflow.external_reconciled_at is None:
                raise ValueError("failed workflow must be externally reconciled before retry")
            updated = connection.execute(
                """
                UPDATE workflows
                SET status = 'requested', adapter_reference = NULL, worktree_path = NULL,
                    terminal_handle = NULL, error = NULL, cleanup_error = NULL,
                    external_reconciled_at = NULL,
                    updated_at = ?
                WHERE id = ? AND status = 'failed'
                """,
                (_now(), workflow_id),
            )
            if updated.rowcount != 1:
                raise ValueError("failed workflow changed while it was being retried")
            connection.execute("DELETE FROM owned_terminals WHERE workflow_id = ?", (workflow_id,))
            if workflow.mode == WorkflowMode.ORCHESTRATED:
                parent_id = (
                    workflow.id if workflow.role == WorkflowRole.MANAGER else workflow.parent_id
                )
                successor_roles = (
                    (WorkflowRole.WORKER.value, WorkflowRole.REVIEWER.value)
                    if workflow.role == WorkflowRole.MANAGER
                    else (
                        (WorkflowRole.REVIEWER.value,)
                        if workflow.role == WorkflowRole.WORKER
                        else ()
                    )
                )
                if parent_id and successor_roles:
                    placeholders = ", ".join("?" for _ in successor_roles)
                    connection.execute(
                        f"UPDATE workflows SET status = 'requested', updated_at = ? "
                        f"WHERE parent_id = ? AND role IN ({placeholders}) "
                        "AND status = 'cancelled'",
                        (_now(), parent_id, *successor_roles),
                    )
        return self.get(workflow_id)

    def active(self) -> list[WorkflowRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {_WORKFLOW_COLUMNS} FROM workflows WHERE status IN ('requested', 'starting', 'running') "
                "ORDER BY created_at, id"
            ).fetchall()
        return [self._record(row) for row in rows]

    def children(self, parent_id: str) -> list[WorkflowRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {_WORKFLOW_COLUMNS} FROM workflows WHERE parent_id = ? "
                "ORDER BY CASE role WHEN 'worker' THEN 1 WHEN 'reviewer' THEN 2 ELSE 3 END, id",
                (parent_id,),
            ).fetchall()
        return [self._record(row) for row in rows]

    def record_handoff(self, source_id: str, target_id: str, summary: str) -> WorkflowHandoff:
        if not summary.strip() or "\n" in summary:
            raise ValueError("handoff summary must be a non-empty single line")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            source_row = connection.execute(
                f"SELECT {_WORKFLOW_COLUMNS} FROM workflows WHERE id = ?", (source_id,)
            ).fetchone()
            target_row = connection.execute(
                f"SELECT {_WORKFLOW_COLUMNS} FROM workflows WHERE id = ?", (target_id,)
            ).fetchone()
            if source_row is None or target_row is None:
                raise ValueError("handoff source or target workflow was not found")
            source, target = self._record(source_row), self._record(target_row)
            if (
                source.status != WorkflowStatus.COMPLETED
                or target.status != WorkflowStatus.REQUESTED
            ):
                raise ValueError("handoff requires a completed source and requested target")
            manager_to_worker = (
                source.role == WorkflowRole.MANAGER
                and target.role == WorkflowRole.WORKER
                and target.parent_id == source.id
            )
            worker_to_reviewer = (
                source.role == WorkflowRole.WORKER
                and target.role == WorkflowRole.REVIEWER
                and source.parent_id is not None
                and source.parent_id == target.parent_id
            )
            if not (manager_to_worker or worker_to_reviewer):
                raise ValueError("handoff source and target do not form a role-plan edge")
            existing_source = connection.execute(
                "SELECT 1 FROM workflow_handoffs WHERE source_workflow_id = ?", (source_id,)
            ).fetchone()
            existing_target = connection.execute(
                "SELECT 1 FROM workflow_handoffs WHERE target_workflow_id = ?", (target_id,)
            ).fetchone()
            if existing_source:
                raise ValueError("handoff source already has a recorded handoff")
            if existing_target:
                raise ValueError("handoff target already has a recorded handoff")
            try:
                connection.execute(
                    "INSERT INTO workflow_handoffs VALUES (?, ?, ?, ?)",
                    (source_id, target_id, summary, _now()),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("handoff was already recorded") from exc
        return self.handoff_for(target_id)

    def handoff_for(self, target_id: str) -> WorkflowHandoff:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_handoffs WHERE target_workflow_id = ?", (target_id,)
            ).fetchone()
        if row is None:
            raise ValueError("required workflow handoff was not found")
        return WorkflowHandoff(**dict(row))

    def stale_active(self, max_age_seconds: float) -> list[WorkflowRecord]:
        if (
            isinstance(max_age_seconds, bool)
            or not isinstance(max_age_seconds, (int, float))
            or not isfinite(max_age_seconds)
            or max_age_seconds <= 0
        ):
            raise ValueError("max_age_seconds must be a finite positive number")
        cutoff = (datetime.now(UTC) - timedelta(seconds=max_age_seconds)).isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT {_WORKFLOW_COLUMNS} FROM workflows
                WHERE status IN ('requested', 'starting', 'running') AND updated_at < ?
                ORDER BY updated_at, id
                """,
                (cutoff,),
            ).fetchall()
        return [self._record(row) for row in rows]

    def stale_reconcilable(
        self, max_age_seconds: float, *, include_failed: bool = True
    ) -> list[WorkflowRecord]:
        if (
            isinstance(max_age_seconds, bool)
            or not isinstance(max_age_seconds, (int, float))
            or not isfinite(max_age_seconds)
            or max_age_seconds <= 0
        ):
            raise ValueError("max_age_seconds must be a finite positive number")
        cutoff = (datetime.now(UTC) - timedelta(seconds=max_age_seconds)).isoformat()
        states = "'starting', 'running', 'cancelled'" + (", 'failed'" if include_failed else "")
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT {_WORKFLOW_COLUMNS} FROM workflows
                WHERE (status IN ({states}) OR (status = 'completed' AND cleanup_error IS NOT NULL))
                    AND adapter_reference IS NOT NULL
                    AND external_reconciled_at IS NULL AND updated_at < ?
                ORDER BY updated_at, id
                """,
                (cutoff,),
            ).fetchall()
        return [self._record(row) for row in rows]

    def mark_external_reconciled(self, workflow_id: str) -> WorkflowRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE workflows SET external_reconciled_at = ?, cleanup_error = NULL, "
                "updated_at = ? WHERE id = ?",
                (_now(), _now(), workflow_id),
            )
            connection.execute(
                "DELETE FROM workflow_lifecycle_operations WHERE workflow_id = ?",
                (workflow_id,),
            )
        return self.get(workflow_id)

    def unreconciled_terminal(self) -> list[WorkflowRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {_WORKFLOW_COLUMNS} FROM workflows "
                "WHERE (status IN ('failed', 'cancelled') "
                "OR (status = 'completed' AND cleanup_error IS NOT NULL)) "
                "AND adapter_reference IS NOT NULL AND external_reconciled_at IS NULL "
                "ORDER BY updated_at, id"
            ).fetchall()
        return [self._record(row) for row in rows]
