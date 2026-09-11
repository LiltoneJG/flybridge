import json
import re
from pathlib import Path

from flybridge_core.config import strip_jsonc

ROOT = Path(__file__).parents[1]


def test_mit_license_is_only_at_the_repository_root() -> None:
    assert (ROOT / "LICENSE").is_file()
    assert list((ROOT / "packages").glob("*/LICENSE")) == []


def test_pre_commit_is_python_focused() -> None:
    config = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")

    assert "ruff-check" in config
    assert "ruff-format" in config
    assert "trailing-whitespace" in config
    assert "clang-format" not in config
    assert "eslint" not in config
    assert "prettier" not in config
    assert "mypy" not in config
    assert "shellcheck" not in config


def test_root_readme_cli_examples_use_source_checkout_invocation() -> None:
    banner = "[English](README.md) | [日本語](README_ja.md)"
    for name in ("README.md", "README_ja.md"):
        readme = (ROOT / name).read_text(encoding="utf-8")
        command_lines = [
            line
            for line in readme.splitlines()
            if re.match(r"^(?:uv run --package flybridge-cli )?flybridge ", line)
        ]

        assert readme.startswith(banner)
        assert "git clone" not in readme
        assert re.search(r"\bv1\b", readme, flags=re.IGNORECASE) is None
        assert "v0.1" not in readme
        assert "docs/assets/orca-orchestrated.png" in readme
        assert "```mermaid" in readme
        assert command_lines
        assert all(
            line.startswith("uv run --package flybridge-cli flybridge ") for line in command_lines
        )
        assert "flybridge-mcp" not in readme
        assert "mcp config" not in readme


def _example_payload(name: str) -> dict[str, object]:
    text = (ROOT / "config" / name).read_text(encoding="utf-8")
    payload = json.loads(strip_jsonc(text))
    assert isinstance(payload, dict)
    return payload


def test_example_configs_share_non_language_values() -> None:
    english = (ROOT / "config" / "flybridge.jsonc.example").read_text(encoding="utf-8")
    japanese = (ROOT / "config" / "flybridge.ja.jsonc.example").read_text(encoding="utf-8")
    english_payload = _example_payload("flybridge.jsonc.example")
    japanese_payload = _example_payload("flybridge.ja.jsonc.example")
    agents = {"single": "codex", "manager": "codex", "worker": "codex", "reviewer": "codex"}

    for payload in (english_payload, japanese_payload):
        assert payload["default_mode"] == "orchestrated"
        assert payload["orca"]["agents"] == agents
        assert payload["skills"]["sources"] == []
        assert payload["queue"] == {"observer": True, "resources": []}
        assert payload["github"]["enabled"] is False

    assert english_payload["skills"]["response_language"] == "English"
    assert english_payload["skills"]["language_specific"] == []
    assert japanese_payload["skills"]["response_language"] == "Japanese"
    assert japanese_payload["skills"]["language_specific"] == [
        "/path/to/flybridge/language_specific/japanese.md"
    ]
    assert "The English prompt" in english
    assert "英語の prompt" in japanese
    assert "queue acquire" in english
    assert "queue acquire" in japanese
    assert "mcp" not in english.lower()
    assert "mcp" not in japanese.lower()


def test_github_actions_are_immutable_and_do_not_persist_credentials() -> None:
    workflows = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    uses_pattern = re.compile(r"^\s*-\s+uses:\s+[^@\s]+@([0-9a-f]{40})(?:\s+#\s+\S+)?$")

    for workflow_path in workflows:
        workflow = workflow_path.read_text(encoding="utf-8")
        uses_lines = [line for line in workflow.splitlines() if "uses:" in line]
        assert uses_lines
        assert all(uses_pattern.match(line) for line in uses_lines)
        assert workflow.count("persist-credentials: false") >= workflow.count("actions/checkout@")


def test_secret_scanning_precedes_public_validation() -> None:
    public_ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert public_ci.index("gitleaks/gitleaks-action@") < public_ci.index("pre-commit")
    assert public_ci.index("gitleaks/gitleaks-action@") < public_ci.index("pytest")
    assert public_ci.index("gitleaks/gitleaks-action@") < public_ci.index("uv build --all-packages")
