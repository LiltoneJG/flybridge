import json
from pathlib import Path

import pytest
from conftest import required_config, write_config
from flybridge_core import ConfigError, load_config


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
            "response_language": "English",
            "language_specific": [],
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
    overlay = tmp_path / "language_specific" / "japanese.md"
    overlay.parent.mkdir()
    for path in (general, role, overlay):
        path.write_text("skill", encoding="utf-8")
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        skills={
            "sources": [general],
            "roles": {"manager": [role]},
            "response_language": "Japanese",
            "language_specific": [overlay],
        },
    )

    config = load_config(config_path)

    assert config.response_language == "Japanese"
    assert config.skill_paths_for("manager") == (general, role, overlay)


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


def test_empty_skill_catalog_and_explicit_directory_are_rejected(tmp_path: Path) -> None:
    empty_catalog = tmp_path / "empty"
    overlay_directory = tmp_path / "overlay"
    empty_catalog.mkdir()
    overlay_directory.mkdir()
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        skills={"sources": [empty_catalog], "language_specific": [overlay_directory]},
    )
    config = load_config(config_path)

    with pytest.raises(ConfigError, match="contains no SKILL.md"):
        config.skill_paths_for("single")

    (empty_catalog / "SKILL.md").write_text("catalog", encoding="utf-8")
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
    overlay = tmp_path / "language_specific" / "japanese.md"
    overlay.parent.mkdir()
    overlay.write_text("overlay", encoding="utf-8")
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        skills={
            "sources": [],
            "roles": {"single": [str(implementing / "*" / "SKILL.md")]},
            "language_specific": [str(overlay.parent / "*.md")],
        },
    )

    assert load_config(config_path).skill_paths_for("single") == (
        first.resolve(),
        second.resolve(),
        overlay.resolve(),
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
            '"roles": {}, "response_language": "English", "language_specific": []}, '
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


def test_enabled_github_requires_a_non_empty_priority_allowlist(tmp_path: Path) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        github={
            "enabled": True,
            "owner": "example",
            "owner_type": "user",
            "project_number": 1,
            "status_field": "Status",
            "todo_status": "Todo",
            "priority_field": "Priority",
            "priority_values": [],
        },
    )

    with pytest.raises(ConfigError, match="priority_values must not be empty"):
        load_config(config_path)


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"skills": {"sources": ["relative"]}}, "absolute path"),
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
