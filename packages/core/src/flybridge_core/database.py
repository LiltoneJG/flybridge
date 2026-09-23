from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

from .storage import configure_sqlite_connection, prepare_private_database

SCHEMA_VERSION = 2
DATABASE_FILENAME = "flybridge.sqlite3"
LEGACY_FILENAMES = ("workflows.sqlite3", "queue.sqlite3")

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE workflow_runs (
        id TEXT PRIMARY KEY,
        repository TEXT NOT NULL,
        mode TEXT NOT NULL CHECK(mode IN ('single', 'orchestrated')),
        name TEXT NOT NULL,
        objective TEXT NOT NULL,
        objective_source_path TEXT,
        objective_sha256 TEXT,
        status TEXT NOT NULL CHECK(status IN (
            'requested', 'starting', 'running', 'completed', 'failed', 'cancelled'
        )),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE workflows (
        id TEXT PRIMARY KEY,
        run_id TEXT REFERENCES workflow_runs(id),
        repository TEXT NOT NULL,
        mode TEXT NOT NULL CHECK(mode IN ('single', 'orchestrated')),
        name TEXT NOT NULL,
        objective TEXT NOT NULL,
        role TEXT NOT NULL,
        slot INTEGER NOT NULL DEFAULT 0 CHECK(slot >= 0),
        attempt INTEGER NOT NULL DEFAULT 1 CHECK(attempt >= 1),
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
        updated_at TEXT NOT NULL,
        queue_observer_enabled INTEGER NOT NULL DEFAULT 0 CHECK(queue_observer_enabled IN (0, 1)),
        observer_last_handle TEXT,
        observer_stopped_at TEXT,
        observer_stop_reason TEXT,
        issue_url TEXT,
        implementation_repository TEXT,
        start_sha TEXT,
        runtime_repository_id TEXT,
        activated_at TEXT
    )
    """,
    """
    CREATE TABLE workflow_steps (
        id TEXT PRIMARY KEY REFERENCES workflows(id) ON DELETE CASCADE,
        run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
        role TEXT NOT NULL,
        slot INTEGER NOT NULL DEFAULT 0 CHECK(slot >= 0),
        attempt INTEGER NOT NULL DEFAULT 1 CHECK(attempt >= 1),
        status TEXT NOT NULL,
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(run_id, role, slot, attempt)
    )
    """,
    """
    CREATE TABLE step_dependencies (
        predecessor_step_id TEXT NOT NULL REFERENCES workflow_steps(id) ON DELETE CASCADE,
        successor_step_id TEXT NOT NULL REFERENCES workflow_steps(id) ON DELETE CASCADE,
        kind TEXT NOT NULL DEFAULT 'completion',
        PRIMARY KEY(predecessor_step_id, successor_step_id, kind)
    )
    """,
    """
    CREATE TABLE agent_runs (
        id TEXT PRIMARY KEY,
        step_id TEXT NOT NULL REFERENCES workflow_steps(id) ON DELETE CASCADE,
        agent TEXT NOT NULL,
        model TEXT,
        terminal_handle TEXT,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        ended_at TEXT
    )
    """,
    """
    CREATE TABLE worktrees (
        orca_id TEXT PRIMARY KEY,
        path TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        ownership TEXT NOT NULL CHECK(ownership IN ('managed', 'unmanaged', 'reconciled')),
        presence TEXT NOT NULL CHECK(presence IN ('present', 'missing', 'unknown')),
        observed_status TEXT,
        comment TEXT NOT NULL DEFAULT '',
        missing_observations INTEGER NOT NULL DEFAULT 0,
        first_missing_at TEXT,
        last_observed_at TEXT,
        observation_error TEXT
    )
    """,
    """
    CREATE TABLE step_worktrees (
        step_id TEXT NOT NULL REFERENCES workflow_steps(id) ON DELETE CASCADE,
        orca_id TEXT NOT NULL REFERENCES worktrees(orca_id) ON DELETE CASCADE,
        relation TEXT NOT NULL DEFAULT 'primary',
        PRIMARY KEY(step_id, orca_id, relation)
    )
    """,
    """
    CREATE TABLE repositories (
        id TEXT PRIMARY KEY,
        github_node_id TEXT,
        canonical_name TEXT,
        local_identity TEXT,
        UNIQUE(github_node_id),
        UNIQUE(canonical_name),
        CHECK(canonical_name IS NOT NULL OR local_identity IS NOT NULL)
    )
    """,
    """
    CREATE TABLE repository_checkouts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        orca_id TEXT NOT NULL REFERENCES worktrees(orca_id) ON DELETE CASCADE,
        repository_id TEXT REFERENCES repositories(id),
        relation TEXT NOT NULL CHECK(relation IN ('main', 'submodule', 'related')),
        path TEXT NOT NULL,
        branch TEXT NOT NULL,
        commit_sha TEXT NOT NULL,
        dirty INTEGER NOT NULL CHECK(dirty IN (0, 1)),
        ahead INTEGER,
        behind INTEGER,
        stale INTEGER NOT NULL DEFAULT 0 CHECK(stale IN (0, 1)),
        observation_error TEXT,
        observed_at TEXT NOT NULL,
        UNIQUE(orca_id, relation, path)
    )
    """,
    """
    CREATE TABLE external_refs (
        id TEXT PRIMARY KEY,
        repository_id TEXT REFERENCES repositories(id),
        github_node_id TEXT,
        kind TEXT NOT NULL CHECK(kind IN ('issue', 'pull_request')),
        canonical_url TEXT NOT NULL UNIQUE,
        number INTEGER NOT NULL,
        observed_state TEXT,
        stale INTEGER NOT NULL DEFAULT 0 CHECK(stale IN (0, 1)),
        observation_error TEXT,
        observed_at TEXT,
        UNIQUE(github_node_id)
    )
    """,
    """
    CREATE TABLE workflow_refs (
        run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
        external_ref_id TEXT NOT NULL REFERENCES external_refs(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK(kind IN ('issue', 'pull_request')),
        relation TEXT NOT NULL CHECK(relation IN ('primary', 'related', 'candidate-primary', 'candidate')),
        provenance TEXT NOT NULL CHECK(provenance IN ('explicit', 'inferred')),
        source TEXT,
        PRIMARY KEY(run_id, external_ref_id, relation, provenance)
    )
    """,
    """
    CREATE UNIQUE INDEX one_explicit_primary_ref
        ON workflow_refs(run_id, kind, provenance)
        WHERE relation = 'primary' AND provenance = 'explicit'
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
    CREATE TABLE workflow_worktree_ownership (
        workflow_id TEXT PRIMARY KEY REFERENCES workflows(id),
        owns_worktree INTEGER NOT NULL CHECK(owns_worktree IN (0, 1))
    )
    """,
    """
    CREATE TABLE workflow_handoffs (
        source_workflow_id TEXT NOT NULL REFERENCES workflows(id),
        target_workflow_id TEXT NOT NULL REFERENCES workflows(id),
        summary TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(source_workflow_id, target_workflow_id)
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
    """
    CREATE TABLE workflow_artifacts (
        root_manager_id TEXT NOT NULL REFERENCES workflows(id),
        workflow_id TEXT NOT NULL REFERENCES workflows(id),
        kind TEXT NOT NULL CHECK(kind IN ('plan', 'verification', 'review')),
        relative_path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        byte_size INTEGER NOT NULL CHECK(byte_size > 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (root_manager_id, workflow_id, kind)
    )
    """,
    """
    CREATE TABLE orchestration_runs (
        root_manager_id TEXT PRIMARY KEY REFERENCES workflows(id),
        status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'blocked', 'failed')),
        current_review_cycle INTEGER NOT NULL DEFAULT 1 CHECK(current_review_cycle > 0),
        max_review_cycles INTEGER NOT NULL CHECK(max_review_cycles > 0),
        error TEXT,
        coordinator_handle TEXT,
        coordinator_released_at TEXT,
        coordinator_release_reason TEXT,
        coordinator_error_count INTEGER NOT NULL DEFAULT 0 CHECK(coordinator_error_count >= 0),
        coordinator_last_error TEXT,
        coordinator_retry_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE workflow_role_readiness (
        id TEXT PRIMARY KEY,
        root_manager_id TEXT NOT NULL REFERENCES workflows(id),
        workflow_id TEXT NOT NULL REFERENCES workflows(id),
        role TEXT NOT NULL CHECK(role IN ('manager', 'worker', 'reviewer')),
        attempt INTEGER NOT NULL CHECK(attempt > 0),
        summary TEXT NOT NULL,
        outcome TEXT CHECK(outcome IN ('approved', 'changes-requested')),
        blocked_reason TEXT,
        artifact_kind TEXT NOT NULL CHECK(artifact_kind IN ('plan', 'verification', 'review')),
        artifact_sha256 TEXT NOT NULL,
        artifact_content TEXT NOT NULL,
        created_at TEXT NOT NULL,
        consumed_at TEXT,
        UNIQUE(root_manager_id, workflow_id, role, attempt)
    )
    """,
    """
    CREATE TABLE operations (
        id TEXT PRIMARY KEY,
        step_id TEXT REFERENCES workflow_steps(id),
        kind TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'succeeded', 'failed')),
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        started_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE queue_requests (
        id TEXT PRIMARY KEY, resource TEXT NOT NULL, owner TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('waiting','leased','released','cancelled'))
    )
    """,
    """
    CREATE TABLE queue_events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
        resource TEXT NOT NULL, request_id TEXT NOT NULL, event TEXT NOT NULL
    )
    """,
    "CREATE VIEW requests AS SELECT id, resource, owner, created_at, updated_at, status FROM queue_requests",
    "CREATE VIEW events AS SELECT sequence, created_at, resource, request_id, event FROM queue_events",
    """
    CREATE TRIGGER requests_insert INSTEAD OF INSERT ON requests
    BEGIN
        INSERT INTO queue_requests VALUES (
            NEW.id, NEW.resource, NEW.owner, NEW.created_at, NEW.updated_at, NEW.status
        );
    END
    """,
    """
    CREATE TRIGGER requests_update INSTEAD OF UPDATE ON requests
    BEGIN
        UPDATE queue_requests SET resource=NEW.resource, owner=NEW.owner,
            created_at=NEW.created_at, updated_at=NEW.updated_at, status=NEW.status
        WHERE id=OLD.id;
    END
    """,
    """
    CREATE TRIGGER requests_delete INSTEAD OF DELETE ON requests
    BEGIN DELETE FROM queue_requests WHERE id=OLD.id; END
    """,
    """
    CREATE TABLE observation_scans (
        id TEXT PRIMARY KEY,
        scope TEXT NOT NULL,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        success INTEGER NOT NULL DEFAULT 0 CHECK(success IN (0, 1)),
        truncated INTEGER NOT NULL DEFAULT 0 CHECK(truncated IN (0, 1)),
        error TEXT
    )
    """,
    """
    CREATE TABLE state_events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        event TEXT NOT NULL,
        payload TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX workflow_active_name ON workflows(name) WHERE status IN ('requested', 'starting', 'running')",
    "CREATE UNIQUE INDEX workflow_owned_adapter_reference ON workflows(adapter_reference) WHERE adapter_reference IS NOT NULL AND external_reconciled_at IS NULL",
    "CREATE INDEX workflow_run_steps ON workflows(run_id, role, slot, attempt)",
    "CREATE INDEX workflow_manager_role ON workflows(parent_id, role)",
    "CREATE INDEX workflow_handoff_target ON workflow_handoffs(target_workflow_id)",
    "CREATE UNIQUE INDEX workflow_artifact_path ON workflow_artifacts(relative_path)",
    "CREATE INDEX request_order ON queue_requests(resource, status, created_at, id)",
    "CREATE UNIQUE INDEX one_resource_lease ON queue_requests(resource) WHERE status = 'leased'",
    "CREATE UNIQUE INDEX one_active_owner_request ON queue_requests(resource, owner) WHERE status IN ('waiting', 'leased')",
    "CREATE INDEX state_event_order ON state_events(sequence)",
    """
    CREATE TRIGGER normalize_workflow_insert AFTER INSERT ON workflows
    BEGIN
        INSERT OR IGNORE INTO workflow_runs(
            id, repository, mode, name, objective, status, created_at, updated_at
        )
        SELECT NEW.id, NEW.repository, NEW.mode, NEW.name, NEW.objective, NEW.status,
               NEW.created_at, NEW.updated_at
        WHERE NEW.parent_id IS NULL;
        UPDATE workflows
        SET run_id = COALESCE(
            (SELECT run_id FROM workflows WHERE id = NEW.parent_id), NEW.id
        )
        WHERE id = NEW.id;
        INSERT INTO workflow_steps(
            id, run_id, role, slot, attempt, status, error, created_at, updated_at
        )
        SELECT id, run_id, role, slot, attempt, status, error, created_at, updated_at
        FROM workflows WHERE id = NEW.id;
    END
    """,
    """
    CREATE TRIGGER normalize_workflow_update AFTER UPDATE OF status, error, updated_at, objective
    ON workflows
    BEGIN
        UPDATE workflow_steps
        SET status = NEW.status, error = NEW.error, updated_at = NEW.updated_at
        WHERE id = NEW.id;
        UPDATE workflow_runs
        SET objective = CASE WHEN NEW.parent_id IS NULL THEN NEW.objective ELSE objective END,
            status = CASE
                WHEN EXISTS(SELECT 1 FROM workflow_steps WHERE run_id=NEW.run_id AND status='failed')
                    THEN 'failed'
                WHEN EXISTS(SELECT 1 FROM workflow_steps WHERE run_id=NEW.run_id AND status='cancelled')
                    THEN 'cancelled'
                WHEN NOT EXISTS(SELECT 1 FROM workflow_steps WHERE run_id=NEW.run_id AND status!='completed')
                    THEN 'completed'
                WHEN EXISTS(SELECT 1 FROM workflow_steps WHERE run_id=NEW.run_id AND status='running')
                    THEN 'running'
                WHEN EXISTS(SELECT 1 FROM workflow_steps WHERE run_id=NEW.run_id AND status='starting')
                    THEN 'starting'
                WHEN EXISTS(SELECT 1 FROM workflow_steps WHERE run_id=NEW.run_id AND status='completed')
                    THEN 'running'
                ELSE 'requested'
            END,
            updated_at = NEW.updated_at
        WHERE id = NEW.run_id;
    END
    """,
    """
    CREATE TRIGGER mirror_lifecycle_operation_insert
    AFTER INSERT ON workflow_lifecycle_operations
    BEGIN
        INSERT INTO operations(
            id, step_id, kind, idempotency_key, status, attempts, last_error,
            started_at, created_at, updated_at
        ) VALUES (
            NEW.workflow_id || ':' || NEW.kind, NEW.workflow_id, NEW.kind,
            NEW.workflow_id || ':' || NEW.kind || ':' || COALESCE(NEW.target, ''),
            'running', 1, NEW.error, NEW.created_at, NEW.created_at, NEW.updated_at
        )
        ON CONFLICT(id) DO UPDATE SET status='running', attempts=operations.attempts+1,
            last_error=NEW.error, started_at=NEW.updated_at, updated_at=NEW.updated_at;
    END
    """,
    """
    CREATE TRIGGER mirror_lifecycle_operation_update
    AFTER UPDATE ON workflow_lifecycle_operations
    BEGIN
        UPDATE operations SET last_error=NEW.error,
            status=CASE WHEN NEW.error IS NULL THEN 'running' ELSE 'failed' END,
            updated_at=NEW.updated_at
        WHERE id=NEW.workflow_id || ':' || NEW.kind;
    END
    """,
    """
    CREATE TRIGGER mirror_lifecycle_operation_delete
    AFTER DELETE ON workflow_lifecycle_operations
    BEGIN
        UPDATE operations SET status='succeeded', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
        WHERE id=OLD.workflow_id || ':' || OLD.kind;
    END
    """,
)


def _legacy_state(state_dir: Path) -> tuple[Path, ...]:
    found: list[Path] = []
    for name in LEGACY_FILENAMES:
        path = state_dir / name
        if path.is_symlink():
            raise OSError(f"legacy state database must not be a symbolic link: {path}")
        if path.exists() and path.stat().st_size > 0:
            found.append(path)
        elif path.exists():
            # An empty placeholder has no legacy history, but still receives private permissions.
            path.chmod(0o600)
    return tuple(found)


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=5)
    connection.row_factory = sqlite3.Row
    configure_sqlite_connection(connection, path, foreign_keys=True)
    return connection


def _install(connection: sqlite3.Connection, statements: Sequence[str]) -> None:
    for statement in statements:
        connection.execute(statement)


def _migrate(connection: sqlite3.Connection, version: int) -> int:
    """Upgrade a recognized prior schema in place. Unknown versions stay rejected."""
    if version == 1:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(workflows)").fetchall()
        }
        if "activated_at" not in columns:
            connection.execute("ALTER TABLE workflows ADD COLUMN activated_at TEXT")
        connection.execute("PRAGMA user_version = 2")
        return 2
    return version


def _normalized(value: object) -> str:
    return (
        " ".join(str(value).lower().split())
        .replace(" ,", ",")
        .replace(", ", ",")
        .replace("( ", "(")
        .replace(" )", ")")
    )


def _verify(connection: sqlite3.Connection, path: Path) -> None:
    with closing(sqlite3.connect(":memory:")) as expected:
        _install(expected, SCHEMA_STATEMENTS)
        definitions = expected.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
            "AND sql IS NOT NULL"
        ).fetchall()
    actual = {
        (str(row[0]), str(row[1])): _normalized(row[2])
        for row in connection.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
            "AND sql IS NOT NULL"
        )
    }
    for object_type, name, sql in definitions:
        key = (str(object_type), str(name))
        if key not in actual:
            raise RuntimeError(
                f"malformed Flybridge state schema: {object_type} {name} is missing; remove {path}"
            )
        if actual[key] != _normalized(sql):
            raise RuntimeError(
                f"malformed Flybridge state schema: definitions differ for {name}; remove {path}"
            )


def prepare_database(state_dir: Path) -> Path:
    legacy = _legacy_state(state_dir)
    if legacy:
        names = ", ".join(path.name for path in legacy)
        raise RuntimeError(
            f"legacy Flybridge state detected ({names}); archive or remove it before using "
            f"{DATABASE_FILENAME}; automatic migration is not supported"
        )
    path = prepare_private_database(state_dir, DATABASE_FILENAME)
    with closing(_connect(path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        objects = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone()
        if version == 0 and objects is None:
            _install(connection, SCHEMA_STATEMENTS)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            version = SCHEMA_VERSION
        if version < SCHEMA_VERSION:
            version = _migrate(connection, version)
        if version != SCHEMA_VERSION:
            raise RuntimeError(
                f"unsupported Flybridge state schema (found version {version}, expected "
                f"{SCHEMA_VERSION}); archive or remove {path}"
            )
        _verify(connection, path)
        connection.commit()
    return path


def connect_database(path: Path) -> sqlite3.Connection:
    return _connect(path)
