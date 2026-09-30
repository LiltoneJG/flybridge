# Resource coordination

Use this skill when a task runs a process that can interfere with another workflow. The queue has no special verification category. Callers pick a name from the configured `queue.resources` list (commonly `heavy-check`) and use the steps below.

Acquire before work that contends for CPU, GPU, disk, ports, Docker, or a shared ROS graph. That includes full workspace or colcon/CMake builds, test suites that start processes, `ros2 launch`, simulators, hardware bringup, live topic or service probes, and image builds.

Do not acquire for isolated static checks: syntax or parse, formatter `--check`, AST tests, or unit tests that do not start the product. Do not acquire in order to read git history, diffs, or GitHub metadata.

Do not start an interfering check while waiting for a lease. Do not run one without a lease.

The lease covers resource-dependent preparation, one interfering operation (or an uninterrupted sequence that requires the same live environment), operation-specific cleanup, and confirmation that the next holder can use the resource without interference. Complete code changes, static checks, and preparation that does not need the shared resource before acquiring. On success or failure, identify temporary state created or changed by your work, clean it up as appropriate for that operation, and confirm the interfering state is gone. Do not change unrelated resources. A command exiting is not, by itself, confirmation that cleanup is complete.

After cleanup is confirmed, release before evaluating results, editing code, committing, preparing a report, or waiting for review. A failed check does not justify holding the lease through the investigation, fix, and retry. Reacquire for the next interfering operation and rejoin the FIFO queue; do not retain the lease to avoid another wait. For example, after a failed simulator test, stop and clean up the simulator, release, investigate and fix, then acquire again for the next build or simulator test. If an interfering process is still running, keep its lease until it and its cleanup finish, even if you perform independent analysis meanwhile.

1. Match the operation to a resource name configured by the user.
2. Run `flybridge --config <config-path> queue acquire <resource> --owner <workflow-id>` once per interfering operation, using the config path and workflow ID supplied in the prompt. Never duplicate an outstanding waiting or granted request.
3. If the result is granted, run the interfering operation only while holding that lease. Complete and confirm cleanup before `flybridge --config <config-path> queue release <resource> --lease <lease-id> --owner <workflow-id>`.
4. If the result is waiting, report the request-id and remain parked. Do not poll `queue inspect`, run `queue watch`, interpret observer JSON, or run `role-ready --outcome blocked` for the wait. Wait for a later Flybridge message that names the lease-id.
5. When that notification arrives, acknowledge and use the named lease without acquiring a duplicate. Run the interfering operation, complete and confirm cleanup, then release. An instruction not to acquire again for a granted request prevents duplicate acquisition of that request; after release, a later interfering operation needs a new acquisition starting at step 1.

If cleanup cannot be completed or confirmed, do not release the lease or claim verification complete or role readiness. Report the remaining state and the recovery needed to the operator while keeping the terminal available. This applies even when the check itself has finished or failed.

Cancellation is an operator-only CLI action. The durable supervisor expires a lease only when the owner is not running or its terminal is invalid, then promotes the next waiter. Healthy waiters are not expired for age. Do not close this terminal while waiting. Do not bypass the queue.
