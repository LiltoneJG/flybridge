# ADR 0009: Per-run delivery and supervisor timeouts

[English](0009-run-delivery-and-watchdog-timeouts.md) | [日本語](../../ja/decisions/0009-run-delivery-and-watchdog-timeouts.md)

## Status

Accepted

## Context

Orchestrated roles were forbidden from pushing. The parent operator harvested and pushed after every reviewer approved, which delayed delivery until a human visited every worktree. Queue wait was often reported as `role-ready --outcome blocked`, which terminated the run and cancelled the request. Child worktree names reused `{name}-worker`, so unreconciled ghost ownership blocked relaunch. Queue leases had operator-only stale recovery and no wait expiration.

## Decision

- After every reviewer approves the same SHA, the coordinator harvests into the manager worktree, runs `delivery-check`, and fast-forward pushes once from that worktree. Force push is not supported. Hosted CI is not awaited. Workers and reviewers never push. A single-mode agent pushes itself after local verification.
- Queue wait is parking. Roles report the request-id and remain in the terminal. The supervisor labels that state `waiting_resource` and does not consume a declared blocker for a role that still holds a waiting or leased request. Healthy waiters are not expired for age.
- `queue.wait_timeout_seconds` remains in JSONC for existing configs and is not applied by the supervisor. `queue.lease_timeout_seconds` defaults to 3600 and is the age used by operator `queue recover`. The supervisor expires a lease only when its owner is not running or its terminal is invalid, then promotes the next waiter. `timeouts.role_seconds` defaults to 3600 and applies only to running roles that are not queued and have no unconsumed `role-ready`. Grant and release refresh `activated_at`. Agents do not poll. Commits are kept; a timed-out role is cancelled and an orchestrated run is `blocked` with a timeout reason.
- Orchestrated child names include an 8-character workflow-id suffix. Ghost ownership (adapter present, worktree missing, terminal ended) is reconciled before launch. Living worktrees are not deleted.

## Consequences

- Delivery is a per-run coordinator duty, not a parent-operator batch.
- A blocked or failed run may still receive one fast-forward progress push when harvest is a fast-forward of verified commits.
- SQLite schema version 2 adds `workflows.activated_at` with an in-place migration from version 1.
