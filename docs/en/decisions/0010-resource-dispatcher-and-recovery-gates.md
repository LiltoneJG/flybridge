# ADR 0010: Resource dispatcher and recovery gates

[English](0010-resource-dispatcher-and-recovery-gates.md) | [日本語](../../ja/decisions/0010-resource-dispatcher-and-recovery-gates.md)

## Status

Accepted

## Context

SQLite promoted FIFO waiters atomically, but an observer could be absent when a waiting request was promoted. A sent prompt did not prove that the agent received or acted on it. Workflow termination could also promote a waiter after an unverified lease was cancelled, while the external resource still contained temporary state.

## Decision

- Keep SQLite as the shared FIFO authority. A single state-directory dispatcher, guarded by a local process lock, delivers persisted grants and runs registered commands. Visible observers show events but do not deliver grants.
- Record grant and job-result acknowledgements separately from send attempts. Retry unacknowledged messages with stable request IDs.
- A job registered with `queue run` executes once after grant. Its executable cleanup check is the explicit proof needed for automatic release. Unknown execution or failed cleanup enters resource recovery; it is never replayed automatically.
- Cancelling an unverified lease, including during workflow termination, blocks that resource. An operator confirms external cleanup for the exact request before FIFO promotion. Age-only recovery is read-only.
- Workflow start and later queue or supervisor commands restart a missing dispatcher. There is no OS service installation, so no autonomous restart is promised when every Flybridge process has stopped.

## Consequences

SQLite schema version 4 upgrades version 3 in place. Manual acquire/release remains available, with agent judgment still responsible for its cleanup assertion. A recovery block favors resource safety over automatic throughput.
