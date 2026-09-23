from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from flybridge_core import ReconcileStore, ReconcileSummary

from .git_probe import GitProbeError, GitWorktreeProbe
from .inventory import worktree_is_excluded


@dataclass(frozen=True)
class FullReconcileResult:
    orca: ReconcileSummary
    git_observed: tuple[str, ...] = ()
    git_errors: tuple[str, ...] = ()
    pruned: tuple[str, ...] = ()


def reconcile_external_state(
    client: object,
    store: ReconcileStore,
    *,
    dry_run: bool = False,
    include_git: bool = True,
    missing_observations: int = 2,
    missing_grace_seconds: int = 300,
    exclude_worktrees: Sequence[str] = (),
) -> FullReconcileResult:
    """Observe outside transactions, then atomically apply each completed observation set."""
    try:
        worktrees, truncated = client.list_worktrees()  # type: ignore[attr-defined]
    except (AttributeError, OSError, RuntimeError, ValueError) as exc:
        return FullReconcileResult(store.record_failure(str(exc), dry_run=dry_run))
    managed_ids = store.managed_orca_ids()
    kept = tuple(
        worktree
        for worktree in worktrees
        if not worktree_is_excluded(worktree.path, exclude_worktrees)
        or worktree.worktree_id in managed_ids
    )
    summary = store.apply_orca_scan(
        kept,
        truncated=bool(truncated),
        dry_run=dry_run,
        missing_observations=missing_observations,
        missing_grace_seconds=missing_grace_seconds,
    )
    pruned: tuple[str, ...] = ()
    if summary.success and not truncated:
        candidates = [
            orca_id
            for orca_id, path in store.list_unmanaged_worktrees()
            if worktree_is_excluded(path, exclude_worktrees)
        ]
        pruned = store.delete_worktrees(candidates, dry_run=dry_run)
    if not include_git:
        return FullReconcileResult(summary, pruned=pruned)
    observed: list[str] = []
    errors: list[str] = []
    probe = GitWorktreeProbe()
    for worktree in kept:
        try:
            state = probe.inspect(worktree.path)
            if not dry_run:
                store.apply_git_observation(worktree.worktree_id, state)
            observed.append(worktree.worktree_id)
        except (GitProbeError, OSError, RuntimeError, ValueError) as exc:
            detail = f"{worktree.worktree_id}: {exc}"
            if not dry_run:
                store.mark_git_error(worktree.worktree_id, str(exc))
            errors.append(detail)
    for related in store.related_checkouts():
        try:
            state = probe.inspect(related["path"])
            if not dry_run:
                store.apply_related_git_observation(related["orca_id"], related["path"], state)
            observed.append(f"{related['orca_id']}:{related['path']}")
        except (GitProbeError, OSError, RuntimeError, ValueError) as exc:
            detail = f"{related['orca_id']}:{related['path']}: {exc}"
            if not dry_run:
                store.mark_related_git_error(related["orca_id"], related["path"], str(exc))
            errors.append(detail)
    return FullReconcileResult(summary, tuple(observed), tuple(errors), pruned)
