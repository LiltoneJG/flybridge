from __future__ import annotations

import shlex
import shutil
from collections.abc import Callable
from pathlib import Path

from flybridge_core import AgentLaunchPreset, load_agent_launch_presets

Which = Callable[[str], str | None]


class UnresolvedAgentError(ValueError):
    """The configured TUI id has no launchable CLI binary."""


def _effective_presets(
    presets: dict[str, AgentLaunchPreset] | None,
) -> dict[str, AgentLaunchPreset]:
    return presets if presets is not None else load_agent_launch_presets()


def _preset_for(agent: str, presets: dict[str, AgentLaunchPreset] | None) -> AgentLaunchPreset:
    available = _effective_presets(presets)
    configured = agent.strip()
    if configured in available:
        return available[configured]
    name = Path(configured).name
    for key, preset in available.items():
        if key != "default" and name in preset.executables:
            return preset
    return available["default"]


def uses_builtin_tui(
    agent: str,
    model: str | None = None,
    *,
    presets: dict[str, AgentLaunchPreset] | None = None,
) -> bool:
    """Return whether Orca `worktree create --agent` can launch this spec."""
    text = agent.strip()
    if not text or Path(text).expanduser().is_absolute():
        return False
    preset = _preset_for(text, presets)
    if not preset.builtin_tui or preset.requires_model and model is None:
        return False
    if model is None:
        return True
    return model in preset.builtin_models


def resolve_terminal_command(
    agent: str,
    *,
    presets: dict[str, AgentLaunchPreset] | None = None,
    which: Which = shutil.which,
) -> str:
    """Resolve a configured agent through its external launch preset."""
    text = agent.strip()
    if not text:
        raise UnresolvedAgentError("configured agent command is empty")
    path = Path(text).expanduser()
    if path.is_absolute():
        if path.is_file():
            return str(path.resolve())
        raise UnresolvedAgentError(f"TUI id {text} has no CLI binary")
    preset = _preset_for(text, presets)
    for executable in preset.executables:
        name = executable.replace("{agent}", text)
        found = which(name)
        if found:
            return str(Path(found).expanduser().resolve())
    raise UnresolvedAgentError(f"TUI id {text} has no CLI binary")


def resolve_launch_command(
    agent: str,
    model: str | None = None,
    *,
    presets: dict[str, AgentLaunchPreset] | None = None,
    which: Which = shutil.which,
) -> str:
    """Return the owned-terminal command for a configured agent and optional model."""
    preset = _preset_for(agent, presets)
    if preset.requires_model and model is None:
        raise UnresolvedAgentError(f"{agent.strip()} requires a model")
    resolved = resolve_terminal_command(agent, presets=presets, which=which)
    arguments = [token.replace("{agent}", agent.strip()) for token in preset.arguments]
    if model is not None:
        arguments.extend(token.replace("{model}", model) for token in preset.model_arguments)
    return shlex.join([resolved, *arguments])


def resolve_prompt_command(
    agent: str,
    model: str | None,
    prompt: str,
    *,
    presets: dict[str, AgentLaunchPreset] | None = None,
    which: Which = shutil.which,
) -> str | None:
    """Build a one-shot prompt command when the preset defines one."""
    preset = _preset_for(agent, presets)
    if not preset.prompt_arguments:
        return None
    if preset.requires_model and model is None:
        raise UnresolvedAgentError(f"{agent.strip()} requires a model")
    resolved = resolve_terminal_command(agent, presets=presets, which=which)
    arguments = [token.replace("{agent}", agent.strip()) for token in preset.arguments]
    if model is not None:
        arguments.extend(token.replace("{model}", model) for token in preset.model_arguments)
    arguments.extend(token.replace("{prompt}", prompt) for token in preset.prompt_arguments)
    return shlex.join([resolved, *arguments])


def agent_cli_is_resolvable(
    agent: str,
    *,
    model: str | None = None,
    presets: dict[str, AgentLaunchPreset] | None = None,
    which: Which = shutil.which,
) -> bool:
    """Return whether doctor can treat the configured agent as launchable."""
    try:
        command = resolve_launch_command(agent, model, presets=presets, which=which)
    except UnresolvedAgentError:
        return False
    executable = shlex.split(command)[0]
    if Path(executable).is_absolute():
        return True
    return which(executable) is not None
