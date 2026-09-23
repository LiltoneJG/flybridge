# ADR 0004: CLI-only resource queue control

[English](0004-cli-only-queue-control.md) | [日本語](../../ja/decisions/0004-cli-only-queue-control.md)

## Status

Accepted

## Context

Agents in Orca worktrees often cannot reach a short-lived stdio MCP client. Resource acquire, inspect, release, status, cancel, and stale recovery must share one control surface that those agents can actually run.

## Decision

The unified CLI is the only resource-queue control surface. Agents and operators use `flybridge queue acquire`, `inspect`, `release`, `status`, `cancel`, and `recover`. Role prompts inject those commands with `--config` and `--owner` when `queue.resources` is non-empty. There is no MCP package or `flybridge mcp` command.

## Consequences

- Queue ordering remains SQLite-backed and is shared by every CLI caller.
- `queue.resources` is a prompt-injection list, not an MCP allowlist.
- Agents must be able to run the Flybridge CLI with the same JSONC used to start the workflow.
- Operator-only cancel stays an explicit CLI action. Stale recovery remains available to operators; the supervisor expires dead-owner leases (see ADR 0009).
