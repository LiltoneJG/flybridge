# ADR 0008: Unified state and command-driven reconciliation

## Status

Accepted.

## Decision

Flybridge stores durable state in private, WAL-backed `flybridge.sqlite3`, starting at schema version 1. Runs contain dependency-linked steps; worktrees, repositories, external references, agent runs, operations, handoffs, terminals, and FIFO requests refer to those identities. Legacy databases are rejected with archive/remove guidance and are never migrated or deleted automatically.

Flybridge has no daemon. Workflow commands perform a lightweight complete Orca scan and `flybridge reconcile` performs the explicit wider scan. External calls finish before an apply transaction begins. Orca status remains an observation, not workflow authority. Only successful, non-truncated full scans count as absence evidence; the default cancellation threshold is two observations and 300 seconds. Cancellation, successor cancellation, queue release, and FIFO promotion commit together.

The schema permits repeated role slots, attempts, many-to-many handoffs, and agent model snapshots. This release accepts `orca.agents.reviewer` as a list of `{agent, model}` specs, fans out from a fixed worker commit SHA, waits for every review, and completes only when every reviewer approved. Any `changes-requested` report fans in every review artifact and appends a bounded correction round.
