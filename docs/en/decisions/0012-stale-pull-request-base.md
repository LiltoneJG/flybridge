# ADR 0012: Stale pull-request base facts and explicit refresh

[English](0012-stale-pull-request-base.md) | [日本語](../../ja/decisions/0012-stale-pull-request-base.md)

## Status

Accepted

## Context

GitHub can keep a pull request's recorded base commit behind the current target-branch tip. Inventory and authored listings then show an inflated diff, and AI review often reports false scope-creep findings. Inventory is read-only (ADR 0006) and must not mutate GitHub.

## Decision

Thin pull-request facts include `base_ref_oid`, `base_ref_tip_oid`, and `base_ref_stale` from GraphQL (`baseRefOid` versus `baseRef.target.oid`). `flybridge inventory` and `flybridge prs screen` report those fields and do not PATCH. Operators refresh with `flybridge prs refresh-base <owner/repo> <number>`, which repeats the recorded `baseRefName` so GitHub recomputes the base onto the current tip.

## Consequences

- Stale-base detection is a fact, not a merge verdict and not an AI-review classifier.
- Refresh is an explicit mutating command. Nightly inventory remains read-only.
- Operator skills decide when to refresh before treating bot scope-creep comments as code work.
