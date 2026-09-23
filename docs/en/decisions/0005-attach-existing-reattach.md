# ADR 0005: Reattach after explicit adapter-reference reconcile

[English](0005-attach-existing-reattach.md) | [日本語](../../ja/decisions/0005-attach-existing-reattach.md)

## Status

Accepted

## Context

`workflow start --attach-existing` must not create or delete an Orca worktree. The partial unique index `workflow_owned_adapter_reference` still forbids two unreconciled rows from sharing an adapter reference. After cancel, fail, or complete, that index blocked a new attach until cleanup ran—even when Flybridge never owned the checkout.

A running attach also kept the previous objective and sent a resume prompt, which did not match an operator who was issuing a new brief on the same path.

## Decision

The ownership unique index is unchanged. Reusing an adapter reference for a new root is allowed only after the previous unreconciled terminal row is marked `external_reconciled_at` without removing the worktree.

- A `running` owner keeps the same workflow ID, replaces the recorded objective, and starts a replacement agent with a start prompt.
- A `cancelled` or `failed` non-owning workflow is marked reconciled when finish completes, so the next attach does not wait for cleanup.
- Owning worktrees stay unreconciled until cleanup so a later `worktree rm` cannot race an operator who still needs that checkout.

Workflow names are unique only while `requested`, `starting`, or `running`.

## Consequences

- Operators can cancel an attached agent and immediately reattach the same path, including with a reused `-n`.
- Cleanup remains the only path that deletes a Flybridge-owned worktree.
- Attach defaults (`single`, observer off) apply to the new root, not to the in-place running replacement which keeps the existing role and mode.
