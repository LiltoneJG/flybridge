# ADR 0002: JSONC configuration with path-only external skills

## Status

Accepted

## Context

Workflows need personal and organization-owned rules without publishing them or
making behavior depend on the parent shell. Users also need an easy-to-inspect
configuration surface.

## Decision

Use a JSONC file in `config/` as the sole user configuration source. Only
`start --mode` and `doctor --mode` override `default_mode`; only
`start --queue-observer` and `start --no-queue-observer` override
`queue.observer`. Other CLI arguments are operation inputs, not configuration
overrides. External skill sources, role assignments, and language overlays are
absolute paths or glob patterns in that file. `~` and directory symbolic links
are resolved, and the resulting documents are indexed in generated English
prompts.

## Consequences

- No skill content, shell-derived path, or credential is committed.
- Child shells behave like their parent because configuration is explicit.
- Environment variables and XDG configuration discovery are deliberately not
  supported. XDG-style paths remain appropriate for runtime state.
