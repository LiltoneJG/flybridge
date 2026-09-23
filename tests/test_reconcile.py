from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from flybridge_application import reconcile_external_state
from flybridge_application.git_probe import GitWorktreeState
from flybridge_core import ReconcileStore, ResourceQueue, WorkflowStore, merge_workflow_marker
from flybridge_orca import ListedWorktree


def _listed(worktree_id: str, path: str, comment: str = "") -> ListedWorktree:
    return ListedWorktree(worktree_id, path, "observed", "in-progress", comment, "main", None, None)


def test_reconcile_registers_unmanaged_and_is_idempotent(tmp_path: Path) -> None:
    store = ReconcileStore(tmp_path)

    first = store.apply_orca_scan((_listed("outside", "/tmp/outside"),), truncated=False)
    second = store.apply_orca_scan((_listed("outside", "/tmp/outside"),), truncated=False)

    assert first.added == ("outside",)
    assert second.added == ()
    assert second.updated == ()
    with sqlite3.connect(store.path) as connection:
        assert (
            connection.execute(
                "SELECT ownership FROM worktrees WHERE orca_id='outside'"
            ).fetchone()[0]
            == "unmanaged"
        )


def test_dry_run_observes_git_without_persisting_it(tmp_path: Path, monkeypatch) -> None:
    worktree = _listed("outside", "/tmp/outside")

    class Client:
        def list_worktrees(self):
            return (worktree,), False

    class Probe:
        def inspect(self, path: str) -> GitWorktreeState:
            assert path == worktree.path
            return GitWorktreeState(
                "main", False, 0, 0, 0, "example/repo", ("example/repo",), (), "a" * 40
            )

    monkeypatch.setattr("flybridge_application.reconcile.GitWorktreeProbe", lambda: Probe())
    store = ReconcileStore(tmp_path)

    result = reconcile_external_state(Client(), store, dry_run=True)

    assert result.git_observed == (worktree.worktree_id,)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM worktrees").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM repository_checkouts").fetchone()[0] == 0


def test_marker_recovers_starting_step_and_tracks_path_change(tmp_path: Path) -> None:
    workflows = WorkflowStore(tmp_path)
    step = workflows.reserve_root_plan(tmp_path, "single", "task", "Do it")[0]
    reconcile = ReconcileStore(tmp_path)
    comment = merge_workflow_marker(
        "https://github.com/example/project/issues/7", step.run_id, step.id
    )

    recovered = reconcile.apply_orca_scan(
        (_listed("orca-1", "/tmp/first", comment),), truncated=False
    )
    reconcile.apply_orca_scan((_listed("orca-1", "/tmp/second", comment),), truncated=False)

    assert recovered.attached_steps == (step.id,)
    assert workflows.get(step.id).adapter_reference == "orca-1"
    assert workflows.get(step.id).worktree_path == "/tmp/second"
    with sqlite3.connect(workflows.path) as connection:
        assert connection.execute(
            "SELECT provenance, source FROM workflow_refs WHERE run_id=?",
            (step.run_id,),
        ).fetchone() == ("inferred", "orca_comment")


def test_missing_requires_complete_scans_and_cancels_queue_atomically(tmp_path: Path) -> None:
    workflows = WorkflowStore(tmp_path)
    queue = ResourceQueue(tmp_path)
    step = workflows.reserve_root_plan(tmp_path, "single", "task", "Do it")[0]
    workflows.attach_external(
        step.id,
        adapter_reference="orca-1",
        worktree_path="/tmp/first",
        terminal_handle="terminal-1",
    )
    queue.acquire("shared", step.id)
    reconcile = ReconcileStore(tmp_path)

    reconcile.apply_orca_scan((), truncated=True, missing_grace_seconds=1)
    first = reconcile.apply_orca_scan((), truncated=False, missing_grace_seconds=1)
    with sqlite3.connect(workflows.path) as connection:
        connection.execute(
            "UPDATE worktrees SET first_missing_at='2000-01-01T00:00:00+00:00' "
            "WHERE orca_id='orca-1'"
        )
    second = reconcile.apply_orca_scan((), truncated=False, missing_grace_seconds=1)

    assert first.cancelled_steps == ()
    assert second.cancelled_steps == (step.id,)
    assert workflows.get(step.id).status == "cancelled"
    assert queue.owner_requests(step.id) == []


def test_external_status_only_updates_observation(tmp_path: Path) -> None:
    workflows = WorkflowStore(tmp_path)
    step = workflows.create(tmp_path, "single", "task", "Do it")
    workflows.transition(step.id, "starting")
    workflows.attach_external(
        step.id,
        adapter_reference="orca-1",
        worktree_path="/tmp/first",
        terminal_handle="terminal-1",
    )
    workflows.transition(step.id, "running")

    ReconcileStore(tmp_path).apply_orca_scan((_listed("orca-1", "/tmp/first"),), truncated=False)

    assert workflows.get(step.id).status == "running"


def test_explicit_primary_is_unique_per_reference_kind(tmp_path: Path) -> None:
    workflows = WorkflowStore(tmp_path)
    step = workflows.create(tmp_path, "single", "task", "Do it")
    refs = ReconcileStore(tmp_path)

    refs.link_reference(step.run_id, "https://github.com/example/project/issues/1", "primary")
    refs.link_reference(step.run_id, "https://github.com/example/project/pull/2", "primary")

    with pytest.raises(ValueError, match="explicit primary"):
        refs.link_reference(step.run_id, "https://github.com/example/project/issues/3", "primary")


def test_reconcile_omits_and_prunes_unmanaged_excluded_worktrees(tmp_path: Path) -> None:
    unmanaged = tmp_path / "projects" / "flybridge"
    managed_path = tmp_path / "orca" / "flybridge"
    keep = tmp_path / "keep"
    unmanaged.mkdir(parents=True)
    managed_path.mkdir(parents=True)
    keep.mkdir()
    workflows = WorkflowStore(tmp_path)
    step = workflows.create(tmp_path, "single", "task", "Do it")
    workflows.transition(step.id, "starting")
    workflows.attach_external(
        step.id,
        adapter_reference="managed-1",
        worktree_path=str(managed_path),
        terminal_handle="terminal-1",
    )
    store = ReconcileStore(tmp_path)
    store.apply_orca_scan(
        (
            _listed("outside", str(unmanaged)),
            _listed("managed-1", str(managed_path)),
            _listed("keep", str(keep)),
        ),
        truncated=False,
    )

    class Client:
        def list_worktrees(self):
            return (
                (
                    _listed("outside", str(unmanaged)),
                    _listed("managed-1", str(managed_path)),
                    _listed("keep", str(keep)),
                ),
                False,
            )

    result = reconcile_external_state(
        Client(), store, include_git=False, exclude_worktrees=("flybridge",)
    )

    assert "outside" in result.pruned
    with sqlite3.connect(store.path) as connection:
        ids = {row[0] for row in connection.execute("SELECT orca_id FROM worktrees").fetchall()}
    assert "outside" not in ids
    assert "managed-1" in ids
    assert "keep" in ids

    again = reconcile_external_state(
        Client(), store, include_git=False, exclude_worktrees=("flybridge",)
    )
    with sqlite3.connect(store.path) as connection:
        ids = {row[0] for row in connection.execute("SELECT orca_id FROM worktrees").fetchall()}
    assert again.pruned == ()
    assert "outside" not in ids
    assert workflows.get(step.id).status != "cancelled"


def test_resolve_run_id_accepts_workflow_step_id(tmp_path: Path) -> None:
    workflows = WorkflowStore(tmp_path)
    step = workflows.create(tmp_path, "single", "task", "Do it")
    refs = ReconcileStore(tmp_path)

    assert refs.resolve_run_id(step.id) == step.run_id
    assert refs.resolve_run_id(step.run_id) == step.run_id
    with pytest.raises(ValueError, match="was not found"):
        refs.resolve_run_id("missing-id")
