# ADR 0004: CLI-only resource queue control

## Status

Accepted

## Context

Version 1 originally exposed resource acquire, inspect, release, and status
through a short-lived stdio MCP server, while cancellation and stale recovery
stayed on the unified CLI. Agents in Orca worktrees often could not reach that
MCP client, so the documented agent path did not match daily operation.

## Decision

The unified CLI is the only resource-queue control surface. Agents and operators
use `flybridge queue acquire`, `inspect`, `release`, `status`, `cancel`, and
`recover`. Role prompts inject those commands with `--config` and `--owner` when
`queue.resources` is non-empty. There is no MCP package or `flybridge mcp`
command.

## Consequences

- Queue ordering remains SQLite-backed and is shared by every CLI caller.
- `queue.resources` is a prompt-injection list, not an MCP allowlist.
- Agents must be able to run the Flybridge CLI with the same JSONC used to start
  the workflow.
- Operator-only cancel and stale recovery stay explicit CLI actions.
