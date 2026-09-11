from __future__ import annotations

import glob as glob_module
import json
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .types import WorkflowMode, WorkflowRole


class ConfigError(ValueError):
    """Raised for missing or invalid user configuration."""


ROLES = frozenset(WorkflowRole)
_INVALID_COMMA_PREDECESSORS = frozenset({"", "{", "[", ",", ":"})
_GLOB_MARKERS = frozenset("*?[")


def _string_literal_end(text: str, start: int) -> int:
    """Return the index just past the JSON string literal opened at ``start``."""
    index = start + 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == '"':
            return index + 1
        index += 1
    return len(text)


def _strip_comments(text: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if char == '"':
            end = _string_literal_end(text, index)
            output.append(text[index:end])
            index = end
        elif char == "/" and next_char == "/":
            index = text.find("\n", index)
            if index < 0:
                break
        elif char == "/" and next_char == "*":
            end = text.find("*/", index + 2)
            if end < 0:
                raise ConfigError("unterminated JSONC block comment")
            output.extend(character for character in text[index:end] if character in "\r\n")
            index = end + 2
        else:
            output.append(char)
            index += 1
    return "".join(output)


def _strip_trailing_commas(text: str) -> str:
    """Drop a comma before ] or } only where a complete value precedes it."""
    output: list[str] = []
    index = 0
    previous = ""
    while index < len(text):
        char = text[index]
        if char == '"':
            end = _string_literal_end(text, index)
            output.append(text[index:end])
            previous = char
            index = end
            continue
        if char == "," and previous not in _INVALID_COMMA_PREDECESSORS:
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "}]":
                index += 1
                continue
        if not char.isspace():
            previous = char
        output.append(char)
        index += 1
    return "".join(output)


def strip_jsonc(text: str) -> str:
    """Remove JSONC comments and trailing commas without touching quoted strings."""
    return _strip_trailing_commas(_strip_comments(text))


def _required(container: dict[str, Any], key: str, field: str) -> Any:
    if key not in container:
        raise ConfigError(f"{field} is required")
    return container[key]


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{field} must be an object")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} must be a non-empty string")
    return value


def _keys(value: dict[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(value).difference(allowed))
    if unknown:
        raise ConfigError(f"unknown configuration key in {field}: {', '.join(unknown)}")


def _reject_control_characters(text: str, field: str) -> None:
    if any(unicodedata.category(character) == "Cc" for character in text):
        raise ConfigError(f"{field} contains unsafe control characters")


def _paths(values: Any, field: str) -> tuple[Path, ...]:
    if not isinstance(values, list):
        raise ConfigError(f"{field} must be a list of absolute paths")
    paths: list[Path] = []
    for index, value in enumerate(values):
        text = _string(value, f"{field}[{index}]")
        _reject_control_characters(text, f"{field}[{index}]")
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            raise ConfigError(f"{field}[{index}] must be an absolute path")
        paths.append(candidate)
    return tuple(paths)


def _has_glob(path: Path) -> bool:
    return any(character in str(path) for character in _GLOB_MARKERS)


def _catalog_documents(root: Path, field: str) -> tuple[Path, ...]:
    resolved_root = root.resolve()
    catalog = sorted(resolved_root.rglob("SKILL.md"))
    if not catalog:
        raise ConfigError(f"skill catalog contains no SKILL.md files: {root}")
    documents: list[Path] = []
    for path in catalog:
        resolved = path.resolve()
        if not resolved.is_file():
            raise ConfigError(f"{field} is not a skill document: {path}")
        if not resolved.is_relative_to(resolved_root):
            raise ConfigError(f"skill catalog entry escapes configured root: {path}")
        documents.append(resolved)
    return tuple(documents)


def _documents_from_entry(path: Path, field: str) -> tuple[Path, ...]:
    if _has_glob(path):
        matches = sorted(Path(match) for match in glob_module.glob(str(path), recursive=True))
        if not matches:
            raise ConfigError(f"{field} matched no paths")
        documents: list[Path] = []
        for match in matches:
            documents.extend(_documents_from_entry(match, field))
        return tuple(documents)
    resolved = path.resolve()
    if resolved.is_file():
        return (resolved,)
    if resolved.is_dir():
        return _catalog_documents(resolved, field)
    raise ConfigError(f"{field} does not exist: {path}")


def _state_dir(value: Any, config_path: Path) -> Path:
    text = _string(value, "state_dir")
    _reject_control_characters(text, "state_dir")
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = config_path.parent / candidate
    return candidate.resolve()


def _response_language(value: Any) -> str:
    language = _string(value, "skills.response_language")
    if "\n" in language or "\r" in language:
        raise ConfigError("skills.response_language must be a single-line string")
    return language


@dataclass(frozen=True)
class GitHubConfig:
    enabled: bool
    owner: str = ""
    owner_type: str = "user"
    project_number: int = 0
    status_field: str = "Status"
    todo_status: str = "Todo"
    priority_field: str = "Priority"
    priority_values: tuple[str, ...] = ()


@dataclass(frozen=True)
class AppConfig:
    path: Path
    state_dir: Path
    default_mode: WorkflowMode
    orca_executable: str
    orca_agents: dict[WorkflowRole, str]
    skill_sources: tuple[Path, ...]
    role_sources: dict[str, tuple[Path, ...]]
    response_language: str
    language_specific: tuple[Path, ...]
    queue_observer: bool
    queue_resources: tuple[str, ...]
    github: GitHubConfig

    def skill_paths_for(self, role: WorkflowRole | str) -> tuple[Path, ...]:
        try:
            role = WorkflowRole(role)
        except ValueError as exc:
            raise ConfigError(f"unknown role: {role}") from exc
        if role not in ROLES:
            raise ConfigError(f"unknown role: {role}")
        documents: list[Path] = []
        for index, source in enumerate(self.skill_sources):
            documents.extend(_documents_from_entry(source, f"skills.sources[{index}]"))
        for index, path in enumerate(self.role_sources.get(role, ())):
            documents.extend(_documents_from_entry(path, f"skills.roles.{role}[{index}]"))
        for index, path in enumerate(self.language_specific):
            documents.extend(_documents_from_entry(path, f"skills.language_specific[{index}]"))
        return tuple(dict.fromkeys(documents))

    def validate_skill_paths(self) -> tuple[Path, ...]:
        """Validate and expand every configured role's effective skill index."""
        return tuple(
            dict.fromkeys(path for role in WorkflowRole for path in self.skill_paths_for(role))
        )


def load_config(path: Path) -> AppConfig:
    config_path = path.expanduser().resolve()
    try:
        raw = json.loads(strip_jsonc(config_path.read_text(encoding="utf-8")))
    except OSError as exc:
        example = config_path.with_name(f"{config_path.name}.example")
        guidance = f"; copy {example} to {config_path}" if example.is_file() else ""
        raise ConfigError(f"cannot read configuration: {path}{guidance}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSONC: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("top-level configuration must be an object")
    _keys(raw, {"default_mode", "state_dir", "orca", "skills", "queue", "github"}, "root")
    mode_raw = _required(raw, "default_mode", "default_mode")
    try:
        mode = WorkflowMode(mode_raw)
    except ValueError as exc:
        raise ConfigError("default_mode must be 'single' or 'orchestrated'") from exc
    orca = _object(_required(raw, "orca", "orca"), "orca")
    _keys(orca, {"executable", "agents"}, "orca")
    agents = _object(_required(orca, "agents", "orca.agents"), "orca.agents")
    _keys(agents, set(ROLES), "orca.agents")
    default_executable = "orca-ide" if sys.platform.startswith("linux") else "orca"
    executable = _string(orca.get("executable", default_executable), "orca.executable")
    configured_agents = {
        WorkflowRole(role): _string(agent, f"orca.agents.{role}") for role, agent in agents.items()
    }
    skills = _object(_required(raw, "skills", "skills"), "skills")
    _keys(
        skills,
        {"sources", "roles", "response_language", "language_specific"},
        "skills",
    )
    sources = _required(skills, "sources", "skills.sources")
    roles = _object(_required(skills, "roles", "skills.roles"), "skills.roles")
    _keys(roles, set(ROLES), "skills.roles")
    response_language = _response_language(
        _required(skills, "response_language", "skills.response_language")
    )
    language_specific = _required(skills, "language_specific", "skills.language_specific")
    queue_raw = _object(_required(raw, "queue", "queue"), "queue")
    _keys(queue_raw, {"observer", "resources"}, "queue")
    queue_observer = _required(queue_raw, "observer", "queue.observer")
    if type(queue_observer) is not bool:
        raise ConfigError("queue.observer must be a boolean")
    resources_raw = _required(queue_raw, "resources", "queue.resources")
    if not isinstance(resources_raw, list):
        raise ConfigError("queue.resources must be a list of strings")
    queue_resources = tuple(
        dict.fromkeys(
            _string(resource, f"queue.resources[{index}]").strip()
            for index, resource in enumerate(resources_raw)
        )
    )
    github_raw = _object(_required(raw, "github", "github"), "github")
    _keys(
        github_raw,
        {
            "enabled",
            "owner",
            "owner_type",
            "project_number",
            "status_field",
            "todo_status",
            "priority_field",
            "priority_values",
        },
        "github",
    )
    enabled = _required(github_raw, "enabled", "github.enabled")
    if type(enabled) is not bool:
        raise ConfigError("github.enabled must be a boolean")
    if enabled:
        for field in (
            "owner",
            "owner_type",
            "project_number",
            "status_field",
            "todo_status",
            "priority_field",
            "priority_values",
        ):
            _required(github_raw, field, f"github.{field}")
        project_number = github_raw["project_number"]
        if type(project_number) is not int or project_number < 1:
            raise ConfigError("github.project_number must be a positive integer")
        owner_type = _string(github_raw["owner_type"], "github.owner_type")
        if owner_type not in {"user", "organization"}:
            raise ConfigError("github.owner_type must be 'user' or 'organization'")
        priority_values_raw = github_raw["priority_values"]
        if not isinstance(priority_values_raw, list):
            raise ConfigError("github.priority_values must be a list of strings")
        if not priority_values_raw:
            raise ConfigError("github.priority_values must not be empty when GitHub is enabled")
        github = GitHubConfig(
            enabled=True,
            owner=_string(github_raw["owner"], "github.owner"),
            owner_type=owner_type,
            project_number=project_number,
            status_field=_string(github_raw["status_field"], "github.status_field"),
            todo_status=_string(github_raw["todo_status"], "github.todo_status"),
            priority_field=_string(github_raw["priority_field"], "github.priority_field"),
            priority_values=tuple(
                _string(value, f"github.priority_values[{index}]")
                for index, value in enumerate(priority_values_raw)
            ),
        )
    else:
        github = GitHubConfig(enabled=False)
    state_dir = raw.get("state_dir", "~/.local/state/flybridge")
    return AppConfig(
        path=config_path,
        state_dir=_state_dir(state_dir, config_path),
        default_mode=mode,
        orca_executable=executable,
        orca_agents=configured_agents,
        skill_sources=_paths(sources, "skills.sources"),
        role_sources={
            role: _paths(values, f"skills.roles.{role}") for role, values in roles.items()
        },
        response_language=response_language,
        language_specific=_paths(language_specific, "skills.language_specific"),
        queue_observer=queue_observer,
        queue_resources=queue_resources,
        github=github,
    )
