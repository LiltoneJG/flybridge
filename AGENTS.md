# Flybridge agent guide

Start with this file; read only the linked document that matches the task.

- Product setup, configuration, and common workflows: [README.md](README.md)
- English technical reference: [docs/en/specification.md](docs/en/specification.md)
- Japanese technical reference: [docs/ja/specification.md](docs/ja/specification.md)
- Commands and bundled skills: [docs/en/skill-and-command-catalog.md](docs/en/skill-and-command-catalog.md)
- Architecture and decisions: [docs/en/architecture.md](docs/en/architecture.md) and [docs/en/decisions/](docs/en/decisions/)
- Package-local implementation guidance: the relevant `packages/*/README.md`

Use `uv run python scripts/test.py -q` for tests. That suite must mock Orca and agent PATH resolution; it must not launch Orca or an LLM. Real runtime checks are the gated acceptance scripts in [docs/en/specification.md](docs/en/specification.md). Run `uv run pre-commit run --all-files` before handing off code. Do not read or add ignored local configuration, runtime state, or external skill content.
