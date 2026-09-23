# ADR 0006: Read-only Orca worktree inventory

[English](0006-read-only-worktree-inventory.md) | [日本語](../../ja/decisions/0006-read-only-worktree-inventory.md)

## Status

Accepted

## Context

Operators and agents need a stable snapshot of every Orca worktree's git state and GitHub pull-request facts. Ad-hoc Python that shells out to `orca-ide` and `gh` diverges between sessions. Workflow start does not select issues by local checkout presence; a dedicated read-only inventory command must supply those facts.

## Decision

Add `flybridge inventory` as a read-only facts snapshot. It lists Orca worktrees, inspects each checkout with git, and optionally batches pull-request queries by parent and submodule GitHub repository. Open queries stay the bulk path. Unmatched heads also receive a bounded `OPEN`/`MERGED`/`CLOSED` query, and an Orca hinted pull-request number is fetched regardless of state. `--with-review-facts` is an optional second GraphQL pass over unique matched eligible open pull requests; it does not change the bulk open query. The command does not write SQLite workflow state, start agents, or emit mergeability verdicts.

GitHub adapter methods remain keyed by repository and branch head. Joining those facts to Orca paths is an application/CLI use case, not Project screening and not workflow dispatch. Submodule checkouts are facts on the parent worktree row, not extra Orca worktrees.

## Consequences

- Agents can treat the inventory JSON as the source of truth for collection. When GitHub is enabled, selected-repository query failures still print that JSON and then return exit status 2 so callers cannot treat an empty `pull_requests` list as success. `github.skip_repositories` prefixes are never queried. HTTP 502/503/504 after one retry is a `warnings` entry and does not set exit status 2. Pull-request rows include `base_ref_stale`; refreshing the GitHub base is `prs refresh-base`, not this command.
- `board screen` lists Project issues. Local matching by canonical issue URL is `board screen --with-refs` (see ADR 0007), not this command.
- GitHub Actor `is_bot` (Bot type, App `resourcePath`, or `[bot]` login) is a reported fact. Operator policy such as treating a User as an AI reviewer, or interpreting review comments as blockers, stays outside this command.
