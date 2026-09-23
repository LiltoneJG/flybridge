from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ConfigError

_ALLOWED_PLACEHOLDERS = frozenset({"{agent}", "{model}", "{prompt}"})
DEFAULT_LAUNCH_PRESETS = Path(__file__).with_name("agent-launch-presets.json")


@dataclass(frozen=True)
class AgentLaunchPreset:
    executables: tuple[str, ...]
    arguments: tuple[str, ...]
    model_arguments: tuple[str, ...]
    prompt_arguments: tuple[str, ...] = ()
    requires_model: bool = False
    builtin_tui: bool = True
    builtin_models: tuple[str, ...] = ()


def _strings(value: Any, field: str, *, nonempty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (nonempty and not value):
        requirement = "a non-empty list" if nonempty else "a list"
        raise ConfigError(f"{field} must be {requirement} of strings")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise ConfigError(f"{field}[{index}] must be a non-empty string")
        if any(unicodedata.category(character) == "Cc" for character in item):
            raise ConfigError(f"{field}[{index}] contains unsafe control characters")
        result.append(item)
    return tuple(result)


def _preset(value: Any, field: str) -> AgentLaunchPreset:
    if not isinstance(value, dict):
        raise ConfigError(f"{field} must be an object")
    allowed = {
        "executables",
        "arguments",
        "model_arguments",
        "prompt_arguments",
        "requires_model",
        "builtin_tui",
        "builtin_models",
    }
    unknown = sorted(set(value).difference(allowed))
    if unknown:
        raise ConfigError(f"unknown configuration key in {field}: {', '.join(unknown)}")
    requires_model = value.get("requires_model", False)
    builtin_tui = value.get("builtin_tui", True)
    if type(requires_model) is not bool:
        raise ConfigError(f"{field}.requires_model must be a boolean")
    if type(builtin_tui) is not bool:
        raise ConfigError(f"{field}.builtin_tui must be a boolean")
    preset = AgentLaunchPreset(
        executables=_strings(value.get("executables"), f"{field}.executables", nonempty=True),
        arguments=_strings(value.get("arguments", []), f"{field}.arguments"),
        model_arguments=_strings(value.get("model_arguments", []), f"{field}.model_arguments"),
        prompt_arguments=_strings(value.get("prompt_arguments", []), f"{field}.prompt_arguments"),
        requires_model=requires_model,
        builtin_tui=builtin_tui,
        builtin_models=_strings(value.get("builtin_models", []), f"{field}.builtin_models"),
    )
    tokens = (
        *preset.executables,
        *preset.arguments,
        *preset.model_arguments,
        *preset.prompt_arguments,
    )
    for token in tokens:
        placeholders = {part for part in _ALLOWED_PLACEHOLDERS if part in token}
        remainder = token
        for placeholder in placeholders:
            remainder = remainder.replace(placeholder, "")
        if "{" in remainder or "}" in remainder:
            raise ConfigError(f"{field} contains an unknown placeholder")
    if "{model}" not in preset.model_arguments and preset.model_arguments:
        raise ConfigError(f"{field}.model_arguments must contain {{model}}")
    return preset


def _document(path: Path, *, optional: bool) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if optional:
            return {}
        raise ConfigError(f"cannot read agent launch presets: {path}") from None
    except OSError as exc:
        raise ConfigError(f"cannot read agent launch presets: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid agent launch preset JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"agent launch preset document must be an object: {path}")
    unknown = sorted(set(value).difference({"version", "agents"}))
    if unknown:
        raise ConfigError(f"unknown agent launch preset key: {', '.join(unknown)}")
    if value.get("version") != 1:
        raise ConfigError(f"agent launch preset version must be 1: {path}")
    agents = value.get("agents")
    if not isinstance(agents, dict) or not agents:
        raise ConfigError(f"agent launch presets must contain agents: {path}")
    return agents


def load_agent_launch_presets(
    preset_path: Path = DEFAULT_LAUNCH_PRESETS,
    override_path: Path | None = None,
) -> dict[str, AgentLaunchPreset]:
    raw = _document(preset_path, optional=False)
    if override_path is not None:
        raw = {**raw, **_document(override_path, optional=True)}
    presets = {
        name: _preset(value, f"agent launch preset {name}")
        for name, value in raw.items()
        if isinstance(name, str) and name
    }
    if "default" not in presets:
        raise ConfigError("agent launch presets must define default")
    return presets
