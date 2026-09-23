# Resource coordination

Use this skill when a task runs a process that can interfere with another workflow. The queue has no special verification category. Callers pick a name from the configured `queue.resources` list (commonly `heavy-check`) and use the steps below.

Acquire before work that contends for CPU, GPU, disk, ports, Docker, or a shared ROS graph. That includes full workspace or colcon/CMake builds, test suites that start processes, `ros2 launch`, simulators, hardware bringup, live topic or service probes, and image builds.

Do not acquire for isolated static checks: syntax or parse, formatter `--check`, AST tests, or unit tests that do not start the product. Do not acquire in order to read git history, diffs, or GitHub metadata.

Do not start an interfering check while waiting for a lease. Do not run one without a lease.

1. Match the operation to a resource name configured by the user.
2. Run `flybridge --config <config-path> queue acquire <resource> --owner <workflow-id>` once, using the config path and workflow ID supplied in the prompt.
3. If the result is granted, run the interfering operation only while holding that lease, then `flybridge --config <config-path> queue release <resource> --lease <lease-id> --owner <workflow-id>`.
4. If the result is waiting, report the request-id and remain parked. Do not poll `queue inspect`, run `queue watch`, interpret observer JSON, or run `role-ready --outcome blocked` for the wait. Wait for a later Flybridge message that names the lease-id.
5. When that notification arrives, run the interfering operation and release the named lease immediately afterward. Do not acquire again.

Cancellation is an operator-only CLI action. The durable supervisor expires a lease only when the owner is not running or its terminal is invalid, then promotes the next waiter. Healthy waiters are not expired for age. Do not close this terminal while waiting. Do not bypass the queue.
