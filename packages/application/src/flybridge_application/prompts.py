from __future__ import annotations

import unicodedata
from pathlib import Path

from flybridge_core import ConfigError, WorkflowMode, WorkflowRole

ROLE_RESPONSIBILITIES = {
    WorkflowRole.MANAGER: (
        "Plan the work and provide a concise implementation handoff after completion. "
        "Do not implement the plan, create agents, delegate work, or start another orchestration; "
        "Flybridge launches the worker after this role is explicitly completed."
    ),
    WorkflowRole.WORKER: "Implement the approved plan and provide concise verification evidence for review.",
    WorkflowRole.REVIEWER: "Review the implementation and report an approval or requested changes.",
    WorkflowRole.SINGLE: "Complete the objective directly and report the result.",
}

ROLE_BRANCH_CONTINUITY = {
    WorkflowRole.MANAGER: (
        "Flybridge creates the next role's worktree from this branch, so only committed work "
        "reaches that role. Commit every file this role produces for the next one, and leave no "
        "other modified or untracked file behind."
    ),
    WorkflowRole.WORKER: (
        "Flybridge creates the reviewer's worktree from this branch, so only committed work "
        "reaches the review. Commit the implementation on this branch before this role is "
        "completed, and leave no other modified or untracked file behind."
    ),
    WorkflowRole.REVIEWER: (
        "This worktree was created from the worker's branch, so the implementation under review "
        "is the commit history of this branch rather than uncommitted files."
    ),
}


def _queue_cli(config_path: Path | None) -> str:
    if config_path is None:
        return "flybridge"
    text = str(config_path)
    if "\n" in text or any(unicodedata.category(character) == "Cc" for character in text):
        raise ConfigError("configured configuration path contains unsafe control characters")
    return f"flybridge --config {text}"


def validate_skill_paths(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    """Reject missing skill documents before an external workflow is created."""
    unsafe = [
        path
        for path in paths
        if any(unicodedata.category(character) == "Cc" for character in str(path))
    ]
    if unsafe:
        raise ConfigError("configured external skill paths contain unsafe control characters")
    resolved = tuple(dict.fromkeys(path.expanduser().resolve() for path in paths))
    missing = [path for path in resolved if not path.is_file()]
    if missing:
        raise ConfigError("configured external skill paths do not exist")
    return resolved


def render_start_prompt(
    *,
    objective: str,
    mode: WorkflowMode,
    role: WorkflowRole,
    response_language: str,
    skill_paths: tuple[Path, ...],
    workflow_id: str = "",
    resource_names: tuple[str, ...] = (),
    config_path: Path | None = None,
    handoff_summary: str | None = None,
) -> str:
    """Render the stable English prompt without copying private skill content."""
    if not objective.strip():
        raise ValueError("workflow objective is required")
    if not response_language.strip() or "\n" in response_language:
        raise ConfigError("skills.response_language must be a single-line string")
    if handoff_summary is not None and (not handoff_summary.strip() or "\n" in handoff_summary):
        raise ValueError("handoff summary must be a non-empty single line")
    instructions = "\n".join(f"- {path}" for path in skill_paths)
    if not instructions:
        instructions = "- No external skill documents are configured."
    handoff = (
        f"Previous-role handoff:\n{handoff_summary}\n\n" if handoff_summary is not None else ""
    )
    resource_coordination = ""
    if resource_names:
        if not workflow_id:
            raise ValueError("workflow identifier is required for resource coordination")
        resources = ", ".join(f"`{name}`" for name in resource_names)
        cli = _queue_cli(config_path)
        resource_coordination = f"""
Resource coordination:
- The following names identify mutually exclusive resources: {resources}.
- Before using one, run `{cli} queue acquire <resource> --owner {workflow_id}`.
- If the request is waiting, run `{cli} queue inspect <request-id> --owner {workflow_id}` until it is leased.
- Release the exact lease with `{cli} queue release <resource> --lease <lease-id> --owner {workflow_id}` immediately after the operation.
- Queue cancellation and stale recovery are operator-only CLI actions.
"""
    continuity = ROLE_BRANCH_CONTINUITY.get(role)
    branch_continuity = f"\nBranch continuity: {continuity}\n" if continuity else ""
    return f"""You are the {role} role in a {mode} Flybridge workflow.

Role responsibility: {ROLE_RESPONSIBILITIES[role]}
{branch_continuity}
Objective:
{objective}

{handoff}Read and follow the external skill documents listed below when they apply. Their
paths are local configuration; do not copy their contents into repository files
unless the user explicitly requests it.

External skill document index:
{instructions}
{resource_coordination}

Respond in the language named: {response_language}.
"""


def render_resume_prompt(
    *,
    objective: str,
    role: WorkflowRole,
    response_language: str,
    skill_paths: tuple[Path, ...],
    workflow_id: str,
    resource_names: tuple[str, ...] = (),
    config_path: Path | None = None,
) -> str:
    """Render concise restart context without embedding private skill content."""
    if not objective.strip():
        raise ValueError("workflow objective is required")
    if not response_language.strip() or "\n" in response_language:
        raise ConfigError("skills.response_language must be a single-line string")
    skills = (
        ", ".join(str(path) for path in skill_paths)
        if skill_paths
        else "no external skill documents"
    )
    resources = (
        " Before using these mutually exclusive resources, acquire, inspect, and release them "
        f"through `{_queue_cli(config_path)} queue` with owner `{workflow_id}`: "
        f"{', '.join(resource_names)}."
        if resource_names
        else ""
    )
    continuity = ROLE_BRANCH_CONTINUITY.get(role)
    branch_continuity = f" Branch continuity: {continuity}" if continuity else ""
    return (
        f"Resume the persisted Flybridge workflow as the {role.value} role. "
        f"Continue this objective: {objective.strip()} "
        f"Role responsibility: {ROLE_RESPONSIBILITIES[role]}{branch_continuity} "
        f"Read applicable configured skill documents from: {skills}.{resources} "
        f"Respond in {response_language}."
    )
