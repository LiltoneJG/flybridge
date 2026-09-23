# ADR 0007: Canonical GitHub issue URLs as worktree join keys

[English](0007-canonical-issue-url.md) | [日本語](../../ja/decisions/0007-canonical-issue-url.md)

## Status

Accepted

## Context

Operators need to join GitHub Project issues to local Orca worktrees and development refs. Path-name heuristics collide across repositories. Orca's `linkedIssue` is a number without a repository. `board screen` listed Project fields only, and `set_lifecycle` overwrote worktree comments.

## Decision

The join key is a set, not a single URL: canonical GitHub issue URLs `https://github.com/{owner}/{repo}/issues/{number}` plus GitHub development refs (`repository#head_ref_name` and linked branch `repository+name`). New `workflow start` requires `--issue`. Attach-existing may read the URL from the worktree comment. Flybridge writes that URL into the Orca comment and preserves it across lifecycle comment updates. `inventory` reports URL-based `github_hint.issues` from the comment and from a same-repo `linkedIssue` only. `board screen --with-refs` joins an issue when that set intersects a worktree's comment URLs, parent or submodule origin plus branch, or inventory pull-request heads. Matching does not parse directory names and does not change GitHub Project fields. Submodule repositories are not emitted as fake worktree rows.

## Consequences

- Unstarted classification stays a caller policy.
- Worktrees without a comment URL appear in `unmatched_worktrees` until a URL or GitHub development ref intersects a board issue.
- Workflow run references store the canonical URL in the unified SQLite schema (schema version 1); the compatibility workflow facade retains `issue_url`.
