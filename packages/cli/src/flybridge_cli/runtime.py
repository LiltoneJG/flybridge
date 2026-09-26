from __future__ import annotations

import argparse
import shlex
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from flybridge_application import validate_skill_paths
from flybridge_core import (
    ConfigError,
    WorkflowMode,
    WorkflowRole,
    WorkflowStatus,
    WorkflowStore,
    load_config,
)
from flybridge_orca import OrcaClient


def _config(args: argparse.Namespace):
    return load_config(Path(args.config))


def _adapter(config) -> OrcaClient:
    client = OrcaClient(config.orca_executable)
    client.launch_presets = config.agent_launch_presets
    client.verify()
    return client


def _running_owner(store: WorkflowStore, owner: str) -> str:
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError("owner must identify an existing running workflow")
    try:
        workflow = store.get(owner.strip())
    except ValueError as exc:
        raise ValueError("owner must identify an existing running workflow") from exc
    if workflow.status != WorkflowStatus.RUNNING:
        raise ValueError("owner must identify an existing running workflow")
    return workflow.id


def _optional_observer_command(config, workflow_id: str) -> str | None:
    if not config.queue_observer:
        return None
    return _observer_command(config.path, workflow_id)


def _observer_command(config_path: Path, workflow_id: str) -> str:
    """Use the current installed interpreter instead of relying on a shell PATH."""
    if not isinstance(workflow_id, str) or not workflow_id.strip():
        raise ValueError("workflow identifier is required")
    arguments = [
        sys.executable,
        "-m",
        "flybridge_cli.main",
        "--config",
        str(config_path),
        "queue",
        "watch",
        "--notify-workflow",
        workflow_id.strip(),
    ]
    return shlex.join(["/bin/sh", "-c", shlex.join(arguments)])


def _coordinator_command(config_path: Path, manager_id: str) -> str:
    """Start the restart-safe orchestration supervisor in its own terminal."""
    if not isinstance(manager_id, str) or not manager_id.strip():
        raise ValueError("manager workflow identifier is required")
    return shlex.join(
        [
            "/bin/sh",
            "-c",
            shlex.join(
                [
                    sys.executable,
                    "-m",
                    "flybridge_cli.main",
                    "--config",
                    str(config_path),
                    "workflow",
                    "supervise",
                    manager_id.strip(),
                ]
            ),
        ]
    )


def _batch_watcher_command(config_path: Path, batch_id: str) -> str:
    arguments = [
        sys.executable,
        "-m",
        "flybridge_cli.main",
        "--config",
        str(config_path),
        "workflow",
        "batch",
        "watch",
        batch_id,
    ]
    return shlex.join(["/bin/sh", "-c", shlex.join(arguments)])


def _validate_mode_skill_paths(config, mode: WorkflowMode) -> None:
    roles = (
        (WorkflowRole.SINGLE,)
        if mode == WorkflowMode.SINGLE
        else (WorkflowRole.MANAGER, WorkflowRole.WORKER, WorkflowRole.REVIEWER)
    )
    paths = tuple(path for role in roles for path in config.skill_paths_for(role))
    validate_skill_paths(tuple(dict.fromkeys(paths)))
    missing_agents = [role.value for role in roles if role not in config.orca_agents]
    if missing_agents:
        raise ConfigError(
            "orca.agents must configure the required roles: " + ", ".join(missing_agents)
        )


def _generated_workflow_name() -> str:
    return f"flybridge-{datetime.now(UTC):%Y%m%d-%H%M%S-%f}-{uuid.uuid4().hex[:8]}"
