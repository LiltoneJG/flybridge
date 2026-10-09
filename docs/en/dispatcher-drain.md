# Dispatcher drain and recovery

The stop/start protocol controls only the dispatcher for the selected configuration's state
directory. It never sends signals to a stored PID, stops actors, kills job children,
releases manual leases, acknowledges results, or resolves cleanup blocks.

## Protocol-aware service

`queue dispatcher stop` persists a drain barrier and returns status immediately.
It is an asynchronous request, not proof that the service has exited. The barrier
and job claims serialize through SQLite `BEGIN IMMEDIATE`: a claim committed first
runs to completion, while a stop committed first prevents the claim. New requests
remain durable and preserve FIFO order; registration, inspection and explicit ACK
remain available. Automatic `ensure_dispatcher` and delayed `serve` launches honor
the barrier. The service stops claiming and delivering notifications, waits for
its existing executors and cleanup checks, preserves pending result notifications,
and exits while retaining the barrier. SIGTERM/SIGINT received by the new foreground
service request the same drain; they do not terminate its children.

Wait for `queue dispatcher status` to show `stop_complete: true`, `lock_held: false`
and `running_jobs: 0`. A heartbeat alone is insufficient. Only then use explicit
`queue dispatcher start` to clear the barrier and launch the new code. Start refuses
while another process holds the service lock or any running job remains. A concurrent
later stop wins over a delayed launch. Concurrent starts may spawn competing children;
the service lock admits only one, and each child rechecks the durable barrier.

Startup never calls `abandon_running_jobs`. A remaining running job has unknown
child-process liveness, even when the dispatcher lock is free. Automatic restart and
explicit start refuse, preserving its lease, job and outboxes without replay. An
operator must establish child execution stopped and external cleanup, persist the
stop barrier, then use `queue dispatcher recover-job REQUEST --execution-stopped
--cleanup-confirmed` before retrying. Recovery requires the exclusive service lock
and barrier, records only the exact orphan as failed with unknown command exit,
preserves its pending result ACK and promotes FIFO without executing it again.
Terminal absence, an old heartbeat or a missing parent
process does not prove child exit. Drain has no timeout or forced termination.

## Legacy service migration

A pre-protocol service ignores the barrier and can still claim jobs or deliver unsafe
notifications. `legacy_or_unknown: true` reports this limitation; stop does not signal
it or claim migration success. PID values in status are informational. PID reuse,
wrong configuration and stale heartbeat cannot authorize termination.

Parent-managed migration must freeze **all** producers, legacy automatic launchers
and notification sources for this exact state, then establish a safe boundary with
no running jobs or in-flight deliveries. Verify the actual process incarnation,
configuration/state identity, exclusive lock owner and child processes immediately
before any parent-authorized external stop, using an identity-bound OS mechanism
such as a pidfd rather than killing a saved PID. If identity, child liveness or the
producer freeze is uncertain, preserve everything and defer migration. The new CLI
cannot create this boundary for old binaries. Queued work, manual leases and pending
results are not reasons to cancel or release them.

The new schema is version 9. Version 8 retains the exact four-field intermediate
control definition. Opening state with the isolated new CLI validates that definition
and transactionally migrates versions 7/8 to 9; the incarnation fields are added only
with a version increment. Unknown version-8 control definitions are rejected;
older CLI binaries reject that upgraded schema. Therefore do not use the new CLI
against live legacy state merely to inspect or experiment: first coordinate the
producer freeze and version transition. After the legacy process has exited, retain
the freeze, install the new code for every launcher, establish the durable stop
barrier, verify lock absence and zero running jobs, then explicitly start. Remove
the freeze only after status confirms the protocol-aware service. Existing-PTY
notification delivery remains fail-closed; no raw-PTY fallback exists.

All automated validation uses temporary state and mock Orca/agent execution. Live
migration requires a separate parent operational decision.
