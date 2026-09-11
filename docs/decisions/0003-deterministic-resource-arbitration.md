# ADR 0003: SQLite-backed FIFO resource arbitration

## Status

Accepted

## Context

Some workflow operations interfere with each other. Inferring eligibility from
an agent conversation makes ordering, debugging, and recovery unpredictable.

## Decision

Represent named exclusive resources with a SQLite-backed FIFO queue. The queue
uses transactional transitions and emits an event log. A visible Orca terminal
observes that log; it does not decide scheduling.

## Consequences

- Ordering and promotion can be tested without an LLM or Orca.
- Any interfering operation can opt into a named resource.
- Stale leases require explicit recovery rules, which are part of the core
  domain rather than an undocumented operator action.
- Agents and operators share one CLI control surface over that queue (see
  ADR 0004).
