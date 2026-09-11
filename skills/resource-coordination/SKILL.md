# Resource coordination

Use this skill when a task runs a process that can interfere with another workflow.

1. Match the operation to a resource name configured by the user.
2. Run `flybridge --config <config-path> queue acquire <resource> --owner <workflow-id>`
   using the config path and workflow ID supplied in the prompt.
3. If the result is waiting, run
   `flybridge --config <config-path> queue inspect <request-id> --owner <workflow-id>`
   until its status is `leased`.
4. Run the interfering operation only while holding the lease.
5. Run `flybridge --config <config-path> queue release <resource> --lease <lease-id> --owner <workflow-id>`
   immediately afterward.

Cancellation and stale recovery are operator-only CLI actions. Report a blocked queue
explicitly instead of bypassing it.
