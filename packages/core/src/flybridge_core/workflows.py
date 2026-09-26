from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from pathlib import Path

from .database import SCHEMA_VERSION as DATABASE_SCHEMA_VERSION
from .database import connect_database, prepare_database
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
    "id, run_id, repository, mode, name, objective, role, slot, attempt, parent_id, status, adapter_reference, "
    "worktree_path, terminal_handle, error, cleanup_error, external_reconciled_at, "
    "created_at, updated_at, queue_observer_enabled, observer_last_handle, "
    "observer_stopped_at, observer_stop_reason, issue_url, implementation_repository, start_sha, "
    "runtime_repository_id, activated_at"
)
_WORKFLOW_SELECT = (
    "workflows.id AS id, workflows.run_id AS run_id, workflows.repository AS repository, workflows.mode AS mode, "
    "workflows.name AS name, workflows.objective AS objective, workflows.role AS role, "
    "workflows.slot AS slot, workflows.attempt AS attempt, "
    "workflows.parent_id AS parent_id, workflows.status AS status, "
    "workflows.adapter_reference AS adapter_reference, workflows.worktree_path AS worktree_path, "
    "workflows.terminal_handle AS terminal_handle, workflows.error AS error, "
    "workflows.cleanup_error AS cleanup_error, "
    "workflows.external_reconciled_at AS external_reconciled_at, "
    "workflows.queue_observer_enabled AS queue_observer_enabled, "
    "workflows.observer_last_handle AS observer_last_handle, "
    "workflows.observer_stopped_at AS observer_stopped_at, "
    "workflows.observer_stop_reason AS observer_stop_reason, "
    "workflows.issue_url AS issue_url, "
    "workflows.implementation_repository AS implementation_repository, "
    "workflows.start_sha AS start_sha, "
    "workflows.runtime_repository_id AS runtime_repository_id, "
    "workflows.activated_at AS activated_at, "
    "workflows.created_at AS created_at, workflows.updated_at AS updated_at, "
    "COALESCE(worktree_ownership.owns_worktree, 1) AS owns_worktree"
)
_WORKFLOW_FROM = (
    "workflows LEFT JOIN workflow_worktree_ownership AS worktree_ownership "
    "ON worktree_ownership.workflow_id = workflows.id"
)
SCHEMA_VERSION = DATABASE_SCHEMA_VERSION
_INSERT_ORCHESTRATION_RUN = """
    INSERT INTO orchestration_runs(
        root_manager_id, status, current_review_cycle, max_review_cycles, error,
        coordinator_handle, coordinator_released_at, coordinator_release_reason,
        coordinator_error_count, coordinator_last_error, coordinator_retry_at,
        created_at, updated_at
    ) VALUES (?, 'running', 1, ?, NULL, NULL, NULL, NULL, 0, NULL, NULL, ?, ?)
"""


def _role_plan_name(root_name: str, role: WorkflowRole, workflow_id: str, slot: int = 0) -> str:
    """Include a unique suffix so Orca child names do not reuse prior ownership."""
    suffix = workflow_id[:8]
    if role == WorkflowRole.WORKER:
        return f"{root_name}-worker-{suffix}"
    if role == WorkflowRole.REVIEWER:
        return f"{root_name}-reviewer-{slot}-{suffix}"
    return root_name


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _cancel_queue_owners(connection: sqlite3.Connection, owners: list[str]) -> None:
    """Cancel queue ownership and promote FIFO successors in the caller's transaction."""
    leased_resources: set[str] = set()
    for owner in owners:
        rows = connection.execute(
            "SELECT id, resource, status FROM queue_requests WHERE owner=? "
            "AND status IN ('waiting','leased') ORDER BY created_at, id",
            (owner,),
        ).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE queue_requests SET status='cancelled', updated_at=? WHERE id=?",
                (_now(), row["id"]),
            )
            connection.execute(
                "INSERT INTO queue_events(created_at, resource, request_id, event) "
                "VALUES (?, ?, ?, 'cancelled')",
                (_now(), row["resource"], row["id"]),
            )
            if row["status"] == "leased":
                leased_resources.add(str(row["resource"]))
    for resource in sorted(leased_resources):
        row = connection.execute(
            "SELECT id FROM queue_requests WHERE resource=? AND status='waiting' "
            "ORDER BY created_at, id LIMIT 1",
            (resource,),
        ).fetchone()
        if row:
            connection.execute(
                "UPDATE queue_requests SET status='leased', updated_at=? WHERE id=?",
                (_now(), row["id"]),
            )
            connection.execute(
                "INSERT INTO queue_events(created_at, resource, request_id, event) "
                "VALUES (?, ?, ?, 'leased')",
                (_now(), resource, row["id"]),
            )


@dataclass(frozen=True)
class WorkflowRecord:
    id: str
    run_id: str
    repository: str
    mode: WorkflowMode
    name: str
    objective: str
    role: WorkflowRole
    slot: int
    attempt: int
    parent_id: str | None
    status: WorkflowStatus
    adapter_reference: str | None
    worktree_path: str | None
    terminal_handle: str | None
    error: str | None
    cleanup_error: str | None
    external_reconciled_at: str | None
    queue_observer_enabled: bool
    observer_last_handle: str | None
    observer_stopped_at: str | None
    observer_stop_reason: str | None
    issue_url: str | None
    implementation_repository: str | None
    start_sha: str | None
    runtime_repository_id: str | None
    activated_at: str | None
    created_at: str
    updated_at: str
    owns_worktree: bool


@dataclass(frozen=True)
class WorkflowHandoff:
    source_workflow_id: str
    target_workflow_id: str
    summary: str
    created_at: str


@dataclass(frozen=True)
class OrchestrationRun:
    root_manager_id: str
    status: str
    current_review_cycle: int
    max_review_cycles: int
    error: str | None
    coordinator_handle: str | None
    coordinator_released_at: str | None
    coordinator_release_reason: str | None
    coordinator_error_count: int
    coordinator_last_error: str | None
    coordinator_retry_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class RoleReadiness:
    id: str
    root_manager_id: str
    workflow_id: str
    role: str
    attempt: int
    summary: str
    outcome: str | None
    blocked_reason: str | None
    artifact_kind: str
    artifact_sha256: str
    artifact_content: str
    created_at: str
    consumed_at: str | None


_ROLE_READINESS_VIEW_COLUMNS = (
    "id, root_manager_id, workflow_id, role, attempt, summary, outcome, blocked_reason, "
    "artifact_kind, artifact_sha256, created_at, consumed_at"
)


@dataclass(frozen=True)
class RoleReadinessView:
    """Read-only readiness facts for `workflow status`, without artifact bodies."""

    id: str
    root_manager_id: str
    workflow_id: str
    role: str
    attempt: int
    summary: str
    outcome: str | None
    blocked_reason: str | None
    artifact_kind: str
    artifact_sha256: str
    created_at: str
    consumed_at: str | None


class AdapterReferenceConflict(ValueError):
    """An unreconciled workflow already owns an adapter reference."""


class LifecycleOperationConflict(ValueError):
    """Another process currently owns this workflow's lifecycle callback."""

    def __init__(self, message: str, *, kind: str = "") -> None:
        super().__init__(message)
        self.kind = kind


def _require_recorded_successor_handoff(
    connection: sqlite3.Connection, workflow: WorkflowRecord
) -> None:
    """Fail closed when an orchestrated predecessor completes without a successor handoff."""
    if workflow.mode != WorkflowMode.ORCHESTRATED:
        return
    if workflow.role == WorkflowRole.MANAGER:
        parent_id = workflow.id
        successor_role = WorkflowRole.WORKER.value
    elif workflow.role == WorkflowRole.WORKER:
        parent_id = workflow.parent_id
        successor_role = WorkflowRole.REVIEWER.value
    else:
        return
    if parent_id is None:
        raise ValueError("orchestrated predecessor has no successor role for a completion handoff")
    successor = connection.execute(
        "SELECT id FROM workflows WHERE parent_id = ? AND role = ? ORDER BY slot, id",
        (parent_id, successor_role),
    ).fetchall()
    if not successor:
        raise ValueError("orchestrated predecessor has no successor role for a completion handoff")
    for row in successor:
        recorded = connection.execute(
            "SELECT 1 FROM workflow_handoffs WHERE source_workflow_id = ? AND target_workflow_id = ?",
            (workflow.id, str(row["id"])),
        ).fetchone()
        if recorded is None:
            raise ValueError(
                "orchestrated predecessor cannot complete before a successor handoff is recorded"
            )


@dataclass(frozen=True)
class LifecycleClaim:
    workflow: WorkflowRecord
    adapter_reference: str | None
    affected_workflow_ids: tuple[str, ...] = ()


class WorkflowStore:
    """Durable workflow records with explicit, adapter-neutral transitions."""

    def __init__(self, state_dir: Path) -> None:
        self.path = prepare_database(state_dir)

    def _connect(self) -> sqlite3.Connection:
        return connect_database(self.path)

    @staticmethod
    def _record(row: sqlite3.Row) -> WorkflowRecord:
        values = dict(row)
        values["mode"] = WorkflowMode(values["mode"])
        values["role"] = WorkflowRole(values["role"])
        values["status"] = WorkflowStatus(values["status"])
        values["owns_worktree"] = bool(values["owns_worktree"])
        values["queue_observer_enabled"] = bool(values["queue_observer_enabled"])
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
        issue_url: str | None = None,
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
                        f"SELECT {_WORKFLOW_SELECT} FROM {_WORKFLOW_FROM} WHERE id = ?",
                        (parent_id,),
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
                        adapter_reference, worktree_path, terminal_handle, error, queue_observer_enabled,
                        issue_url, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'requested', NULL, NULL, NULL, NULL, 0, ?, ?, ?)
                    """,
                    (
                        workflow_id,
                        str(repository.resolve()),
                        mode.value,
                        name,
                        objective,
                        role.value,
                        parent_id,
                        issue_url,
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
        self,
        repository: Path,
        name: str,
        objective: str,
        *,
        issue_url: str | None = None,
        max_review_cycles: int = 3,
        reviewer_count: int = 1,
    ) -> list[WorkflowRecord]:
        """Create the complete fixed role plan atomically, before any Orca side effect."""
        if not name.strip() or not objective.strip():
            raise ValueError("workflow name and objective are required")
        if type(max_review_cycles) is not int or max_review_cycles < 1:
            raise ValueError("max_review_cycles must be a positive integer")
        if type(reviewer_count) is not int or reviewer_count < 1:
            raise ValueError("reviewer_count must be a positive integer")
        objective = objective.strip()
        now = _now()
        manager_id, worker_id = str(uuid.uuid4()), str(uuid.uuid4())
        reviewer_ids = tuple(str(uuid.uuid4()) for _ in range(reviewer_count))
        rows = [
            (manager_id, name, WorkflowRole.MANAGER, None, 0),
            (
                worker_id,
                _role_plan_name(name, WorkflowRole.WORKER, worker_id),
                WorkflowRole.WORKER,
                manager_id,
                0,
            ),
            *[
                (
                    reviewer_id,
                    _role_plan_name(name, WorkflowRole.REVIEWER, reviewer_id, slot),
                    WorkflowRole.REVIEWER,
                    manager_id,
                    slot,
                )
                for slot, reviewer_id in enumerate(reviewer_ids)
            ],
        ]
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                for workflow_id, workflow_name, role, parent_id, slot in rows:
                    connection.execute(
                        """
                        INSERT INTO workflows(id, repository, mode, name, objective, role, slot, parent_id,
                        status, adapter_reference, worktree_path, terminal_handle, error,
                        queue_observer_enabled, issue_url, created_at, updated_at)
                        VALUES (?, ?, 'orchestrated', ?, ?, ?, ?, ?, 'requested', NULL, NULL, NULL, NULL, 0, ?, ?, ?)
                        """,
                        (
                            workflow_id,
                            str(repository.resolve()),
                            workflow_name,
                            objective,
                            role.value,
                            slot,
                            parent_id,
                            issue_url,
                            now,
                            now,
                        ),
                    )
                connection.execute(
                    _INSERT_ORCHESTRATION_RUN, (manager_id, max_review_cycles, now, now)
                )
                connection.executemany(
                    "INSERT INTO step_dependencies(predecessor_step_id, successor_step_id, kind) "
                    "VALUES (?, ?, 'completion')",
                    (
                        (manager_id, worker_id),
                        *((worker_id, reviewer_id) for reviewer_id in reviewer_ids),
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
        issue_url: str | None = None,
        max_review_cycles: int = 3,
        reviewer_count: int = 1,
    ) -> list[WorkflowRecord]:
        """Atomically create a root plan and claim its root for external startup."""
        try:
            mode = WorkflowMode(mode)
        except ValueError as exc:
            raise ValueError("workflow mode is invalid") from exc
        if not name.strip() or not objective.strip():
            raise ValueError("workflow name and objective are required")
        if type(max_review_cycles) is not int or max_review_cycles < 1:
            raise ValueError("max_review_cycles must be a positive integer")
        if type(reviewer_count) is not int or reviewer_count < 1:
            raise ValueError("reviewer_count must be a positive integer")
        objective = objective.strip()
        repository_text = str(repository.resolve())
        now = _now()
        manager_id = str(uuid.uuid4())
        if mode == WorkflowMode.ORCHESTRATED:
            worker_id = str(uuid.uuid4())
            reviewer_ids = tuple(str(uuid.uuid4()) for _ in range(reviewer_count))
            rows = [
                (manager_id, name, WorkflowRole.MANAGER, None, WorkflowStatus.STARTING, 0),
                (
                    worker_id,
                    _role_plan_name(name, WorkflowRole.WORKER, worker_id),
                    WorkflowRole.WORKER,
                    manager_id,
                    WorkflowStatus.REQUESTED,
                    0,
                ),
                *[
                    (
                        reviewer_id,
                        _role_plan_name(name, WorkflowRole.REVIEWER, reviewer_id, slot),
                        WorkflowRole.REVIEWER,
                        manager_id,
                        WorkflowStatus.REQUESTED,
                        slot,
                    )
                    for slot, reviewer_id in enumerate(reviewer_ids)
                ],
            ]
        else:
            reviewer_ids = ()
            worker_id = ""
            rows = [
                (
                    manager_id,
                    name,
                    WorkflowRole.SINGLE,
                    None,
                    WorkflowStatus.STARTING,
                    0,
                ),
            ]
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
                for workflow_id, workflow_name, role, parent_id, status, slot in rows:
                    connection.execute(
                        """
                        INSERT INTO workflows(
                            id, repository, mode, name, objective, role, slot, parent_id, status,
                            adapter_reference, worktree_path, terminal_handle, error,
                            queue_observer_enabled, issue_url, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, 0, ?, ?, ?)
                        """,
                        (
                            workflow_id,
                            repository_text,
                            mode.value,
                            workflow_name,
                            objective,
                            role.value,
                            slot,
                            parent_id,
                            status.value,
                            issue_url,
                            now,
                            now,
                        ),
                    )
                if mode == WorkflowMode.ORCHESTRATED:
                    connection.execute(
                        _INSERT_ORCHESTRATION_RUN, (manager_id, max_review_cycles, now, now)
                    )
                    connection.executemany(
                        "INSERT INTO step_dependencies(predecessor_step_id, successor_step_id, kind) "
                        "VALUES (?, ?, 'completion')",
                        (
                            (manager_id, worker_id),
                            *((worker_id, reviewer_id) for reviewer_id in reviewer_ids),
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
        owns_worktree: bool = True,
        implementation_repository: str | None = None,
        start_sha: str | None = None,
        runtime_repository_id: str | None = None,
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
                    SET adapter_reference = ?, worktree_path = ?, terminal_handle = ?,
                        implementation_repository = ?, start_sha = ?, runtime_repository_id = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        adapter_reference,
                        worktree_path,
                        terminal_handle,
                        implementation_repository,
                        start_sha,
                        runtime_repository_id,
                        _now(),
                        workflow_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise AdapterReferenceConflict("Orca worktree reference is already owned") from exc
            connection.execute(
                "INSERT OR IGNORE INTO owned_terminals VALUES (?, ?, 'agent', ?)",
                (workflow_id, terminal_handle, _now()),
            )
            connection.execute(
                "INSERT INTO worktrees(orca_id, path, ownership, presence) "
                "VALUES (?, ?, 'managed', 'unknown') "
                "ON CONFLICT(orca_id) DO UPDATE SET path=excluded.path, ownership='managed'",
                (adapter_reference, worktree_path),
            )
            connection.execute(
                "INSERT OR IGNORE INTO step_worktrees VALUES (?, ?, 'primary')",
                (workflow_id, adapter_reference),
            )
            connection.execute(
                """
                INSERT INTO workflow_worktree_ownership(workflow_id, owns_worktree)
                VALUES (?, ?)
                ON CONFLICT(workflow_id) DO UPDATE SET owns_worktree = excluded.owns_worktree
                """,
                (workflow_id, int(bool(owns_worktree))),
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
            connection.execute(
                "INSERT INTO worktrees(orca_id, path, ownership, presence) "
                "VALUES (?, ?, 'managed', 'unknown') "
                "ON CONFLICT(orca_id) DO UPDATE SET path=excluded.path, ownership='managed'",
                (adapter_reference, worktree_path),
            )
            connection.execute(
                "INSERT OR IGNORE INTO step_worktrees VALUES (?, ?, 'primary')",
                (workflow_id, adapter_reference),
            )
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
            if kind == "observer":
                existing_observer = connection.execute(
                    "SELECT handle FROM owned_terminals WHERE workflow_id = ? AND kind = 'observer'",
                    (workflow_id,),
                ).fetchone()
                if existing_observer is not None and existing_observer["handle"] != handle:
                    raise LifecycleOperationConflict(
                        "queue observer is already owned by this workflow", kind="observer"
                    )
            connection.execute(
                "INSERT OR IGNORE INTO owned_terminals VALUES (?, ?, ?, ?)",
                (workflow_id, handle, kind, _now()),
            )
            if kind == "observer":
                connection.execute(
                    "UPDATE workflows SET queue_observer_enabled = 1, updated_at = ? WHERE id = ?",
                    (_now(), workflow_id),
                )

    def record_agent_run(
        self,
        workflow_id: str,
        agent: str,
        terminal_handle: str | None,
        *,
        model: str | None = None,
    ) -> str:
        if not agent.strip():
            raise ValueError("agent identifier is required")
        run_id = str(uuid.uuid4())
        with self._connect() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM workflow_steps WHERE id=?", (workflow_id,)
                ).fetchone()
                is None
            ):
                raise ValueError("workflow step was not found")
            connection.execute(
                "UPDATE agent_runs SET status='superseded', ended_at=? "
                "WHERE step_id=? AND status='running'",
                (_now(), workflow_id),
            )
            connection.execute(
                "INSERT INTO agent_runs(id, step_id, agent, model, terminal_handle, status, "
                "started_at) VALUES (?, ?, ?, ?, ?, 'running', ?)",
                (run_id, workflow_id, agent.strip(), model, terminal_handle, _now()),
            )
        return run_id

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
                "DELETE FROM owned_terminals WHERE workflow_id = ? AND kind = 'observer'",
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

    def owned_terminal_handles(self, workflow_id: str, *, kind: str | None = None) -> list[str]:
        with self._connect() as connection:
            if kind is None:
                rows = connection.execute(
                    "SELECT handle FROM owned_terminals WHERE workflow_id = ? ORDER BY created_at, handle",
                    (workflow_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT handle FROM owned_terminals
                    WHERE workflow_id = ? AND kind = ?
                    ORDER BY created_at, handle
                    """,
                    (workflow_id, kind),
                ).fetchall()
        return [str(row["handle"]) for row in rows]

    def set_queue_observer_enabled(self, workflow_id: str, enabled: bool) -> WorkflowRecord:
        """Update the observer policy for an existing running workflow."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE workflows
                SET queue_observer_enabled = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (int(enabled), _now(), workflow_id),
            )
            if updated.rowcount != 1:
                raise ValueError("queue observer policy requires a running workflow")
        return self.get(workflow_id)

    def detach_observers_for_unavailable_agent(
        self, workflow_id: str, expected_agent_handle: str
    ) -> list[str]:
        """Forget observers only when their original running agent is still current."""
        if not expected_agent_handle:
            return []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, terminal_handle FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
            if (
                row is None
                or row["status"] != WorkflowStatus.RUNNING.value
                or row["terminal_handle"] != expected_agent_handle
            ):
                return []
            handles = [
                str(item["handle"])
                for item in connection.execute(
                    "SELECT handle FROM owned_terminals WHERE workflow_id = ? AND kind = 'observer'",
                    (workflow_id,),
                ).fetchall()
            ]
            connection.execute(
                "DELETE FROM owned_terminals WHERE workflow_id = ? AND kind = 'observer'",
                (workflow_id,),
            )
            if handles:
                connection.execute(
                    """
                    UPDATE workflows
                    SET observer_last_handle = ?, observer_stopped_at = ?,
                        observer_stop_reason = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        handles[-1],
                        _now(),
                        "owner_terminal_unavailable",
                        _now(),
                        workflow_id,
                    ),
                )
        return handles

    def forget_owned_terminals(
        self,
        workflow_id: str,
        handles: tuple[str, ...] | list[str],
        *,
        kind: str,
        reason: str,
    ) -> None:
        """Drop recorded ownership for specific terminals without finishing the workflow."""
        wanted = tuple(handle for handle in handles if handle)
        if not wanted or not kind or not reason:
            return
        placeholders = ", ".join("?" for _ in wanted)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                f"""
                DELETE FROM owned_terminals
                WHERE workflow_id = ? AND kind = ? AND handle IN ({placeholders})
                """,
                (workflow_id, kind, *wanted),
            )
            if kind == "observer":
                connection.execute(
                    """
                    UPDATE workflows
                    SET observer_last_handle = ?, observer_stopped_at = ?,
                        observer_stop_reason = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (wanted[-1], _now(), reason, _now(), workflow_id),
                )

    def record_observer_termination(self, workflow_id: str, handle: str, reason: str) -> None:
        """Persist a Flybridge-observed observer termination for status diagnostics."""
        if not handle or not reason:
            return
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE workflows
                SET observer_last_handle = ?, observer_stopped_at = ?,
                    observer_stop_reason = ?, updated_at = ?
                WHERE id = ?
                """,
                (handle, _now(), reason, _now(), workflow_id),
            )

    def find_by_adapter_reference(self, adapter_reference: str) -> WorkflowRecord:
        """Return the active Flybridge workflow that owns an Orca worktree."""
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_WORKFLOW_SELECT} FROM {_WORKFLOW_FROM} WHERE adapter_reference = ? "
                "AND status IN ('starting', 'running')",
                (adapter_reference,),
            ).fetchone()
        if row is None:
            raise ValueError("active Flybridge workflow for Orca worktree was not found")
        return self._record(row)

    def find_unreconciled_by_adapter_reference(
        self, adapter_reference: str
    ) -> WorkflowRecord | None:
        """Return the unreconciled owner of an adapter reference, if any."""
        if not adapter_reference:
            raise ValueError("adapter reference is required")
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_WORKFLOW_SELECT} FROM {_WORKFLOW_FROM} WHERE adapter_reference = ? "
                "AND external_reconciled_at IS NULL ORDER BY updated_at DESC, id",
                (adapter_reference,),
            ).fetchone()
        return None if row is None else self._record(row)

    def find_prior_unreconciled_by_name(
        self, name: str, *, exclude_workflow_id: str
    ) -> WorkflowRecord | None:
        """Return prior durable ownership that would reuse a workflow's Orca name."""
        if not name or not exclude_workflow_id:
            raise ValueError("workflow name and excluded workflow identifier are required")
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_WORKFLOW_SELECT} FROM {_WORKFLOW_FROM} WHERE name = ? AND id != ? "
                "AND adapter_reference IS NOT NULL AND external_reconciled_at IS NULL "
                "ORDER BY updated_at DESC, id",
                (name, exclude_workflow_id),
            ).fetchone()
        return None if row is None else self._record(row)

    def update_objective(self, workflow_id: str, objective: str) -> WorkflowRecord:
        """Replace the recorded objective of a running workflow."""
        text = objective.strip()
        if not text:
            raise ValueError("workflow objective is required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
            if row is None:
                raise ValueError("workflow was not found")
            if row["status"] != WorkflowStatus.RUNNING.value:
                raise ValueError("objective can only be replaced for a running workflow")
            updated = connection.execute(
                "UPDATE workflows SET objective = ?, updated_at = ? "
                "WHERE id = ? AND status = 'running'",
                (text, _now(), workflow_id),
            )
            if updated.rowcount != 1:
                raise ValueError("workflow objective changed while it was being replaced")
        return self.get(workflow_id)

    def set_objective_source(
        self, run_id: str, source_path: Path | None, sha256: str | None
    ) -> None:
        """Snapshot the resolved objective source without changing the expanded objective."""
        if (source_path is None) != (sha256 is None):
            raise ValueError("objective source path and digest must be provided together")
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE workflow_runs SET objective_source_path=?, objective_sha256=?, "
                "updated_at=? WHERE id=?",
                (
                    None if source_path is None else str(source_path.expanduser().resolve()),
                    sha256,
                    _now(),
                    run_id,
                ),
            )
            if updated.rowcount != 1:
                raise ValueError("workflow run was not found")

    def list_workflows(
        self, *, statuses: tuple[str | WorkflowStatus, ...] | None = None
    ) -> list[WorkflowRecord]:
        """Return workflow records, optionally filtered by terminal/active status."""
        query = f"SELECT {_WORKFLOW_SELECT} FROM {_WORKFLOW_FROM}"
        parameters: tuple[object, ...] = ()
        if statuses:
            unique = tuple(dict.fromkeys(WorkflowStatus(status) for status in statuses))
            placeholders = ", ".join("?" for _ in unique)
            query += f" WHERE status IN ({placeholders})"
            parameters = tuple(status.value for status in unique)
        query += " ORDER BY updated_at, id"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._record(row) for row in rows]

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
                f"SELECT {_WORKFLOW_SELECT} FROM {_WORKFLOW_FROM} WHERE id = ?", (workflow_id,)
            ).fetchone()
        if row is None:
            raise ValueError("workflow was not found")
        return self._record(row)

    def get_by_run_or_step_id(self, identifier: str) -> WorkflowRecord:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_WORKFLOW_SELECT} FROM {_WORKFLOW_FROM} "
                "WHERE workflows.id=? OR (workflows.run_id=? AND workflows.parent_id IS NULL) "
                "ORDER BY CASE WHEN workflows.id=? THEN 0 ELSE 1 END LIMIT 1",
                (identifier, identifier, identifier),
            ).fetchone()
        if row is None:
            raise ValueError("workflow run or step was not found")
        return self._record(row)

    def get_run(self, run_id: str) -> dict[str, object]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM workflow_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("workflow run was not found")
        return dict(row)

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
                f"SELECT {_WORKFLOW_SELECT} FROM {_WORKFLOW_FROM} WHERE id = ?", (workflow_id,)
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
                "UPDATE workflows SET updated_at = ?, activated_at = ? "
                "WHERE id = ? AND status = 'running'",
                (_now(), _now(), workflow_id),
            )
            if updated.rowcount != 1:
                raise ValueError("only a running workflow can be marked resumed")
        return self.get(workflow_id)

    def has_unconsumed_readiness(self, workflow_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM workflow_role_readiness "
                "WHERE workflow_id = ? AND consumed_at IS NULL LIMIT 1",
                (workflow_id,),
            ).fetchone()
        return row is not None

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
                f"SELECT {_WORKFLOW_SELECT} FROM {_WORKFLOW_FROM} WHERE id = ?", (workflow_id,)
            ).fetchone()
            if row is None:
                raise ValueError("workflow was not found")
            current = WorkflowStatus(row["status"])
            if target not in TRANSITIONS[current]:
                raise ValueError(f"cannot transition workflow from {current} to {target}")
            now = _now()
            activated_at = now if target == WorkflowStatus.RUNNING else None
            connection.execute(
                """
                UPDATE workflows
                SET status = ?, adapter_reference = COALESCE(?, adapter_reference),
                    error = COALESCE(?, error), updated_at = ?,
                    activated_at = COALESCE(?, activated_at)
                WHERE id = ?
                """,
                (target.value, adapter_reference, error, now, activated_at, workflow_id),
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
            now = _now()
            updated = connection.execute(
                "UPDATE workflows SET status = 'running', activated_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'starting'",
                (now, now, workflow_id),
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
                f"SELECT {_WORKFLOW_SELECT} FROM {_WORKFLOW_FROM} WHERE id = ?", (workflow_id,)
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
            if valid_running and target == WorkflowStatus.COMPLETED:
                _require_recorded_successor_handoff(connection, workflow)
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
                _cancel_queue_owners(connection, affected_ids)
                connection.execute(
                    "UPDATE agent_runs SET status='cancelled', ended_at=? "
                    "WHERE step_id=? AND status='running'",
                    (now, workflow_id),
                )
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
                f"SELECT {_WORKFLOW_SELECT} FROM {_WORKFLOW_FROM} WHERE id = ?", (workflow_id,)
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
                f"SELECT {_WORKFLOW_SELECT} FROM {_WORKFLOW_FROM} WHERE id = ?", (workflow_id,)
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
            if target in {WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}:
                successor_rows = connection.execute(
                    "WITH RECURSIVE successors(id) AS ("
                    "SELECT successor_step_id FROM step_dependencies WHERE predecessor_step_id=? "
                    "UNION SELECT d.successor_step_id FROM step_dependencies d "
                    "JOIN successors s ON d.predecessor_step_id=s.id) "
                    "SELECT s.id AS successor_step_id FROM successors s "
                    "JOIN workflows w ON w.id=s.id WHERE w.status='requested'",
                    (workflow_id,),
                ).fetchall()
                successor_ids = [str(item["successor_step_id"]) for item in successor_rows]
                if successor_ids:
                    placeholders = ", ".join("?" for _ in successor_ids)
                    connection.execute(
                        f"UPDATE workflows SET status='cancelled', updated_at=? "
                        f"WHERE id IN ({placeholders})",
                        (_now(), *successor_ids),
                    )
                _cancel_queue_owners(connection, [workflow_id, *successor_ids])
            connection.execute(
                "UPDATE agent_runs SET status=?, ended_at=? WHERE step_id=? AND status='running'",
                (target.value, _now(), workflow_id),
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
                _cancel_queue_owners(connection, ids)
        return [self.get(workflow_id) for workflow_id in ids]

    def retry_failed(self, workflow_id: str) -> WorkflowRecord:
        """Return a reconciled failed role to its original requested state."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT {_WORKFLOW_SELECT} FROM {_WORKFLOW_FROM} WHERE id = ?", (workflow_id,)
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
                    external_reconciled_at = NULL, implementation_repository = NULL,
                    start_sha = NULL, runtime_repository_id = NULL,
                    updated_at = ?
                WHERE id = ? AND status = 'failed'
                """,
                (_now(), workflow_id),
            )
            if updated.rowcount != 1:
                raise ValueError("failed workflow changed while it was being retried")
            connection.execute("DELETE FROM owned_terminals WHERE workflow_id = ?", (workflow_id,))
            connection.execute(
                "DELETE FROM workflow_worktree_ownership WHERE workflow_id = ?", (workflow_id,)
            )
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
                f"SELECT {_WORKFLOW_SELECT} FROM {_WORKFLOW_FROM} WHERE status IN ('requested', 'starting', 'running') "
                "ORDER BY created_at, id"
            ).fetchall()
        return [self._record(row) for row in rows]

    def children(self, parent_id: str) -> list[WorkflowRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {_WORKFLOW_SELECT} FROM {_WORKFLOW_FROM} WHERE parent_id = ? "
                "ORDER BY CASE role WHEN 'worker' THEN 1 WHEN 'reviewer' THEN 2 ELSE 3 END, slot, id",
                (parent_id,),
            ).fetchall()
        return [self._record(row) for row in rows]

    def registered_source_urls(self, run_id: str) -> tuple[str, ...]:
        """Return issue and pull-request URLs already linked to a persisted run."""
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM workflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if exists is None:
                raise ValueError("workflow run was not found")
            rows = connection.execute(
                """
                SELECT er.canonical_url
                FROM workflow_refs wr
                JOIN external_refs er ON er.id = wr.external_ref_id
                WHERE wr.run_id = ?
                    AND wr.provenance = 'explicit'
                    AND wr.relation IN ('primary', 'related')
                    AND er.kind IN ('issue', 'pull_request')
                ORDER BY
                    CASE wr.relation
                        WHEN 'primary' THEN 0
                        WHEN 'candidate-primary' THEN 1
                        ELSE 2
                    END,
                    CASE er.kind WHEN 'issue' THEN 0 ELSE 1 END,
                    er.canonical_url
                """,
                (run_id,),
            ).fetchall()
        return tuple(str(row["canonical_url"]) for row in rows)

    def record_handoff(self, source_id: str, target_id: str, summary: str) -> WorkflowHandoff:
        if not summary.strip() or "\n" in summary:
            raise ValueError("handoff summary must be a non-empty single line")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            source_row = connection.execute(
                f"SELECT {_WORKFLOW_SELECT} FROM {_WORKFLOW_FROM} WHERE id = ?", (source_id,)
            ).fetchone()
            target_row = connection.execute(
                f"SELECT {_WORKFLOW_SELECT} FROM {_WORKFLOW_FROM} WHERE id = ?", (target_id,)
            ).fetchone()
            if source_row is None or target_row is None:
                raise ValueError("handoff source or target workflow was not found")
            source, target = self._record(source_row), self._record(target_row)
            if (
                source.status
                not in {
                    WorkflowStatus.RUNNING,
                    WorkflowStatus.COMPLETED,
                }
                or target.status != WorkflowStatus.REQUESTED
            ):
                raise ValueError(
                    "handoff requires a running or completed source and a requested target"
                )
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
            if existing_source and not worker_to_reviewer:
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

    def record_or_update_handoff(
        self, source_id: str, target_id: str, summary: str
    ) -> WorkflowHandoff:
        """Record a fixed role edge, updating its summary for a later review cycle."""
        try:
            return self.record_handoff(source_id, target_id, summary)
        except ValueError as exc:
            if "already" not in str(exc):
                raise
        if not summary.strip() or "\n" in summary:
            raise ValueError("handoff summary must be a non-empty single line")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT target_workflow_id FROM workflow_handoffs "
                "WHERE source_workflow_id = ? AND target_workflow_id = ?",
                (source_id, target_id),
            ).fetchone()
            if row is None:
                raise ValueError("handoff source already targets another workflow")
            connection.execute(
                "UPDATE workflow_handoffs SET summary = ?, created_at = ? "
                "WHERE source_workflow_id = ? AND target_workflow_id = ?",
                (summary.strip(), _now(), source_id, target_id),
            )
        return self.handoff_for(target_id)

    def orchestration_run(self, root_manager_id: str) -> OrchestrationRun:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM orchestration_runs WHERE root_manager_id = ?",
                (root_manager_id,),
            ).fetchone()
        if row is None:
            raise ValueError("orchestration run was not found")
        return OrchestrationRun(**dict(row))

    def record_role_readiness(
        self,
        workflow_id: str,
        *,
        summary: str,
        artifact_kind: str,
        artifact_sha256: str,
        artifact_content: str,
        outcome: str | None = None,
        blocked_reason: str | None = None,
    ) -> RoleReadiness:
        """Record an idempotent, durable agent signal without changing terminal ownership."""
        if not summary.strip() or "\n" in summary:
            raise ValueError("readiness summary must be a non-empty single line")
        workflow = self.get(workflow_id)
        if workflow.mode != WorkflowMode.ORCHESTRATED or workflow.role == WorkflowRole.SINGLE:
            raise ValueError("role readiness requires an orchestrated role")
        if workflow.status != WorkflowStatus.RUNNING:
            raise ValueError("only a running role can report readiness")
        root_id = workflow.id if workflow.role == WorkflowRole.MANAGER else workflow.parent_id
        if root_id is None:
            raise ValueError("orchestrated role has no root manager")
        run = self.orchestration_run(root_id)
        attempt = 1 if workflow.role == WorkflowRole.MANAGER else run.current_review_cycle
        if blocked_reason is not None:
            blocked_reason = blocked_reason.strip()
            if not blocked_reason or "\n" in blocked_reason:
                raise ValueError("blocked reason must be a non-empty single line")
            if outcome is not None:
                raise ValueError("blocked readiness cannot include a reviewer outcome")
        elif workflow.role == WorkflowRole.REVIEWER:
            if outcome not in {"approved", "changes-requested"}:
                raise ValueError("reviewer readiness requires approved or changes-requested")
        elif outcome is not None:
            raise ValueError("only reviewer readiness accepts an outcome")
        now = _now()
        readiness_id = str(uuid.uuid4())
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO workflow_role_readiness(
                        id, root_manager_id, workflow_id, role, attempt, summary, outcome, blocked_reason,
                        artifact_kind, artifact_sha256, artifact_content, created_at, consumed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        readiness_id,
                        root_id,
                        workflow.id,
                        workflow.role.value,
                        attempt,
                        summary.strip(),
                        outcome,
                        blocked_reason,
                        artifact_kind,
                        artifact_sha256,
                        artifact_content,
                        now,
                    ),
                )
        except sqlite3.IntegrityError:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM workflow_role_readiness "
                    "WHERE workflow_id = ? AND role = ? AND attempt = ?",
                    (workflow.id, workflow.role.value, attempt),
                ).fetchone()
            if row is None:
                raise
            existing = RoleReadiness(**dict(row))
            if (
                existing.workflow_id != workflow.id
                or existing.summary != summary.strip()
                or existing.outcome != outcome
                or existing.blocked_reason != blocked_reason
                or existing.artifact_kind != artifact_kind
                or existing.artifact_sha256 != artifact_sha256
                or existing.artifact_content != artifact_content
            ):
                raise ValueError("role readiness was already recorded with different details")
            return existing
        return self.role_readiness_for(workflow.id, attempt)

    def role_readiness_for(self, workflow_id: str, attempt: int) -> RoleReadiness:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_role_readiness WHERE workflow_id = ? AND attempt = ?",
                (workflow_id, attempt),
            ).fetchone()
        if row is None:
            raise ValueError("role readiness was not found")
        return RoleReadiness(**dict(row))

    def role_readiness_history(self, root_manager_id: str) -> list[RoleReadinessView]:
        """Return every readiness signal for a run, oldest first, without artifact bodies."""
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM workflows WHERE id = ?", (root_manager_id,)
            ).fetchone()
            if exists is None:
                raise ValueError("workflow was not found")
            rows = connection.execute(
                f"""
                SELECT {_ROLE_READINESS_VIEW_COLUMNS}
                FROM workflow_role_readiness
                WHERE root_manager_id = ?
                ORDER BY attempt, created_at, id
                """,
                (root_manager_id,),
            ).fetchall()
        return [RoleReadinessView(**dict(row)) for row in rows]

    def role_readiness_list(
        self, root_manager_id: str, role: str | WorkflowRole, attempt: int
    ) -> list[RoleReadiness]:
        role = WorkflowRole(role)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM workflow_role_readiness "
                "WHERE root_manager_id = ? AND role = ? AND attempt = ? "
                "ORDER BY created_at, id",
                (root_manager_id, role.value, attempt),
            ).fetchall()
        return [RoleReadiness(**dict(row)) for row in rows]

    def role_readiness(
        self, root_manager_id: str, role: str | WorkflowRole, attempt: int
    ) -> RoleReadiness:
        role = WorkflowRole(role)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_role_readiness "
                "WHERE root_manager_id = ? AND role = ? AND attempt = ?",
                (root_manager_id, role.value, attempt),
            ).fetchone()
        if row is None:
            raise ValueError("role readiness was not found")
        return RoleReadiness(**dict(row))

    def pending_blocker(self, root_manager_id: str) -> RoleReadiness | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM workflow_role_readiness
                WHERE root_manager_id = ? AND blocked_reason IS NOT NULL
                    AND consumed_at IS NULL
                ORDER BY created_at, id
                LIMIT 1
                """,
                (root_manager_id,),
            ).fetchone()
        return None if row is None else RoleReadiness(**dict(row))

    def blocker_for_run(self, root_manager_id: str) -> RoleReadiness | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM workflow_role_readiness
                WHERE root_manager_id = ? AND blocked_reason IS NOT NULL
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (root_manager_id,),
            ).fetchone()
        return None if row is None else RoleReadiness(**dict(row))

    def consume_role_readiness(self, readiness_id: str) -> bool:
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE workflow_role_readiness SET consumed_at = ? "
                "WHERE id = ? AND consumed_at IS NULL",
                (_now(), readiness_id),
            )
        return updated.rowcount == 1

    def release_coordinator_ownership(self, root_manager_id: str, reason: str) -> tuple[str, ...]:
        """Release ownership once; later replay must preserve the first terminal reason."""
        if not reason.strip():
            raise ValueError("coordinator release reason is required")
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT handle FROM owned_terminals WHERE workflow_id = ? AND kind = 'coordinator' "
                "ORDER BY created_at, handle",
                (root_manager_id,),
            ).fetchall()
            handles = tuple(str(row["handle"]) for row in rows)
            run = connection.execute(
                "SELECT coordinator_handle FROM orchestration_runs WHERE root_manager_id = ?",
                (root_manager_id,),
            ).fetchone()
            if run is None:
                raise ValueError("orchestration run was not found")
            released = connection.execute(
                "SELECT coordinator_released_at FROM orchestration_runs WHERE root_manager_id = ?",
                (root_manager_id,),
            ).fetchone()
            if released is not None and released["coordinator_released_at"] is not None:
                return ()
            recorded = handles[-1] if handles else run["coordinator_handle"]
            connection.execute(
                "DELETE FROM owned_terminals WHERE workflow_id = ? AND kind = 'coordinator'",
                (root_manager_id,),
            )
            connection.execute(
                """
                UPDATE orchestration_runs
                SET coordinator_handle = ?, coordinator_released_at = ?,
                    coordinator_release_reason = ?, updated_at = ?
                WHERE root_manager_id = ?
                """,
                (recorded, now, reason.strip(), now, root_manager_id),
            )
        return handles

    def finalize_reviewer_outcome(
        self,
        readiness_id: str,
        *,
        status: str,
        error: str | None,
        coordinator_reason: str,
        extra_readiness_ids: tuple[str, ...] = (),
    ) -> OrchestrationRun:
        """Atomically consume reviewer readiness, finish the run, and release coordinator ownership."""
        if status not in {"completed", "blocked"}:
            raise ValueError("reviewer terminal outcome must be completed or blocked")
        if not coordinator_reason.strip():
            raise ValueError("coordinator release reason is required")
        readiness_ids = tuple(dict.fromkeys((readiness_id, *extra_readiness_ids)))
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = []
            for item_id in readiness_ids:
                readiness = connection.execute(
                    "SELECT id, root_manager_id, role, outcome, consumed_at "
                    "FROM workflow_role_readiness WHERE id = ?",
                    (item_id,),
                ).fetchone()
                if readiness is None or readiness["role"] != WorkflowRole.REVIEWER.value:
                    raise ValueError("reviewer readiness was not found")
                rows.append(readiness)
            outcomes = {row["outcome"] for row in rows}
            if status == "completed":
                if outcomes != {"approved"}:
                    raise ValueError("reviewer readiness does not match the terminal outcome")
            elif "changes-requested" not in outcomes:
                raise ValueError("reviewer readiness does not match the terminal outcome")
            root_id = str(rows[0]["root_manager_id"])
            if any(str(row["root_manager_id"]) != root_id for row in rows):
                raise ValueError("reviewer readiness was not found")
            run = connection.execute(
                "SELECT status, coordinator_handle, coordinator_released_at "
                "FROM orchestration_runs WHERE root_manager_id = ?",
                (root_id,),
            ).fetchone()
            if run is None:
                raise ValueError("orchestration run was not found")
            consumed = [row for row in rows if row["consumed_at"] is not None]
            pending = [row for row in rows if row["consumed_at"] is None]
            if consumed and run["status"] != status:
                raise ValueError("reviewer readiness was consumed by another outcome")
            if pending:
                handles = connection.execute(
                    "SELECT handle FROM owned_terminals "
                    "WHERE workflow_id = ? AND kind = 'coordinator' ORDER BY created_at, handle",
                    (root_id,),
                ).fetchall()
                recorded = str(handles[-1]["handle"]) if handles else run["coordinator_handle"]
                for row in pending:
                    connection.execute(
                        "UPDATE workflow_role_readiness SET consumed_at = ? "
                        "WHERE id = ? AND consumed_at IS NULL",
                        (now, str(row["id"])),
                    )
                updated = connection.execute(
                    """
                    UPDATE orchestration_runs
                    SET status = ?, error = ?, coordinator_handle = ?,
                        coordinator_released_at = COALESCE(coordinator_released_at, ?),
                        coordinator_release_reason = COALESCE(coordinator_release_reason, ?),
                        coordinator_retry_at = NULL, updated_at = ?
                    WHERE root_manager_id = ? AND status = 'running'
                    """,
                    (status, error, recorded, now, coordinator_reason.strip(), now, root_id),
                )
                if updated.rowcount != 1:
                    raise ValueError("orchestration outcome changed concurrently")
                connection.execute(
                    "DELETE FROM owned_terminals WHERE workflow_id = ? AND kind = 'coordinator'",
                    (root_id,),
                )
        return self.orchestration_run(root_id)

    def finalize_blocked_readiness(self, readiness_id: str) -> OrchestrationRun:
        """Atomically consume a declared blocker and terminate its aggregate run."""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            readiness = connection.execute(
                """
                SELECT root_manager_id, workflow_id, role, blocked_reason, consumed_at
                FROM workflow_role_readiness WHERE id = ?
                """,
                (readiness_id,),
            ).fetchone()
            if readiness is None or readiness["blocked_reason"] is None:
                raise ValueError("blocked readiness was not found")
            root_id = str(readiness["root_manager_id"])
            run = connection.execute(
                "SELECT status, coordinator_handle FROM orchestration_runs "
                "WHERE root_manager_id = ?",
                (root_id,),
            ).fetchone()
            if run is None:
                raise ValueError("orchestration run was not found")
            if readiness["consumed_at"] is not None:
                if run["status"] != "blocked":
                    raise ValueError("blocked readiness was consumed by another outcome")
            else:
                role_state = connection.execute(
                    "SELECT status FROM workflows WHERE id = ?",
                    (str(readiness["workflow_id"]),),
                ).fetchone()
                if role_state is None or role_state["status"] != WorkflowStatus.CANCELLED.value:
                    raise ValueError("blocked role must be cancelled before run finalization")
                handles = connection.execute(
                    "SELECT handle FROM owned_terminals "
                    "WHERE workflow_id = ? AND kind = 'coordinator' "
                    "ORDER BY created_at, handle",
                    (root_id,),
                ).fetchall()
                recorded = str(handles[-1]["handle"]) if handles else run["coordinator_handle"]
                consumed = connection.execute(
                    "UPDATE workflow_role_readiness SET consumed_at = ? "
                    "WHERE id = ? AND consumed_at IS NULL",
                    (now, readiness_id),
                )
                if consumed.rowcount != 1:
                    raise ValueError("blocked readiness changed concurrently")
                error = f"{readiness['role']} blocked: {readiness['blocked_reason']}"
                updated = connection.execute(
                    """
                    UPDATE orchestration_runs
                    SET status = 'blocked', error = ?, coordinator_handle = ?,
                        coordinator_released_at = COALESCE(coordinator_released_at, ?),
                        coordinator_release_reason = COALESCE(
                            coordinator_release_reason, 'orchestration_blocked'
                        ),
                        coordinator_retry_at = NULL, updated_at = ?
                    WHERE root_manager_id = ? AND status = 'running'
                    """,
                    (error, recorded, now, now, root_id),
                )
                if updated.rowcount != 1:
                    raise ValueError("orchestration outcome changed concurrently")
                connection.execute(
                    "DELETE FROM owned_terminals WHERE workflow_id = ? AND kind = 'coordinator'",
                    (root_id,),
                )
        return self.orchestration_run(root_id)

    def record_coordinator_error(
        self,
        root_manager_id: str,
        detail: str,
        *,
        max_errors: int,
        initial_delay_seconds: float,
        max_delay_seconds: float,
    ) -> OrchestrationRun:
        """Persist bounded exponential retry state so a replacement supervisor can resume it."""
        if max_errors < 1 or initial_delay_seconds <= 0 or max_delay_seconds <= 0:
            raise ValueError("coordinator retry policy is invalid")
        now = datetime.now(UTC)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, coordinator_error_count FROM orchestration_runs "
                "WHERE root_manager_id = ?",
                (root_manager_id,),
            ).fetchone()
            if row is None:
                raise ValueError("orchestration run was not found")
            if row["status"] == "running":
                count = int(row["coordinator_error_count"]) + 1
            else:
                count = 0
            if row["status"] == "running" and count >= max_errors:
                connection.execute(
                    """
                    UPDATE orchestration_runs
                    SET status = 'blocked', error = ?, coordinator_error_count = ?,
                        coordinator_last_error = ?, coordinator_retry_at = NULL, updated_at = ?
                    WHERE root_manager_id = ? AND status = 'running'
                    """,
                    (
                        f"coordinator retry limit reached after {count} errors: {detail}",
                        count,
                        detail,
                        now.isoformat(),
                        root_manager_id,
                    ),
                )
            elif row["status"] == "running":
                delay = min(initial_delay_seconds * (2 ** (count - 1)), max_delay_seconds)
                retry_at = (now + timedelta(seconds=delay)).isoformat()
                connection.execute(
                    """
                    UPDATE orchestration_runs
                    SET coordinator_error_count = ?, coordinator_last_error = ?,
                        coordinator_retry_at = ?, updated_at = ?
                    WHERE root_manager_id = ? AND status = 'running'
                    """,
                    (count, detail, retry_at, now.isoformat(), root_manager_id),
                )
        return self.orchestration_run(root_manager_id)

    def clear_coordinator_errors(self, root_manager_id: str) -> OrchestrationRun:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE orchestration_runs
                SET coordinator_error_count = 0, coordinator_last_error = NULL,
                    coordinator_retry_at = NULL, updated_at = ?
                WHERE root_manager_id = ? AND status = 'running'
                """,
                (_now(), root_manager_id),
            )
        return self.orchestration_run(root_manager_id)

    def retry_orchestration(
        self, root_manager_id: str, *, expected_updated_at: str
    ) -> OrchestrationRun:
        """CAS-reset a recoverable terminal run while preserving role progress."""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM orchestration_runs WHERE root_manager_id = ?",
                (root_manager_id,),
            ).fetchone()
            if run is None:
                raise ValueError("orchestration run was not found")
            if run["status"] == "completed":
                raise ValueError("completed orchestration cannot be retried")
            if run["status"] not in {"blocked", "failed"}:
                raise ValueError("only a blocked or failed orchestration can be retried")

            pending_blocker = connection.execute(
                """
                SELECT 1 FROM workflow_role_readiness
                WHERE root_manager_id = ? AND blocked_reason IS NOT NULL
                    AND consumed_at IS NULL
                LIMIT 1
                """,
                (root_manager_id,),
            ).fetchone()
            consumed_blocker = connection.execute(
                """
                SELECT 1 FROM workflow_role_readiness
                WHERE root_manager_id = ? AND blocked_reason IS NOT NULL
                    AND consumed_at IS NOT NULL
                LIMIT 1
                """,
                (root_manager_id,),
            ).fetchone()
            exhausted_review = connection.execute(
                """
                SELECT 1 FROM workflow_role_readiness
                WHERE root_manager_id = ? AND role = 'reviewer'
                    AND attempt = ? AND outcome = 'changes-requested'
                    AND consumed_at IS NOT NULL
                LIMIT 1
                """,
                (root_manager_id, int(run["current_review_cycle"])),
            ).fetchone()
            pending_readiness = connection.execute(
                """
                SELECT 1 FROM workflow_role_readiness
                WHERE root_manager_id = ? AND consumed_at IS NULL
                LIMIT 1
                """,
                (root_manager_id,),
            ).fetchone()
            live_role = connection.execute(
                """
                SELECT 1 FROM workflows
                WHERE (id = ? OR parent_id = ?) AND status = 'running'
                LIMIT 1
                """,
                (root_manager_id, root_manager_id),
            ).fetchone()

            if pending_blocker is None:
                if consumed_blocker is not None:
                    raise ValueError("consumed declared blocker cannot be retried")
                if (
                    int(run["current_review_cycle"]) >= int(run["max_review_cycles"])
                    and exhausted_review is not None
                ):
                    raise ValueError("maximum review cycle outcome cannot be retried")
                if pending_readiness is None and live_role is None:
                    raise ValueError("durable role state does not permit coordinator replay")

            updated = connection.execute(
                """
                UPDATE orchestration_runs
                SET status = 'running', error = NULL, coordinator_handle = NULL,
                    coordinator_released_at = NULL, coordinator_release_reason = NULL,
                    coordinator_error_count = 0, coordinator_last_error = NULL,
                    coordinator_retry_at = NULL, updated_at = ?
                WHERE root_manager_id = ? AND status IN ('blocked', 'failed')
                    AND updated_at = ?
                """,
                (now, root_manager_id, expected_updated_at),
            )
            if updated.rowcount != 1:
                raise ValueError("orchestration run changed during coordinator retry")
            connection.execute(
                "DELETE FROM owned_terminals WHERE workflow_id = ? AND kind = 'coordinator'",
                (root_manager_id,),
            )
        return self.orchestration_run(root_manager_id)

    def set_orchestration_outcome(
        self, root_manager_id: str, status: str, *, error: str | None = None
    ) -> OrchestrationRun:
        if status not in {"running", "completed", "blocked", "failed"}:
            raise ValueError("invalid orchestration run status")
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE orchestration_runs SET status = ?, error = ?, updated_at = ? "
                "WHERE root_manager_id = ?",
                (status, error, _now(), root_manager_id),
            )
        if updated.rowcount != 1:
            raise ValueError("orchestration run was not found")
        return self.orchestration_run(root_manager_id)

    def prepare_next_review_cycle(
        self,
        root_manager_id: str,
        *,
        worker_start_sha: str,
        reviewer_readiness_id: str,
    ) -> OrchestrationRun:
        """Reopen fixed worker/reviewer records after the old reviewer was removed.

        The worker keeps its worktree and repository identity across cycles, so its
        start SHA must advance to the reviewed commit. Otherwise the next cycle's
        readiness gate would still be satisfied by the previous cycle's commits.
        """
        if not worker_start_sha.strip():
            raise ValueError("next review cycle requires the reviewed worker start SHA")
        if not reviewer_readiness_id:
            raise ValueError("next review cycle requires reviewer readiness")
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT status, current_review_cycle, max_review_cycles "
                "FROM orchestration_runs WHERE root_manager_id = ?",
                (root_manager_id,),
            ).fetchone()
            readiness_rows = connection.execute(
                """
                SELECT id, root_manager_id, workflow_id, role, attempt, outcome, consumed_at
                FROM workflow_role_readiness
                WHERE root_manager_id = ? AND role = 'reviewer' AND attempt = ?
                """,
                (root_manager_id, int(run["current_review_cycle"]) if run is not None else 0),
            ).fetchall()
            roles = connection.execute(
                "SELECT id, role, status, external_reconciled_at FROM workflows "
                "WHERE parent_id = ? AND role IN ('worker', 'reviewer') ORDER BY slot, id",
                (root_manager_id,),
            ).fetchall()
            worker = next((row for row in roles if row["role"] == WorkflowRole.WORKER.value), None)
            reviewers = [row for row in roles if row["role"] == WorkflowRole.REVIEWER.value]
            if run is None:
                raise ValueError("orchestration run was not found")
            if run["status"] != "running":
                raise ValueError("only a running orchestration can open another review cycle")
            if int(run["current_review_cycle"]) >= int(run["max_review_cycles"]):
                raise ValueError("maximum review cycles reached")
            if (
                worker is None
                or not reviewers
                or worker["status"] != WorkflowStatus.COMPLETED.value
                or any(row["status"] != WorkflowStatus.COMPLETED.value for row in reviewers)
                or any(row["external_reconciled_at"] is None for row in reviewers)
            ):
                raise ValueError("role records are not ready for another review cycle")
            by_id = {str(row["id"]): row for row in readiness_rows}
            trigger = by_id.get(reviewer_readiness_id)
            if (
                trigger is None
                or trigger["root_manager_id"] != root_manager_id
                or trigger["outcome"] != "changes-requested"
                or trigger["consumed_at"] is not None
            ):
                raise ValueError("reviewer readiness cannot open the next review cycle")
            if len(readiness_rows) != len(reviewers) or any(
                row["consumed_at"] is not None
                or int(row["attempt"]) != int(run["current_review_cycle"])
                for row in readiness_rows
            ):
                raise ValueError("reviewer readiness cannot open the next review cycle")
            worker_updated = connection.execute(
                "UPDATE workflows SET status = 'running', error = NULL, cleanup_error = NULL, "
                "terminal_handle = NULL, start_sha = ?, updated_at = ? "
                "WHERE id = ? AND status = 'completed'",
                (worker_start_sha.strip(), now, str(worker["id"])),
            )
            if worker_updated.rowcount != 1:
                raise ValueError("worker record changed while the next review cycle opened")
            for reviewer in reviewers:
                reviewer_updated = connection.execute(
                    """
                    UPDATE workflows
                    SET status = 'requested', adapter_reference = NULL, worktree_path = NULL,
                        terminal_handle = NULL, error = NULL, cleanup_error = NULL,
                        external_reconciled_at = NULL, implementation_repository = NULL,
                        start_sha = NULL, runtime_repository_id = NULL, updated_at = ?
                    WHERE id = ? AND status = 'completed'
                    """,
                    (now, str(reviewer["id"])),
                )
                if reviewer_updated.rowcount != 1:
                    raise ValueError("reviewer record changed while the next review cycle opened")
                connection.execute(
                    "DELETE FROM owned_terminals WHERE workflow_id = ?", (str(reviewer["id"]),)
                )
                connection.execute(
                    "DELETE FROM workflow_worktree_ownership WHERE workflow_id = ?",
                    (str(reviewer["id"]),),
                )
            run_updated = connection.execute(
                "UPDATE orchestration_runs SET current_review_cycle = current_review_cycle + 1, "
                "error = NULL, updated_at = ? WHERE root_manager_id = ? AND status = 'running' "
                "AND current_review_cycle = ?",
                (now, root_manager_id, int(run["current_review_cycle"])),
            )
            if run_updated.rowcount != 1:
                raise ValueError("review cycle changed concurrently")
            for row in readiness_rows:
                consumed = connection.execute(
                    "UPDATE workflow_role_readiness SET consumed_at = ? "
                    "WHERE id = ? AND consumed_at IS NULL",
                    (now, str(row["id"])),
                )
                if consumed.rowcount != 1:
                    raise ValueError("reviewer readiness changed concurrently")
        return self.orchestration_run(root_manager_id)

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
                SELECT {_WORKFLOW_SELECT} FROM {_WORKFLOW_FROM}
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
                SELECT {_WORKFLOW_SELECT} FROM {_WORKFLOW_FROM}
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
            connection.execute(
                "UPDATE worktrees SET ownership='reconciled' WHERE orca_id="
                "(SELECT adapter_reference FROM workflows WHERE id=?)",
                (workflow_id,),
            )
        return self.get(workflow_id)

    def unreconciled_terminal(self) -> list[WorkflowRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {_WORKFLOW_SELECT} FROM {_WORKFLOW_FROM} "
                "WHERE (status IN ('failed', 'cancelled') "
                "OR (status = 'completed' AND cleanup_error IS NOT NULL)) "
                "AND adapter_reference IS NOT NULL AND external_reconciled_at IS NULL "
                "ORDER BY updated_at, id"
            ).fetchall()
        return [self._record(row) for row in rows]

    def roots_needing_child_retirement(self) -> list[WorkflowRecord]:
        """Finished orchestrated roots that still own an unreconciled child worktree."""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT {_WORKFLOW_SELECT} FROM {_WORKFLOW_FROM}
                WHERE workflows.parent_id IS NULL AND workflows.mode = 'orchestrated'
                    AND workflows.id IN (
                        SELECT child.parent_id FROM workflows AS child
                        LEFT JOIN workflow_worktree_ownership AS child_own
                            ON child_own.workflow_id = child.id
                        WHERE child.parent_id IS NOT NULL
                            AND child.adapter_reference IS NOT NULL
                            AND child.external_reconciled_at IS NULL
                            AND COALESCE(child_own.owns_worktree, 1) = 1
                    )
                    AND (
                        workflows.status IN ('completed', 'failed', 'cancelled')
                        OR EXISTS (
                            SELECT 1 FROM orchestration_runs AS run
                            WHERE run.root_manager_id = workflows.id
                                AND run.status IN ('completed', 'blocked', 'failed')
                        )
                    )
                ORDER BY workflows.updated_at, workflows.id
                """
            ).fetchall()
        return [self._record(row) for row in rows]
