from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .database import connect_database, prepare_database

MARKER_RE = re.compile(r"<!--\s*flybridge:run=(?P<run>[0-9a-f-]+);step=(?P<step>[0-9a-f-]+)\s*-->")
GITHUB_REF_RE = re.compile(
    r"https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/"
    r"(?P<kind>issues|pull)/(?P<number>[1-9][0-9]*)/?",
    re.IGNORECASE,
)


def workflow_marker(run_id: str, step_id: str) -> str:
    return f"<!-- flybridge:run={run_id};step={step_id} -->"


def merge_workflow_marker(comment: str | None, run_id: str, step_id: str) -> str:
    marker = workflow_marker(run_id, step_id)
    text = (comment or "").strip()
    return marker if not text else f"{text}\n{marker}"


class ObservedWorktree(Protocol):
    worktree_id: str
    path: str
    name: str
    workspace_status: str
    comment: str


@dataclass(frozen=True)
class ReconcileSummary:
    scan_id: str
    dry_run: bool
    success: bool
    truncated: bool
    added: tuple[str, ...]
    updated: tuple[str, ...]
    attached_steps: tuple[str, ...]
    cancelled_steps: tuple[str, ...]
    errors: tuple[str, ...] = ()


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ReconcileStore:
    def __init__(self, state_dir: Path, *, event_retention: int = 10_000) -> None:
        self.path = prepare_database(state_dir)
        self.event_retention = event_retention

    def _event(
        self,
        connection: sqlite3.Connection,
        entity_type: str,
        entity_id: str,
        event: str,
        payload: dict,
    ) -> None:
        connection.execute(
            "INSERT INTO state_events(created_at, entity_type, entity_id, event, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (_now(), entity_type, entity_id, event, json.dumps(payload, sort_keys=True)),
        )
        connection.execute(
            "DELETE FROM state_events WHERE sequence <= "
            "(SELECT MAX(sequence) - ? FROM state_events)",
            (self.event_retention,),
        )

    @staticmethod
    def _inferred_ref(connection: sqlite3.Connection, run_id: str, url: str, source: str) -> None:
        match = GITHUB_REF_RE.fullmatch(url.rstrip("/"))
        if match is None:
            return
        repository = f"{match.group('owner')}/{match.group('repo')}"
        kind = "issue" if match.group("kind").lower() == "issues" else "pull_request"
        number = int(match.group("number"))
        canonical = (
            f"https://github.com/{repository}/{'issues' if kind == 'issue' else 'pull'}/{number}"
        )
        repository_id = ReconcileStore._repository_id(repository, None)
        ref_id = hashlib.sha256(f"{repository.lower()}:{kind}:{number}".encode()).hexdigest()
        connection.execute(
            "INSERT OR IGNORE INTO repositories(id, canonical_name) VALUES (?, ?)",
            (repository_id, repository),
        )
        connection.execute(
            "INSERT OR IGNORE INTO external_refs(id, repository_id, kind, canonical_url, number) "
            "VALUES (?, ?, ?, ?, ?)",
            (ref_id, repository_id, kind, canonical, number),
        )
        connection.execute(
            "INSERT OR IGNORE INTO workflow_refs("
            "run_id, external_ref_id, kind, relation, provenance, source) "
            "VALUES (?, ?, ?, 'candidate', 'inferred', ?)",
            (run_id, ref_id, kind, source),
        )

    @staticmethod
    def _queue_event(
        connection: sqlite3.Connection, resource: str, request_id: str, event: str
    ) -> None:
        connection.execute(
            "INSERT INTO queue_events(created_at, resource, request_id, event) VALUES (?, ?, ?, ?)",
            (_now(), resource, request_id, event),
        )

    def _cancel_queue_owner(self, connection: sqlite3.Connection, owner: str) -> None:
        rows = connection.execute(
            "SELECT id, resource, status FROM queue_requests WHERE owner = ? "
            "AND status IN ('waiting', 'leased') ORDER BY created_at, id",
            (owner,),
        ).fetchall()
        leased: set[str] = set()
        for row in rows:
            connection.execute(
                "UPDATE queue_requests SET status = 'cancelled', updated_at = ? WHERE id = ?",
                (_now(), row["id"]),
            )
            self._queue_event(connection, str(row["resource"]), str(row["id"]), "cancelled")
            if row["status"] == "leased":
                leased.add(str(row["resource"]))
        for resource in sorted(leased):
            waiting = connection.execute(
                "SELECT id FROM queue_requests WHERE resource = ? AND status = 'waiting' "
                "ORDER BY created_at, id LIMIT 1",
                (resource,),
            ).fetchone()
            if waiting:
                connection.execute(
                    "UPDATE queue_requests SET status = 'leased', updated_at = ? WHERE id = ?",
                    (_now(), waiting["id"]),
                )
                self._queue_event(connection, resource, str(waiting["id"]), "leased")

    def apply_orca_scan(
        self,
        worktrees: tuple[ObservedWorktree, ...],
        *,
        truncated: bool,
        dry_run: bool = False,
        missing_observations: int = 2,
        missing_grace_seconds: int = 300,
    ) -> ReconcileSummary:
        scan_id = str(uuid.uuid4())
        now = _now()
        added: list[str] = []
        updated: list[str] = []
        attached: list[str] = []
        cancelled: list[str] = []
        with connect_database(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = {
                str(row["orca_id"]): row
                for row in connection.execute("SELECT * FROM worktrees").fetchall()
            }
            observed_ids = {item.worktree_id for item in worktrees}
            for item in worktrees:
                old = existing.get(item.worktree_id)
                marker = MARKER_RE.search(item.comment or "")
                step = None
                if marker:
                    step = connection.execute(
                        "SELECT id, run_id, status, adapter_reference FROM workflows "
                        "WHERE id = ? AND run_id = ?",
                        (marker.group("step"), marker.group("run")),
                    ).fetchone()
                owner = connection.execute(
                    "SELECT id, run_id FROM workflows WHERE adapter_reference = ? "
                    "AND external_reconciled_at IS NULL",
                    (item.worktree_id,),
                ).fetchone()
                recoverable_step = bool(
                    step
                    and step["status"] == "starting"
                    and not step["adapter_reference"]
                    and owner is None
                )
                ownership = "managed" if owner or recoverable_step else "unmanaged"
                changed = (
                    old is None
                    or old["path"] != item.path
                    or old["observed_status"] != item.workspace_status
                    or old["comment"] != item.comment
                    or old["presence"] != "present"
                    or old["ownership"] != ownership
                )
                if old is None:
                    added.append(item.worktree_id)
                elif changed:
                    updated.append(item.worktree_id)
                if not dry_run:
                    connection.execute(
                        """
                        INSERT INTO worktrees(
                            orca_id, path, name, ownership, presence, observed_status, comment,
                            missing_observations, first_missing_at, last_observed_at, observation_error
                        ) VALUES (?, ?, ?, ?, 'present', ?, ?, 0, NULL, ?, NULL)
                        ON CONFLICT(orca_id) DO UPDATE SET
                            path=excluded.path, name=excluded.name,
                            ownership=CASE WHEN worktrees.ownership='reconciled'
                                THEN 'reconciled' ELSE excluded.ownership END,
                            presence='present', observed_status=excluded.observed_status,
                            comment=excluded.comment, missing_observations=0,
                            first_missing_at=NULL, last_observed_at=excluded.last_observed_at,
                            observation_error=NULL
                        """,
                        (
                            item.worktree_id,
                            item.path,
                            item.name,
                            ownership,
                            item.workspace_status,
                            item.comment,
                            now,
                        ),
                    )
                    if changed:
                        self._event(
                            connection,
                            "worktree",
                            item.worktree_id,
                            "observed",
                            {"path": item.path, "status": item.workspace_status},
                        )
                    if owner is not None:
                        connection.execute(
                            "UPDATE workflows SET worktree_path=?, updated_at=? WHERE id=? "
                            "AND worktree_path IS NOT ?",
                            (item.path, now, owner["id"], item.path),
                        )
                        connection.execute(
                            "INSERT OR IGNORE INTO step_worktrees VALUES (?, ?, 'primary')",
                            (owner["id"], item.worktree_id),
                        )
                    related_run_id = (
                        str(owner["run_id"])
                        if owner is not None
                        else str(step["run_id"])
                        if recoverable_step and step is not None
                        else None
                    )
                    if related_run_id is not None:
                        for match in GITHUB_REF_RE.finditer(item.comment or ""):
                            self._inferred_ref(
                                connection, related_run_id, match.group(0), "orca_comment"
                            )
                        linked_issue = getattr(item, "linked_issue", None)
                        project_id = str(getattr(item, "project_id", ""))
                        prefix, separator, repository = project_id.partition(":")
                        if (
                            isinstance(linked_issue, int)
                            and linked_issue > 0
                            and separator
                            and prefix.lower() == "github"
                            and "/" in repository
                        ):
                            self._inferred_ref(
                                connection,
                                related_run_id,
                                f"https://github.com/{repository}/issues/{linked_issue}",
                                "orca_linked_issue",
                            )
                if recoverable_step and step is not None:
                    attached.append(str(step["id"]))
                    if not dry_run:
                        connection.execute(
                            "UPDATE workflows SET adapter_reference=?, worktree_path=?, updated_at=? "
                            "WHERE id=? AND status='starting' AND adapter_reference IS NULL",
                            (item.worktree_id, item.path, now, step["id"]),
                        )
                        connection.execute(
                            "INSERT OR IGNORE INTO workflow_worktree_ownership VALUES (?, 1)",
                            (step["id"],),
                        )
                        connection.execute(
                            "INSERT OR IGNORE INTO step_worktrees VALUES (?, ?, 'primary')",
                            (step["id"], item.worktree_id),
                        )

            if not truncated:
                managed = connection.execute(
                    "SELECT w.*, f.id AS step_id, f.status AS step_status, f.run_id "
                    "FROM worktrees w JOIN workflows f ON f.adapter_reference=w.orca_id "
                    "WHERE w.ownership='managed' AND f.external_reconciled_at IS NULL"
                ).fetchall()
                for row in managed:
                    if row["orca_id"] in observed_ids:
                        continue
                    count = int(row["missing_observations"]) + 1
                    first = str(row["first_missing_at"] or now)
                    age = (
                        datetime.fromisoformat(now) - datetime.fromisoformat(first)
                    ).total_seconds()
                    should_cancel = (
                        row["step_status"] in {"starting", "running"}
                        and count >= missing_observations
                        and age >= missing_grace_seconds
                    )
                    if should_cancel:
                        cancelled.append(str(row["step_id"]))
                    if dry_run:
                        continue
                    connection.execute(
                        "UPDATE worktrees SET presence='missing', missing_observations=?, "
                        "first_missing_at=?, last_observed_at=? WHERE orca_id=?",
                        (count, first, now, row["orca_id"]),
                    )
                    if should_cancel:
                        reason = "orca_worktree_missing_after_reconcile"
                        connection.execute(
                            "UPDATE workflows SET status='cancelled', error=?, updated_at=?, "
                            "external_reconciled_at=? WHERE id=?",
                            (reason, now, now, row["step_id"]),
                        )
                        successors = connection.execute(
                            "WITH RECURSIVE successors(id) AS ("
                            "SELECT successor_step_id FROM step_dependencies "
                            "WHERE predecessor_step_id=? UNION "
                            "SELECT d.successor_step_id FROM step_dependencies d "
                            "JOIN successors s ON d.predecessor_step_id=s.id) "
                            "SELECT id AS successor_step_id FROM successors",
                            (row["step_id"],),
                        ).fetchall()
                        for successor in successors:
                            successor_id = str(successor["successor_step_id"])
                            connection.execute(
                                "UPDATE workflows SET status='cancelled', error=?, updated_at=? "
                                "WHERE id=? AND status='requested'",
                                (reason, now, successor_id),
                            )
                            self._cancel_queue_owner(connection, successor_id)
                        self._cancel_queue_owner(connection, str(row["step_id"]))
                        connection.execute(
                            "UPDATE worktrees SET ownership='reconciled' WHERE orca_id=?",
                            (row["orca_id"],),
                        )
                        self._event(
                            connection,
                            "workflow_step",
                            str(row["step_id"]),
                            "cancelled",
                            {"reason": reason},
                        )
            if not dry_run:
                connection.execute(
                    "INSERT INTO observation_scans VALUES (?, 'orca', ?, ?, 1, ?, NULL)",
                    (scan_id, now, now, int(truncated)),
                )
            else:
                connection.rollback()
        return ReconcileSummary(
            scan_id,
            dry_run,
            True,
            truncated,
            tuple(added),
            tuple(updated),
            tuple(attached),
            tuple(cancelled),
        )

    def record_failure(self, detail: str, *, dry_run: bool = False) -> ReconcileSummary:
        scan_id, now = str(uuid.uuid4()), _now()
        if not dry_run:
            with connect_database(self.path) as connection:
                connection.execute(
                    "UPDATE worktrees SET presence='unknown', observation_error=?",
                    (detail,),
                )
                connection.execute(
                    "INSERT INTO observation_scans VALUES (?, 'orca', ?, ?, 0, 0, ?)",
                    (scan_id, now, now, detail),
                )
        return ReconcileSummary(scan_id, dry_run, False, False, (), (), (), (), (detail,))

    def list_unmanaged_worktrees(self) -> tuple[tuple[str, str], ...]:
        with connect_database(self.path) as connection:
            rows = connection.execute(
                "SELECT orca_id, path FROM worktrees WHERE ownership != 'managed' ORDER BY orca_id"
            ).fetchall()
        return tuple((str(row["orca_id"]), str(row["path"])) for row in rows)

    def managed_orca_ids(self) -> frozenset[str]:
        with connect_database(self.path) as connection:
            rows = connection.execute(
                "SELECT orca_id FROM worktrees WHERE ownership = 'managed'"
            ).fetchall()
        return frozenset(str(row["orca_id"]) for row in rows)

    def delete_worktrees(
        self, orca_ids: Sequence[str], *, dry_run: bool = False
    ) -> tuple[str, ...]:
        unique = tuple(dict.fromkeys(orca_ids))
        if dry_run or not unique:
            return unique
        with connect_database(self.path) as connection:
            for orca_id in unique:
                connection.execute("DELETE FROM worktrees WHERE orca_id=?", (orca_id,))
                self._event(connection, "worktree", orca_id, "pruned", {"reason": "excluded"})
        return unique

    def resolve_run_id(self, identifier: str) -> str:
        token = identifier.strip()
        if not token:
            raise ValueError("workflow run or step identifier is required")
        with connect_database(self.path) as connection:
            if connection.execute("SELECT 1 FROM workflow_runs WHERE id=?", (token,)).fetchone():
                return token
            row = connection.execute("SELECT run_id FROM workflows WHERE id=?", (token,)).fetchone()
        if row is None:
            raise ValueError("workflow run or step was not found")
        return str(row["run_id"])

    def freshness(self) -> dict[str, object]:
        with connect_database(self.path) as connection:
            row = connection.execute(
                "SELECT completed_at, success, truncated, error FROM observation_scans "
                "WHERE scope='orca' ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return {"observed_at": None, "fresh": False, "error": None, "truncated": False}
        return {
            "observed_at": row["completed_at"],
            "fresh": bool(row["success"]) and not bool(row["truncated"]),
            "error": row["error"],
            "truncated": bool(row["truncated"]),
        }

    @staticmethod
    def _repository_id(canonical_name: str | None, local_identity: str | None) -> str:
        key = f"github:{canonical_name}" if canonical_name else f"local:{local_identity}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def apply_git_observation(
        self, orca_id: str, state: object, *, observed_at: str | None = None
    ) -> None:
        """Replace auto-observed main/submodule facts while preserving related checkouts."""
        observed_at = observed_at or _now()
        with connect_database(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            worktree = connection.execute(
                "SELECT path FROM worktrees WHERE orca_id=?", (orca_id,)
            ).fetchone()
            if worktree is None:
                raise ValueError("Orca worktree was not found")
            main_repository = getattr(state, "github_repository", None)
            main_id = self._repository_id(main_repository, str(worktree["path"]))
            connection.execute(
                "INSERT OR IGNORE INTO repositories(id, canonical_name, local_identity) VALUES (?, ?, ?)",
                (main_id, main_repository, None if main_repository else str(worktree["path"])),
            )
            connection.execute(
                "DELETE FROM repository_checkouts WHERE orca_id=? AND relation IN ('main','submodule')",
                (orca_id,),
            )
            connection.execute(
                """
                INSERT INTO repository_checkouts(
                    orca_id, repository_id, relation, path, branch, commit_sha, dirty,
                    ahead, behind, stale, observation_error, observed_at
                ) VALUES (?, ?, 'main', ?, ?, ?, ?, ?, ?, 0, NULL, ?)
                """,
                (
                    orca_id,
                    main_id,
                    str(worktree["path"]),
                    str(getattr(state, "branch", "")),
                    str(getattr(state, "commit_sha", "")),
                    int(bool(getattr(state, "dirty", False))),
                    getattr(state, "ahead", None),
                    getattr(state, "behind", None),
                    observed_at,
                ),
            )
            for submodule in getattr(state, "submodules", ()):
                canonical = getattr(submodule, "github_repository", None)
                path = str(Path(str(worktree["path"])) / str(submodule.path))
                repository_id = self._repository_id(canonical, path)
                connection.execute(
                    "INSERT OR IGNORE INTO repositories(id, canonical_name, local_identity) VALUES (?, ?, ?)",
                    (repository_id, canonical, None if canonical else path),
                )
                connection.execute(
                    """
                    INSERT INTO repository_checkouts(
                        orca_id, repository_id, relation, path, branch, commit_sha, dirty,
                        ahead, behind, stale, observation_error, observed_at
                    ) VALUES (?, ?, 'submodule', ?, ?, ?, ?, ?, NULL, 0, NULL, ?)
                    """,
                    (
                        orca_id,
                        repository_id,
                        path,
                        str(getattr(submodule, "branch", "")),
                        str(getattr(submodule, "sha", "")),
                        int(bool(getattr(submodule, "dirty", False))),
                        getattr(submodule, "unpushed_commits", None),
                        observed_at,
                    ),
                )

    def mark_git_error(self, orca_id: str, detail: str) -> None:
        """Keep last-known checkout facts and mark them stale on probe failure."""
        with connect_database(self.path) as connection:
            connection.execute(
                "UPDATE repository_checkouts SET stale=1, observation_error=? WHERE orca_id=? "
                "AND relation IN ('main','submodule')",
                (detail, orca_id),
            )

    def mark_related_git_error(self, orca_id: str, path: str, detail: str) -> None:
        with connect_database(self.path) as connection:
            connection.execute(
                "UPDATE repository_checkouts SET stale=1, observation_error=? "
                "WHERE orca_id=? AND relation='related' AND path=?",
                (detail, orca_id, path),
            )

    def add_related_repository(self, orca_id: str, path: Path) -> None:
        resolved = path.expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"related repository does not exist: {resolved}")
        repository_id = self._repository_id(None, str(resolved))
        with connect_database(self.path) as connection:
            if (
                connection.execute("SELECT 1 FROM worktrees WHERE orca_id=?", (orca_id,)).fetchone()
                is None
            ):
                raise ValueError("Orca worktree was not found")
            connection.execute(
                "INSERT OR IGNORE INTO repositories(id, local_identity) VALUES (?, ?)",
                (repository_id, str(resolved)),
            )
            connection.execute(
                """
                INSERT INTO repository_checkouts(
                    orca_id, repository_id, relation, path, branch, commit_sha, dirty,
                    stale, observed_at
                ) VALUES (?, ?, 'related', ?, '', '', 0, 1, ?)
                ON CONFLICT(orca_id, relation, path) DO NOTHING
                """,
                (orca_id, repository_id, str(resolved), _now()),
            )

    def related_checkouts(self) -> tuple[dict[str, str], ...]:
        with connect_database(self.path) as connection:
            rows = connection.execute(
                "SELECT orca_id, path FROM repository_checkouts "
                "WHERE relation='related' ORDER BY orca_id, path"
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def apply_related_git_observation(self, orca_id: str, path: str, state: object) -> None:
        canonical = getattr(state, "github_repository", None)
        repository_id = self._repository_id(canonical, path)
        with connect_database(self.path) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO repositories(id, canonical_name, local_identity) "
                "VALUES (?, ?, ?)",
                (repository_id, canonical, None if canonical else path),
            )
            updated = connection.execute(
                """
                UPDATE repository_checkouts SET repository_id=?, branch=?, commit_sha=?,
                    dirty=?, ahead=?, behind=?, stale=0, observation_error=NULL, observed_at=?
                WHERE orca_id=? AND relation='related' AND path=?
                """,
                (
                    repository_id,
                    str(getattr(state, "branch", "")),
                    str(getattr(state, "commit_sha", "")),
                    int(bool(getattr(state, "dirty", False))),
                    getattr(state, "ahead", None),
                    getattr(state, "behind", None),
                    _now(),
                    orca_id,
                    path,
                ),
            )
            if updated.rowcount != 1:
                raise ValueError("related repository checkout was not found")

    def remove_related_repository(self, orca_id: str, path: Path) -> None:
        resolved = str(path.expanduser().resolve())
        with connect_database(self.path) as connection:
            row = connection.execute(
                "SELECT relation FROM repository_checkouts WHERE orca_id=? AND path=?",
                (orca_id, resolved),
            ).fetchone()
            if row is None:
                raise ValueError("repository checkout relation was not found")
            if row["relation"] != "related":
                raise ValueError("main and submodule relations cannot be removed")
            connection.execute(
                "DELETE FROM repository_checkouts WHERE orca_id=? AND relation='related' AND path=?",
                (orca_id, resolved),
            )

    def link_reference(self, run_id: str, url: str, relation: str) -> str:
        if relation not in {"primary", "related"}:
            raise ValueError("reference relation must be primary or related")
        match = GITHUB_REF_RE.fullmatch(url.strip().rstrip("/"))
        if match is None:
            raise ValueError("canonical GitHub issue or pull request URL is invalid")
        repository = f"{match.group('owner')}/{match.group('repo')}"
        kind = "issue" if match.group("kind").lower() == "issues" else "pull_request"
        number = int(match.group("number"))
        canonical = (
            f"https://github.com/{repository}/{'issues' if kind == 'issue' else 'pull'}/{number}"
        )
        repository_id = self._repository_id(repository, None)
        ref_id = hashlib.sha256(f"{repository.lower()}:{kind}:{number}".encode()).hexdigest()
        with connect_database(self.path) as connection:
            if (
                connection.execute("SELECT 1 FROM workflow_runs WHERE id=?", (run_id,)).fetchone()
                is None
            ):
                raise ValueError("workflow run was not found")
            connection.execute(
                "INSERT OR IGNORE INTO repositories(id, canonical_name) VALUES (?, ?)",
                (repository_id, repository),
            )
            connection.execute(
                "INSERT OR IGNORE INTO external_refs(id, repository_id, kind, canonical_url, number) "
                "VALUES (?, ?, ?, ?, ?)",
                (ref_id, repository_id, kind, canonical, number),
            )
            try:
                connection.execute(
                    "INSERT INTO workflow_refs(run_id, external_ref_id, kind, relation, provenance, source) "
                    "VALUES (?, ?, ?, ?, 'explicit', 'cli')",
                    (run_id, ref_id, kind, relation),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    "this reference is already linked or an explicit primary already exists"
                ) from exc
        return canonical

    def unlink_reference(self, run_id: str, url: str) -> None:
        match = GITHUB_REF_RE.fullmatch(url.strip().rstrip("/"))
        if match is None:
            raise ValueError("canonical GitHub issue or pull request URL is invalid")
        canonical = (
            f"https://github.com/{match.group('owner')}/{match.group('repo')}/"
            f"{match.group('kind').lower()}/{int(match.group('number'))}"
        )
        with connect_database(self.path) as connection:
            deleted = connection.execute(
                "DELETE FROM workflow_refs WHERE run_id=? AND provenance='explicit' "
                "AND external_ref_id=(SELECT id FROM external_refs WHERE canonical_url=?)",
                (run_id, canonical),
            )
            if deleted.rowcount == 0:
                raise ValueError("explicit workflow reference was not found")

    def branch_targets(self) -> tuple[dict[str, str], ...]:
        with connect_database(self.path) as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT f.run_id, r.canonical_name AS repository, c.branch
                FROM repository_checkouts c
                JOIN repositories r ON r.id=c.repository_id
                JOIN step_worktrees sw ON sw.orca_id=c.orca_id
                JOIN workflows f ON f.id=sw.step_id
                WHERE c.relation IN ('main','submodule') AND c.stale=0
                  AND r.canonical_name IS NOT NULL AND c.branch != ''
                ORDER BY f.run_id, r.canonical_name, c.branch
                """
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def apply_pull_request_candidates(
        self, run_id: str, facts: tuple[object, ...], *, source: str = "branch"
    ) -> None:
        relation = "candidate-primary" if len(facts) == 1 else "candidate"
        with connect_database(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            for fact in facts:
                repository = str(fact.repository)
                number = int(fact.number)
                canonical = str(fact.url).rstrip("/")
                repository_id = self._repository_id(repository, None)
                ref_id = hashlib.sha256(
                    f"{repository.lower()}:pull_request:{number}".encode()
                ).hexdigest()
                connection.execute(
                    "INSERT OR IGNORE INTO repositories(id, canonical_name) VALUES (?, ?)",
                    (repository_id, repository),
                )
                connection.execute(
                    """
                    INSERT INTO external_refs(
                        id, repository_id, kind, canonical_url, number, observed_state,
                        stale, observation_error, observed_at
                    ) VALUES (?, ?, 'pull_request', ?, ?, ?, 0, NULL, ?)
                    ON CONFLICT(id) DO UPDATE SET observed_state=excluded.observed_state,
                        stale=0, observation_error=NULL, observed_at=excluded.observed_at
                    """,
                    (
                        ref_id,
                        repository_id,
                        canonical,
                        number,
                        str(getattr(fact, "state", "")),
                        _now(),
                    ),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO workflow_refs("
                    "run_id, external_ref_id, kind, relation, provenance, source) "
                    "VALUES (?, ?, 'pull_request', ?, 'inferred', ?)",
                    (run_id, ref_id, relation, source),
                )

    def mark_github_error(self, repository: str, detail: str) -> None:
        with connect_database(self.path) as connection:
            connection.execute(
                "UPDATE external_refs SET stale=1, observation_error=? WHERE repository_id="
                "(SELECT id FROM repositories WHERE canonical_name=?)",
                (detail, repository),
            )

    def explicit_issue_targets(self) -> tuple[dict[str, str], ...]:
        with connect_database(self.path) as connection:
            rows = connection.execute(
                """
                SELECT wr.run_id, er.canonical_url AS url
                FROM workflow_refs wr JOIN external_refs er ON er.id=wr.external_ref_id
                WHERE wr.provenance='explicit' AND er.kind='issue'
                ORDER BY wr.run_id, er.canonical_url
                """
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def add_inferred_reference(
        self, run_id: str, url: str, *, relation: str = "related", source: str
    ) -> None:
        match = GITHUB_REF_RE.fullmatch(url.strip().rstrip("/"))
        if match is None:
            return
        repository = f"{match.group('owner')}/{match.group('repo')}"
        kind = "issue" if match.group("kind").lower() == "issues" else "pull_request"
        number = int(match.group("number"))
        canonical = (
            f"https://github.com/{repository}/{'issues' if kind == 'issue' else 'pull'}/{number}"
        )
        repository_id = self._repository_id(repository, None)
        ref_id = hashlib.sha256(f"{repository.lower()}:{kind}:{number}".encode()).hexdigest()
        with connect_database(self.path) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO repositories(id, canonical_name) VALUES (?, ?)",
                (repository_id, repository),
            )
            connection.execute(
                "INSERT OR IGNORE INTO external_refs(id, repository_id, kind, canonical_url, number) "
                "VALUES (?, ?, ?, ?, ?)",
                (ref_id, repository_id, kind, canonical, number),
            )
            connection.execute(
                "INSERT OR IGNORE INTO workflow_refs("
                "run_id, external_ref_id, kind, relation, provenance, source) "
                "VALUES (?, ?, ?, ?, 'inferred', ?)",
                (run_id, ref_id, kind, relation, source),
            )
