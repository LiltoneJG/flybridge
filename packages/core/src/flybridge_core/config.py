from __future__ import annotations

import glob as glob_module
import json
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .types import WorkflowMode, WorkflowRole

if TYPE_CHECKING:
    from .launch_presets import AgentLaunchPreset


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


def _positive_int(value: Any, field: str) -> int:
    if type(value) is not int or value < 1:
        raise ConfigError(f"{field} must be a positive integer")
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
class GitHubBoard:
    owner: str
    owner_type: str
    project_number: int
    status_field: str = "Status"
    priority_field: str = "Priority"


@dataclass(frozen=True)
class GitHubConfig:
    enabled: bool
    login: str = ""
    boards: tuple[GitHubBoard, ...] = ()
    skip_repositories: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReconcileConfig:
    auto_before_workflow_commands: bool = True
    missing_observations: int = 2
    missing_grace_seconds: int = 300
    event_retention: int = 10_000
    exclude_worktrees: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentSpec:
    agent: str
    model: str | None = None


@dataclass(frozen=True)
class AppConfig:
    path: Path
    state_dir: Path
    default_mode: WorkflowMode
    orca_executable: str
    orca_agents: dict[WorkflowRole, tuple[AgentSpec, ...]]
    agent_launch_presets: dict[str, AgentLaunchPreset]
    skill_sources: tuple[Path, ...]
    role_sources: dict[str, tuple[Path, ...]]
    operator_sources: tuple[Path, ...]
    response_language: str
    queue_observer: bool
    queue_resources: tuple[str, ...]
    queue_wait_timeout_seconds: int
    queue_lease_timeout_seconds: int
    role_timeout_seconds: int
    delivery_orchestrated: str
    delivery_force_push: bool
    max_review_cycles: int
    max_coordinator_errors: int
    coordinator_retry_initial_seconds: float
    coordinator_retry_max_seconds: float
    github: GitHubConfig
    reconcile: ReconcileConfig = ReconcileConfig()

    def agent_specs(self, role: WorkflowRole | str) -> tuple[AgentSpec, ...]:
        try:
            role = WorkflowRole(role)
        except ValueError as exc:
            raise ConfigError(f"unknown role: {role}") from exc
        return self.orca_agents.get(role, ())

    def agent_spec(self, role: WorkflowRole | str, slot: int = 0) -> AgentSpec:
        specs = self.agent_specs(role)
        if not specs:
            raise ConfigError(f"orca.agents.{role} is required")
        if type(slot) is not int or slot < 0 or slot >= len(specs):
            raise ConfigError(f"orca.agents.{role} has no agent for slot {slot}")
        return specs[slot]

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
        return tuple(dict.fromkeys(documents))

    def validate_skill_paths(self) -> tuple[Path, ...]:
        """Validate and expand every configured role's effective skill index."""
        return tuple(
            dict.fromkeys(path for role in WorkflowRole for path in self.skill_paths_for(role))
        )

    def operator_skill_paths(self) -> tuple[Path, ...]:
        """Expand the private guidance read by the workflow operator."""
        documents: list[Path] = []
        for index, source in enumerate(self.operator_sources):
            documents.extend(_documents_from_entry(source, f"skills.operator[{index}]"))
        return tuple(dict.fromkeys(documents))


def _agent_model(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} must be a non-empty string or null")
    return value.strip()


def _agent_spec(value: Any, field: str) -> AgentSpec:
    if isinstance(value, str):
        return AgentSpec(agent=_string(value, field), model=None)
    spec = _object(value, field)
    _keys(spec, {"agent", "model"}, field)
    return AgentSpec(
        agent=_string(_required(spec, "agent", f"{field}.agent"), f"{field}.agent"),
        model=_agent_model(spec.get("model"), f"{field}.model"),
    )


def _agent_specs(value: Any, field: str, *, allow_multiple: bool) -> tuple[AgentSpec, ...]:
    if isinstance(value, list):
        if not allow_multiple:
            raise ConfigError(f"{field} must be a string or object")
        if not value:
            raise ConfigError(f"{field} must contain at least one agent")
        return tuple(_agent_spec(item, f"{field}[{index}]") for index, item in enumerate(value))
    return (_agent_spec(value, field),)


def _config_relative_path(value: Any, field: str, config_path: Path) -> Path:
    text = _string(value, field)
    _reject_control_characters(text, field)
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = config_path.parent / path
    return path.resolve()


def _github_skip_repositories(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ConfigError("github.skip_repositories must be a list of strings")
    return tuple(
        dict.fromkeys(
            _string(pattern, f"github.skip_repositories[{index}]").strip()
            for index, pattern in enumerate(value)
        )
    )


def _github_board(value: Any, field: str) -> GitHubBoard:
    board = _object(value, field)
    _keys(
        board,
        {"owner", "owner_type", "project_number", "status_field", "priority_field"},
        field,
    )
    project_number = _required(board, "project_number", f"{field}.project_number")
    if type(project_number) is not int or project_number < 1:
        raise ConfigError(f"{field}.project_number must be a positive integer")
    owner_type = _string(
        _required(board, "owner_type", f"{field}.owner_type"), f"{field}.owner_type"
    )
    if owner_type not in {"user", "organization"}:
        raise ConfigError(f"{field}.owner_type must be 'user' or 'organization'")
    return GitHubBoard(
        owner=_string(_required(board, "owner", f"{field}.owner"), f"{field}.owner"),
        owner_type=owner_type,
        project_number=project_number,
        status_field=_string(
            _required(board, "status_field", f"{field}.status_field"), f"{field}.status_field"
        ),
        priority_field=_string(
            _required(board, "priority_field", f"{field}.priority_field"),
            f"{field}.priority_field",
        ),
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
    _keys(
        raw,
        {
            "default_mode",
            "state_dir",
            "orca",
            "skills",
            "queue",
            "timeouts",
            "delivery",
            "orchestration",
            "github",
            "reconcile",
        },
        "root",
    )
    mode_raw = _required(raw, "default_mode", "default_mode")
    try:
        mode = WorkflowMode(mode_raw)
    except ValueError as exc:
        raise ConfigError("default_mode must be 'single' or 'orchestrated'") from exc
    orca = _object(_required(raw, "orca", "orca"), "orca")
    _keys(orca, {"executable", "agents", "launch_presets", "launch_overrides"}, "orca")
    agents = _object(_required(orca, "agents", "orca.agents"), "orca.agents")
    _keys(agents, set(ROLES), "orca.agents")
    default_executable = "orca-ide" if sys.platform.startswith("linux") else "orca"
    executable = _string(orca.get("executable", default_executable), "orca.executable")
    configured_agents = {
        WorkflowRole(role): _agent_specs(
            agent, f"orca.agents.{role}", allow_multiple=role == WorkflowRole.REVIEWER.value
        )
        for role, agent in agents.items()
    }
    from .launch_presets import DEFAULT_LAUNCH_PRESETS, load_agent_launch_presets

    preset_path = (
        _config_relative_path(orca["launch_presets"], "orca.launch_presets", config_path)
        if "launch_presets" in orca
        else DEFAULT_LAUNCH_PRESETS
    )
    override_path = (
        _config_relative_path(orca["launch_overrides"], "orca.launch_overrides", config_path)
        if "launch_overrides" in orca
        else config_path.parent / "agent-launch-overrides.json"
    )
    agent_launch_presets = load_agent_launch_presets(preset_path, override_path)
    skills = _object(_required(raw, "skills", "skills"), "skills")
    _keys(
        skills,
        {"sources", "roles", "operator", "response_language"},
        "skills",
    )
    sources = _required(skills, "sources", "skills.sources")
    roles = _object(_required(skills, "roles", "skills.roles"), "skills.roles")
    _keys(roles, set(ROLES), "skills.roles")
    operator = _required(skills, "operator", "skills.operator")
    response_language = _response_language(
        _required(skills, "response_language", "skills.response_language")
    )
    queue_raw = _object(_required(raw, "queue", "queue"), "queue")
    _keys(
        queue_raw,
        {"observer", "resources", "wait_timeout_seconds", "lease_timeout_seconds"},
        "queue",
    )
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
    queue_wait_timeout_seconds = _positive_int(
        queue_raw.get("wait_timeout_seconds", 3600), "queue.wait_timeout_seconds"
    )
    queue_lease_timeout_seconds = _positive_int(
        queue_raw.get("lease_timeout_seconds", 3600), "queue.lease_timeout_seconds"
    )
    timeouts_raw = _object(raw.get("timeouts", {}), "timeouts")
    _keys(timeouts_raw, {"role_seconds"}, "timeouts")
    role_timeout_seconds = _positive_int(
        timeouts_raw.get("role_seconds", 3600), "timeouts.role_seconds"
    )
    delivery_raw = _object(raw.get("delivery", {}), "delivery")
    _keys(delivery_raw, {"orchestrated", "force_push"}, "delivery")
    delivery_orchestrated = delivery_raw.get("orchestrated", "manager_worktree")
    if delivery_orchestrated != "manager_worktree":
        raise ConfigError("delivery.orchestrated must be 'manager_worktree'")
    delivery_force_push = delivery_raw.get("force_push", False)
    if type(delivery_force_push) is not bool:
        raise ConfigError("delivery.force_push must be a boolean")
    if delivery_force_push:
        raise ConfigError("delivery.force_push is not supported")
    orchestration_raw = _object(raw.get("orchestration", {}), "orchestration")
    _keys(
        orchestration_raw,
        {
            "max_review_cycles",
            "max_coordinator_errors",
            "retry_initial_seconds",
            "retry_max_seconds",
        },
        "orchestration",
    )
    max_review_cycles = orchestration_raw.get("max_review_cycles", 3)
    if type(max_review_cycles) is not int or max_review_cycles < 1:
        raise ConfigError("orchestration.max_review_cycles must be a positive integer")
    max_coordinator_errors = orchestration_raw.get("max_coordinator_errors", 5)
    if type(max_coordinator_errors) is not int or max_coordinator_errors < 1:
        raise ConfigError("orchestration.max_coordinator_errors must be a positive integer")
    retry_initial_seconds = orchestration_raw.get("retry_initial_seconds", 1.0)
    retry_max_seconds = orchestration_raw.get("retry_max_seconds", 30.0)
    for value, field in (
        (retry_initial_seconds, "orchestration.retry_initial_seconds"),
        (retry_max_seconds, "orchestration.retry_max_seconds"),
    ):
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            raise ConfigError(f"{field} must be a positive number")
    if retry_max_seconds < retry_initial_seconds:
        raise ConfigError(
            "orchestration.retry_max_seconds must be greater than or equal to retry_initial_seconds"
        )
    github_raw = _object(_required(raw, "github", "github"), "github")
    _keys(github_raw, {"enabled", "login", "boards", "skip_repositories"}, "github")
    enabled = _required(github_raw, "enabled", "github.enabled")
    if type(enabled) is not bool:
        raise ConfigError("github.enabled must be a boolean")
    skip_repositories = _github_skip_repositories(github_raw.get("skip_repositories", []))
    if enabled:
        login = _string(_required(github_raw, "login", "github.login"), "github.login")
        boards_raw = _required(github_raw, "boards", "github.boards")
        if not isinstance(boards_raw, list) or not boards_raw:
            raise ConfigError("github.boards must be a non-empty list when GitHub is enabled")
        github = GitHubConfig(
            enabled=True,
            login=login,
            boards=tuple(
                _github_board(item, f"github.boards[{index}]")
                for index, item in enumerate(boards_raw)
            ),
            skip_repositories=skip_repositories,
        )
    else:
        github = GitHubConfig(enabled=False, skip_repositories=skip_repositories)
    reconcile_raw = _object(raw.get("reconcile", {}), "reconcile")
    _keys(
        reconcile_raw,
        {
            "auto_before_workflow_commands",
            "missing_observations",
            "missing_grace_seconds",
            "event_retention",
            "exclude_worktrees",
        },
        "reconcile",
    )
    auto_reconcile = reconcile_raw.get("auto_before_workflow_commands", True)
    if type(auto_reconcile) is not bool:
        raise ConfigError("reconcile.auto_before_workflow_commands must be a boolean")
    integer_values = {
        "missing_observations": reconcile_raw.get("missing_observations", 2),
        "missing_grace_seconds": reconcile_raw.get("missing_grace_seconds", 300),
        "event_retention": reconcile_raw.get("event_retention", 10_000),
    }
    for field, value in integer_values.items():
        if type(value) is not int or value < 1:
            raise ConfigError(f"reconcile.{field} must be a positive integer")
    exclude_raw = reconcile_raw.get("exclude_worktrees", [])
    if not isinstance(exclude_raw, list):
        raise ConfigError("reconcile.exclude_worktrees must be a list of strings")
    exclude_worktrees = tuple(
        dict.fromkeys(
            _string(pattern, f"reconcile.exclude_worktrees[{index}]").strip()
            for index, pattern in enumerate(exclude_raw)
        )
    )
    state_dir = raw.get("state_dir", "~/.local/state/flybridge")
    return AppConfig(
        path=config_path,
        state_dir=_state_dir(state_dir, config_path),
        default_mode=mode,
        orca_executable=executable,
        orca_agents=configured_agents,
        agent_launch_presets=agent_launch_presets,
        skill_sources=_paths(sources, "skills.sources"),
        role_sources={
            role: _paths(values, f"skills.roles.{role}") for role, values in roles.items()
        },
        operator_sources=_paths(operator, "skills.operator"),
        response_language=response_language,
        queue_observer=queue_observer,
        queue_resources=queue_resources,
        queue_wait_timeout_seconds=queue_wait_timeout_seconds,
        queue_lease_timeout_seconds=queue_lease_timeout_seconds,
        role_timeout_seconds=role_timeout_seconds,
        delivery_orchestrated=delivery_orchestrated,
        delivery_force_push=delivery_force_push,
        max_review_cycles=max_review_cycles,
        max_coordinator_errors=max_coordinator_errors,
        coordinator_retry_initial_seconds=float(retry_initial_seconds),
        coordinator_retry_max_seconds=float(retry_max_seconds),
        github=github,
        reconcile=ReconcileConfig(
            auto_before_workflow_commands=auto_reconcile,
            exclude_worktrees=exclude_worktrees,
            **integer_values,
        ),
    )
