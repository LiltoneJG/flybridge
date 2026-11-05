# Bounded reliability trial

Use this protocol after changing workflow supervision or recovery. It compares a manual-supervision baseline with the candidate behavior without publishing raw operational records.

## Record and run

Keep raw input and output under `.reliability-trials/`, which is ignored. Each JSON-array record must identify `variant` (`baseline` or `trial`), `scenario`, and `work_kind` (`implementation`, `validation`, or `review`). It must contain these non-negative metrics:

- `completion_seconds`, `intervention_count`, and `intervention_seconds`
- `detection_seconds` and `recovery_seconds`
- `missing_results`, `stale_running_states`, and `orphan_ownership`
- `rework_count` and `resource_wait_seconds`

Run both variants for normal exit, abnormal exit, missing result, question waiting, monitor restart, delayed duplicate result, interrupted cleanup, validation-only work, and retained dirty work. Distribute the cases so implementation, validation, and review work are all represented in both variants. Use disposable state and repositories; do not use production worktrees or credentials as trial data.

```console
uv run python scripts/reliability_trial.py \
  .reliability-trials/records.json \
  --output .reliability-trials/summary.json
```

The command rejects missing coverage, duplicate case identities, and invalid metrics. Its output contains aggregates only; inspect it before copying a safe summary to an issue. Do not commit the raw records, command logs, repository paths, terminal handles, prompts, artifacts, or generated summary.

## Decision and rollback

Adopt the candidate only when it introduces no missing result, stale running state, or orphan ownership; every abnormal scenario reaches an explicit terminal or attention state; and intervention count/time do not regress without a documented reason. Completion and resource-wait time are diagnostic and must not be improved by skipping validation or cleanup.

Rollback to manual supervision when any safety metric is non-zero, recovery changes another activation's ownership, duplicate results overwrite a final result, interrupted cleanup loses retained work, or required evidence cannot be collected. The fallback is to stop automated advancement, preserve the current worktree and state directory, inspect `workflow status` and queue ownership, and let the operator perform the documented recovery commands only after external cleanup is confirmed.
