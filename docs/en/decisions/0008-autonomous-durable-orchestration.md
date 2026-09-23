# ADR 0008: Autonomous durable orchestration and state-backed artifacts

[English](0008-autonomous-durable-orchestration.md) | [日本語](../../ja/decisions/0008-autonomous-durable-orchestration.md)

## Status

Accepted

## Context

The original orchestrated lifecycle required an operator to copy each handoff, complete the current role, and launch its successor. That made the operator a fragile scheduler, left plan documents in implementation repositories, and could not represent review feedback cycles durably. Orca worktree IDs also combine a runtime repository identity with a checkout path; treating that identity as the GitHub implementation repository made attached and child worktrees ambiguous.

## Decision

- An orchestrated start creates one durable coordinator terminal. Roles signal readiness with `workflow role-ready`; the coordinator verifies the referenced artifact and repository state, completes the role, and launches the next role.
- Manager plans, worker verification, and reviewer reports are bounded UTF-8 artifacts under `state_dir/artifacts`. Files are named by SHA-256, use private permissions, and are verified before a readiness signal is consumed. They are never plan commits or coordination files in the implementation repository.
- A changes-requested review returns to the existing worker with the review artifact. The worker must produce a new commit before another readiness signal. The configured `orchestration.max_review_cycles` bounds this loop.
- Any role may report `--outcome blocked` after storing its required artifact with verified and unverified scope. The readiness event stores `blocked_reason` separately from reviewer approval semantics. The coordinator closes that role, cancels requested successors, and atomically consumes the blocker while marking the aggregate run blocked.
- Workflow records separately persist the GitHub implementation repository, Orca runtime repository ID, and starting Git SHA. Child creation selects the runtime repository while identity verification protects the implementation repository and commit ancestry.
- The coordinator atomically records a terminal run outcome, consumes reviewer readiness, and releases terminal ownership. It then writes and flushes the result JSON before closing its own terminal. Real Orca testing established that terminal creation returns to a connected shell rather than disconnecting naturally, so post-flush self-close is required. Operator cleanup remains a fallback for an inactive tab left by a failed close.
- Transient coordinator errors are persisted on the run and retried with bounded exponential backoff across process restarts. Permanent validation errors, dead roles, exhausted review cycles, and the configured coordinator error limit produce durable blocked or failed outcomes.
- Operators may use `workflow coordinator-retry` after a hot upgrade or coordinator crash. It closes a live stale handle, CAS-resets only a replayable blocked/failed run, clears coordinator-only error and release metadata, and launches current code. Role, readiness, and artifact records remain unchanged. Completed runs, consumed blockers, and consumed maximum-cycle outcomes cannot be reopened; an unconsumed blocker remains replayable so upgraded code can terminate it correctly.
- `workflow proceed`, `handoff`, and `advance` remain recovery and diagnostic controls. They are not the normal orchestrated lifecycle.

## Initial state schema

Schema version 1 is the v1.0.0 state format. It includes repository identities, starting Git SHAs, durable workflow artifacts, orchestration runs, role-readiness events, review-cycle state, coordinator release metadata, and nullable readiness `blocked_reason` values. There is no predecessor schema and therefore no migration path in v1.0.0.

## Consequences

The normal manager-to-worker-to-reviewer path no longer needs operator `proceed`. Progress survives CLI process restarts and readiness replay is idempotent. State storage now contains potentially sensitive plans and review evidence, so its private permissions, size limits, digest verification, backup, and retention are operational requirements.
