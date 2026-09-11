# Skill and command catalog

Flybridge ships only reusable public guidance. Organization and personal policies stay in
external catalogs and are referenced through local JSONC configuration.

## Bundled guidance

| Path | Purpose |
| --- | --- |
| `skills/resource-coordination/SKILL.md` | Acquire, inspect, and release named FIFO resources |
| `language_specific/japanese.md` | Optional Japanese response guidance written in English |

List the absolute path to `skills/` in `skills.sources` to discover bundled `SKILL.md`
documents, or a glob such as `/path/to/flybridge/skills/*/SKILL.md`. List a language
document or glob in `skills.language_specific` only when that overlay should apply.
`~` and directory symbolic links are resolved.

## Agent-facing queue commands

| Command | Purpose |
| --- | --- |
| `flybridge queue acquire` | Join a named FIFO queue and receive a request or lease ID |
| `flybridge queue inspect` | Detect when an owner-matched waiting request has become leased |
| `flybridge queue release` | Release an owner-matched exact lease and promote the oldest waiter |
| `flybridge queue status` | Read aggregate queue counts |

Role prompts include these commands when `queue.resources` is non-empty. An empty
list injects no resource instructions. Queue cancellation and stale recovery are
intentionally operator-only CLI actions.
