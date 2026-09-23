# ADR 0003: SQLite-backed FIFO resource arbitration

[English](0003-deterministic-resource-arbitration.md) | [日本語](../../ja/decisions/0003-deterministic-resource-arbitration.md)

## Status

Accepted

## Context

Some workflow operations interfere with each other. Inferring eligibility from an agent conversation makes ordering, debugging, and recovery unpredictable.

## Decision

Represent named exclusive resources with a SQLite-backed FIFO queue. The queue uses transactional transitions and emits an event log. A visible Orca terminal observes that log and may notify the parked owner after promotion; it does not decide scheduling or change FIFO order.

## Consequences

- Ordering and promotion can be tested without an LLM or Orca.
- Any interfering operation can opt into a named resource.
- Stale leases require recovery rules, which are part of the core domain. Operators may recover explicitly; the durable supervisor expires a lease when its owner is dead (see ADR 0009).
- Agents and operators share one CLI control surface over that queue (see ADR 0004).
