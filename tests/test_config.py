import json
from pathlib import Path

import pytest
from conftest import ENABLED_GITHUB, required_config, write_config
from flybridge_core import ConfigError, load_config
from flybridge_core.config import AgentSpec


def test_jsonc_comments_and_trailing_comma_are_supported(tmp_path: Path) -> None:
    config_path = tmp_path / "config.jsonc"
    config_path.write_text(
        """
        {
          // comment
          "default_mode": "single",
          "orca": { "agents": {} },
          "skills": {
            "sources": [],
            "roles": {},
            "operator": [],
            "response_language": "English"
          },
          "queue": { "observer": false, "resources": [] },
          "github": { "enabled": false },
        }
        """,
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert config.default_mode == "single"
    assert config.github.enabled is False
    assert config.max_coordinator_errors == 5
    assert config.coordinator_retry_initial_seconds == 1.0
    assert config.coordinator_retry_max_seconds == 30.0
    assert config.queue_wait_timeout_seconds == 3600
    assert config.queue_lease_timeout_seconds == 3600
    assert config.role_timeout_seconds == 3600
    assert config.delivery_orchestrated == "manager_worktree"
    assert config.delivery_force_push is False


@pytest.mark.parametrize(
    ("orchestration", "message"),
    [
        ({"max_coordinator_errors": 0}, "max_coordinator_errors"),
        ({"retry_initial_seconds": 0}, "retry_initial_seconds"),
        (
            {"retry_initial_seconds": 2, "retry_max_seconds": 1},
            "greater than or equal",
        ),
    ],
)
def test_invalid_coordinator_retry_policy_is_rejected(
    tmp_path: Path, orchestration: dict[str, object], message: str
) -> None:
    config_path = write_config(tmp_path / "config.jsonc", orchestration=orchestration)

    with pytest.raises(ConfigError, match=message):
        load_config(config_path)


def test_jsonc_never_rewrites_commas_inside_strings(tmp_path: Path) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        skills={"response_language": "English,} and ,] stay // intact /* here */"},
        queue={"resources": ["build,]", "test,}"]},
    )

    config = load_config(config_path)

    assert config.response_language == "English,} and ,] stay // intact /* here */"
    assert config.queue_resources == ("build,]", "test,}")


@pytest.mark.parametrize(
    "payload",
    [
        '{, "default_mode": "single"}',
        '{"default_mode": "single",, }',
        '{"default_mode": [1,,]}',
        '{"default_mode": "single", "orca": {,}}',
        '{"default_mode": ,}',
    ],
)
def test_invalid_comma_placement_remains_invalid_jsonc(payload: str, tmp_path: Path) -> None:
    config_path = tmp_path / "config.jsonc"
    config_path.write_text(payload, encoding="utf-8")

    with pytest.raises(ConfigError, match="invalid JSONC"):
        load_config(config_path)


def test_orca_executable_has_a_platform_default_but_agents_are_explicit(tmp_path: Path) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(config_path)

    config = load_config(config_path)

    assert config.orca_agents == {}
    assert config.orca_executable


def test_skills_are_combined_in_configuration_order(tmp_path: Path) -> None:
    general = tmp_path / "general.md"
    role = tmp_path / "manager.md"
    for path in (general, role):
        path.write_text("skill", encoding="utf-8")
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        skills={
            "sources": [general],
            "roles": {"manager": [role]},
            "response_language": "Japanese",
        },
    )

    config = load_config(config_path)

    assert config.response_language == "Japanese"
    assert config.skill_paths_for("manager") == (general, role)


def test_operator_skills_are_expanded_separately_from_workflow_roles(tmp_path: Path) -> None:
    operator = tmp_path / "operator" / "SKILL.md"
    operator.parent.mkdir()
    operator.write_text("operator policy", encoding="utf-8")
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, skills={"operator": [operator]})

    config = load_config(config_path)

    assert config.operator_skill_paths() == (operator.resolve(),)
    assert config.skill_paths_for("single") == ()


def test_skill_catalog_directories_expand_deterministically(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog"
    first = catalog / "a" / "SKILL.md"
    second = catalog / "b" / "SKILL.md"
    first.parent.mkdir(parents=True)
    second.parent.mkdir()
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, skills={"sources": [catalog, first]})

    assert load_config(config_path).skill_paths_for("single") == (first, second)


def test_empty_skill_catalog_is_rejected(tmp_path: Path) -> None:
    empty_catalog = tmp_path / "empty"
    empty_catalog.mkdir()
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, skills={"sources": [empty_catalog]})
    config = load_config(config_path)

    with pytest.raises(ConfigError, match="contains no SKILL.md"):
        config.skill_paths_for("single")


def test_directory_symlinks_are_resolved_and_escaping_catalog_links_are_rejected(
    tmp_path: Path,
) -> None:
    real_root = tmp_path / "real"
    linked_root = tmp_path / "linked"
    document = real_root / "skill.md"
    real_root.mkdir()
    document.write_text("skill", encoding="utf-8")
    linked_root.symlink_to(real_root, target_is_directory=True)
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, skills={"sources": [linked_root / "skill.md"]})

    assert load_config(config_path).skill_paths_for("single") == (document.resolve(),)

    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "SKILL.md").symlink_to(outside)
    write_config(config_path, skills={"sources": [catalog]})
    with pytest.raises(ConfigError, match="escapes configured root"):
        load_config(config_path).skill_paths_for("single")


def test_skill_globs_and_role_catalogs_expand_deterministically(tmp_path: Path) -> None:
    implementing = tmp_path / "implementing" / "skills"
    first = implementing / "alpha" / "SKILL.md"
    second = implementing / "beta" / "SKILL.md"
    first.parent.mkdir(parents=True)
    second.parent.mkdir()
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        skills={
            "sources": [],
            "roles": {"single": [str(implementing / "*" / "SKILL.md")]},
        },
    )

    assert load_config(config_path).skill_paths_for("single") == (
        first.resolve(),
        second.resolve(),
    )

    write_config(
        config_path,
        skills={"roles": {"single": [str(tmp_path / "missing" / "*" / "SKILL.md")]}},
    )
    with pytest.raises(ConfigError, match="matched no paths"):
        load_config(config_path).skill_paths_for("single")


@pytest.mark.parametrize(
    "escaped_character",
    [r"\n", r"\r", r"\u0000", r"\u001f", r"\u0085"],
)
def test_skill_paths_reject_unsafe_control_characters(
    escaped_character: str, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.jsonc"
    config_path.write_text(
        (
            '{"default_mode": "single", "orca": {"agents": {}}, "skills": {'
            f'"sources": ["/tmp/unsafe{escaped_character}path"], '
            '"roles": {}, "operator": [], "response_language": "English"}, '
            '"queue": {"observer": false, "resources": []}, "github": {"enabled": false}}'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="unsafe control"):
        load_config(config_path)


def test_relative_state_dir_is_resolved_from_configuration_file(tmp_path: Path) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, state_dir="state")

    assert load_config(config_path).state_dir == tmp_path / "state"


def test_state_dir_rejects_unsafe_control_characters(tmp_path: Path) -> None:
    config_path = tmp_path / "config.jsonc"
    config_path.write_text(
        json.dumps(required_config(state_dir="/tmp/state\u0000dir")),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="state_dir contains unsafe control"):
        load_config(config_path)


@pytest.mark.parametrize("suffix", ["", "/nested"])
def test_state_dir_resolves_symbolic_links(suffix: str, tmp_path: Path) -> None:
    real_state = tmp_path / "real-state"
    real_state.mkdir()
    linked_state = tmp_path / "linked-state"
    linked_state.symlink_to(real_state, target_is_directory=True)
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, state_dir=f"{linked_state}{suffix}")

    expected = (real_state / "nested").resolve() if suffix else real_state.resolve()
    assert load_config(config_path).state_dir == expected


def test_missing_configuration_points_to_adjacent_example(tmp_path: Path) -> None:
    config_path = tmp_path / "flybridge.jsonc"
    example = tmp_path / "flybridge.jsonc.example"
    example.write_text("{}", encoding="utf-8")

    with pytest.raises(ConfigError, match="copy"):
        load_config(config_path)


def test_missing_required_configuration_keys_are_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(config_path)
    payload = required_config()
    del payload["default_mode"]
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfigError, match="default_mode is required"):
        load_config(config_path)


def test_operator_skill_configuration_is_required(tmp_path: Path) -> None:
    config_path = tmp_path / "config.jsonc"
    payload = required_config()
    del payload["skills"]["operator"]
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfigError, match="skills.operator is required"):
        load_config(config_path)


def test_enabled_github_requires_login_and_boards(tmp_path: Path) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        github={
            "enabled": True,
            "login": "alice",
            "boards": [],
        },
    )

    with pytest.raises(ConfigError, match="github.boards must be a non-empty list"):
        load_config(config_path)


def test_enabled_github_rejects_legacy_project_keys(tmp_path: Path) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        github={
            "enabled": True,
            "login": "alice",
            "owner": "example",
            "boards": [
                {
                    "owner": "example",
                    "owner_type": "user",
                    "project_number": 1,
                    "status_field": "Status",
                    "priority_field": "Priority",
                }
            ],
        },
    )

    with pytest.raises(ConfigError, match="unknown configuration key"):
        load_config(config_path)


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"skills": {"sources": ["relative"]}}, "absolute path"),
        (
            {"skills": {"language_specific": ["/tmp/language_specific/japanese.md"]}},
            "unknown configuration key",
        ),
        ({"orca": {"unknown": "codex"}}, "unknown configuration key"),
        ({"github": {"enabled": "true"}}, "must be a boolean"),
        ({"queue": {"observer": "true"}}, "must be a boolean"),
        ({"queue": {"resources": [""]}}, "non-empty string"),
    ],
)
def test_invalid_configuration_is_rejected(overrides: dict, message: str, tmp_path: Path) -> None:
    config_path = tmp_path / "config.jsonc"
    config_path.write_text(json.dumps(required_config(**overrides)), encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        load_config(config_path)


def test_reconcile_exclude_worktrees_parses_string_list(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path / "config.jsonc",
        reconcile={"exclude_worktrees": ["flybridge", "vendor-checkout", "flybridge"]},
    )

    config = load_config(config_path)

    assert config.reconcile.exclude_worktrees == ("flybridge", "vendor-checkout")


def test_github_skip_repositories_parses_string_list(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path / "config.jsonc",
        github={
            **ENABLED_GITHUB,
            "skip_repositories": [
                "flybridge-review-fixture/",
                "flybridge-acceptance/",
                "flybridge-review-fixture/",
            ],
        },
    )

    config = load_config(config_path)

    assert config.github.skip_repositories == (
        "flybridge-review-fixture/",
        "flybridge-acceptance/",
    )


def test_reconcile_exclude_worktrees_must_be_a_string_list(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path / "config.jsonc",
        reconcile={"exclude_worktrees": "flybridge"},
    )

    with pytest.raises(ConfigError, match="exclude_worktrees must be a list"):
        load_config(config_path)


def test_orca_agents_accept_strings_objects_and_reviewer_lists(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path / "config.jsonc",
        orca={
            "agents": {
                "single": "cursor",
                "manager": {"agent": "cursor", "model": "auto"},
                "worker": {"agent": "cursor", "model": None},
                "reviewer": [
                    {"agent": "cursor", "model": "auto"},
                    {"agent": "cursor", "model": "auto"},
                    {"agent": "ollama", "model": "gemma4:26b"},
                ],
            }
        },
    )

    config = load_config(config_path)

    assert config.agent_spec("single").agent == "cursor"
    assert config.agent_spec("single").model is None
    assert config.agent_spec("manager") == AgentSpec("cursor", "auto")
    assert config.agent_spec("worker").model is None
    assert len(config.agent_specs("reviewer")) == 3
    assert config.agent_spec("reviewer", 2) == AgentSpec("ollama", "gemma4:26b")


def test_non_reviewer_agent_lists_and_empty_reviewers_are_rejected(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path / "config.jsonc",
        orca={"agents": {"worker": ["cursor", "codex"]}},
    )
    with pytest.raises(ConfigError, match="must be a string or object"):
        load_config(config_path)

    write_config(tmp_path / "config.jsonc", orca={"agents": {"reviewer": []}})
    with pytest.raises(ConfigError, match="at least one agent"):
        load_config(tmp_path / "config.jsonc")


def test_default_agent_launch_presets_are_loaded_from_packaged_file(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path / "flybridge.json", orca={"agents": {}}))

    cursor = config.agent_launch_presets["cursor"]
    codex = config.agent_launch_presets["codex"]
    assert cursor.arguments == ("--trust", "--yolo")
    assert cursor.model_arguments == ("--model", "{model}")
    assert codex.arguments == ("exec", "--dangerously-bypass-approvals-and-sandbox")
    assert codex.prompt_arguments == ("{prompt}",)


def test_agent_launch_override_replaces_one_tracked_preset(tmp_path: Path) -> None:
    override_path = tmp_path / "local-launch.json"
    override_path.write_text(
        json.dumps(
            {
                "version": 1,
                "agents": {
                    "cursor": {
                        "executables": ["custom-cursor"],
                        "arguments": ["--trust", "--yolo"],
                        "model_arguments": ["--model", "{model}"],
                        "builtin_tui": True,
                        "builtin_models": ["auto"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    config = load_config(
        write_config(
            tmp_path / "flybridge.json",
            orca={"agents": {}, "launch_overrides": str(override_path)},
        )
    )

    assert config.agent_launch_presets["cursor"].executables == ("custom-cursor",)
    assert config.agent_launch_presets["cursor"].arguments == ("--trust", "--yolo")
    assert "ollama" in config.agent_launch_presets


def test_delivery_force_push_is_rejected(tmp_path: Path) -> None:
    config_path = write_config(tmp_path / "config.jsonc", delivery={"force_push": True})
    with pytest.raises(ConfigError, match="force_push"):
        load_config(config_path)


def test_timeout_and_delivery_defaults_can_be_overridden(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path / "config.jsonc",
        queue={"observer": False, "resources": [], "wait_timeout_seconds": 10},
        timeouts={"role_seconds": 20},
    )
    config = load_config(config_path)
    assert config.queue_wait_timeout_seconds == 10
    assert config.role_timeout_seconds == 20
