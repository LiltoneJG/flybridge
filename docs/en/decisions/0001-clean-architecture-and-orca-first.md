# ADR 0001: Clean boundaries with Orca as the first adapter

[English](0001-clean-architecture-and-orca-first.md) | [日本語](../../ja/decisions/0001-clean-architecture-and-orca-first.md)

## Status

Accepted

## Context

The predecessor tools mixed terminal control, workflow policy, configuration, and issue handling. Version 1.0.0 needs a reliable Orca implementation now, while leaving a credible path to other IDEs later.

## Decision

Core state and use cases are adapter-neutral. Orca-specific subprocess commands live only in `packages/orca`; entry points depend on application services rather than the other way around. Version 1.0.0 implements only that adapter.

## Consequences

- Another IDE requires an adapter, not a second CLI or a fork of queue logic.
- Orca changes remain localized.
- An abstraction must earn its place through a use case; version 1.0.0 does not build unneeded adapters.
