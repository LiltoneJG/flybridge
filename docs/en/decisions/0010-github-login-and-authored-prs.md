# ADR 0010: Configured GitHub login and authored pull-request screening

[English](0010-github-login-and-authored-prs.md) | [日本語](../../ja/decisions/0010-github-login-and-authored-prs.md)

## Status

Accepted

## Context

`github.login` was only the default assignee for `board screen`. GitHub adapter commands invoked `gh` with the host's active account. A different authenticated account made private repositories unresolvable. `flybridge inventory` matches Orca worktree heads and cannot list pull requests by author. Agents then wrote ad-hoc `gh search` collectors.

`gh` has no per-command `--user` flag. `gh auth switch` mutates host configuration and races with other processes.

## Decision

When GitHub is enabled, adapters resolve `gh auth token --user <github.login>` once per process and set `GH_TOKEN` on subsequent `gh` child environments. The host active account is not switched. An existing parent `GH_TOKEN` is overridden so configuration, not the caller shell, selects the account. Tokens are never printed. `flybridge doctor` reports configured versus active logins and fails when the configured login cannot authenticate.

Add `flybridge prs screen` as a read-only authored pull-request listing. It uses GitHub search, defaults `--author` to `github.login` and `--state` to `OPEN`, and optionally attaches `--with-review-facts` with the same eligibility rules as inventory. It does not join worktrees or emit merge verdicts. Inventory remains the worktree snapshot (ADR 0006) and now includes thin `author` facts.

## Consequences

- Agents treat `flybridge prs screen` as the source of truth for authored pull requests. They must not write one-shot collectors unless that command fails.
- Operators can keep a host default `gh` account that differs from `github.login`.
- Token resolution requires `github.login` to be an authenticated `gh` account on the host.
