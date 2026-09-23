# ADR 0011: Terminal-run child retirement and LFS-safe submodule prepare

[English](0011-terminal-run-child-retirement.md) | [日本語](../../ja/decisions/0011-terminal-run-child-retirement.md)

## Status

Accepted

## Context

After an orchestrated run reached `completed`, `blocked`, or `failed`, the coordinator harvested (and on success, pushed) but left worker and reviewer worktrees on disk. The next `workflow start --attach-existing` reused the same workflow name and failed with `AdapterReferenceConflict` while those worktrees still existed. Name-ghost reconciliation only runs when both the worktree and terminal are gone.

`workflow cleanup --dry-run` listed active records and unreconciled failed/cancelled worktrees. A completed manager with leftover owned children was not a candidate, so operators saw `candidate_count: 0`.

`workflow retire` harvests into the manager worktree, then runs `git submodule update --init --recursive --checkout` with a 120 second timeout. Nested Git LFS submodules exceeded that budget, left a dirty index, and made the next retire fail with `manager worktree is not clean`. Harvest does not need LFS blob contents.

## Decision

- When an orchestrated run becomes `completed`, `blocked`, `failed`, or `delivery_failed`, the coordinator runs `retire --keep manager` after delivery or progress-push. Attach-existing manager worktrees are never removed. A retire failure is recorded as `cleanup_error` on the root and does not roll back the run outcome.
- `workflow cleanup --dry-run` also lists finished orchestrated roots that still own an unreconciled child worktree, with `retire_recommended` and `root_id`. `--apply` remains age-forced stale reconciliation only; leftover children are closed with `workflow retire --keep manager`.
- Repository preparation sets `GIT_LFS_SKIP_SMUDGE=1` on the submodule update. Timeout and non-zero exit stay fail-closed. If the manager is dirty after failure, the error names that dirt.
- Operator `workflow harvest` and `workflow retire --keep manager|none` remain recovery controls. If the worker path is no longer a git worktree, harvest skips with `worker_worktree_gone` and retire still closes owned children. A present worktree with a mismatched Git identity stays fail-closed.

## Consequences

- Nightly attach of the same name no longer depends on a parent agent remembering to retire children.
- Nested LFS checkouts no longer block harvest or retirement.
- Cleanup dry-run is the signal that operator retire is still needed when automatic retirement failed.
- A worker already moved to trash no longer blocks `retire --keep manager`.
