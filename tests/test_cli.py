import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import ENABLED_GITHUB, write_config
from flybridge_application import WorkflowService
from flybridge_cli.main import _generated_workflow_name, _observer_command, build_parser, main
from flybridge_core import ResourceQueue, WorkflowStore
from flybridge_github import GitHubProjectError
from flybridge_orca.client import StartedWorkflow


def _install_orca(monkeypatch, client_cls) -> None:
    if getattr(client_cls, "verify", None) is None:
        client_cls.verify = lambda self: {"app_version": "test", "skill": "orchestration"}
    monkeypatch.setattr("flybridge_cli.main.OrcaClient", client_cls)


def _start_owned(tmp_path: Path):
    service = WorkflowService(WorkflowStore(tmp_path))
    workflow = service.store.create(tmp_path, "single", "example", "Implement the change.")
    running = service.start_existing(
        workflow.id,
        lambda: SimpleNamespace(
            worktree_id="repo::/tmp/worktree", worktree="/tmp/worktree", terminal="term-1"
        ),
        lambda _reference: None,
        lambda _reference, _terminal: None,
    )
    return service, running


def test_start_rejects_missing_repository_without_creating_state(tmp_path: Path, capsys) -> None:
    state_dir = tmp_path / "state"
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, state_dir=state_dir, orca={"agents": {"single": "codex"}})

    result = main(
        ["--config", str(config_path), "start", str(tmp_path / "missing"), "-o", "Inspect."]
    )

    assert result == 2
    assert "repository does not exist" in capsys.readouterr().err
    assert not state_dir.exists()


def test_cli_reports_filesystem_errors_without_traceback(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(config_path)

    def fail_queue(_state_dir: Path):
        raise OSError("state directory is unavailable")

    monkeypatch.setattr("flybridge_cli.main.ResourceQueue", fail_queue)

    assert main(["--config", str(config_path), "queue", "status"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "flybridge: state directory is unavailable\n"
    assert "Traceback" not in captured.err


def test_start_rejects_orca_repository_registration_without_creating_state(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    state_dir = tmp_path / "state"
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, state_dir=state_dir, orca={"agents": {"single": "codex"}})

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def prepare_repository(self, _repository: Path) -> None:
            pass

        def register_repository(self, _repository: Path) -> None:
            raise RuntimeError("Orca repository registration failed")

    _install_orca(monkeypatch, FakeOrcaClient)

    assert main(["--config", str(config_path), "start", str(tmp_path), "-o", "Inspect."]) == 2
    assert "repository registration failed" in capsys.readouterr().err
    assert not state_dir.exists()


def test_start_requires_an_explicit_agent_for_every_selected_mode_role(
    tmp_path: Path, capsys
) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, orca={"agents": {"manager": "codex"}})

    result = main(
        [
            "--config",
            str(config_path),
            "start",
            str(tmp_path),
            "--mode",
            "orchestrated",
            "--objective",
            "Inspect.",
        ]
    )

    assert result == 2
    assert "worker, reviewer" in capsys.readouterr().err


def test_service_records_adapter_failure_after_preflight(tmp_path: Path) -> None:
    service = WorkflowService(WorkflowStore(tmp_path))
    workflow = service.store.create(tmp_path, "single", "example", "Implement the change.")

    try:
        service.start_existing(
            workflow.id,
            lambda: (_ for _ in ()).throw(RuntimeError("adapter failed")),
            lambda _reference: None,
            lambda _reference, _terminal: None,
        )
    except RuntimeError as exc:
        assert str(exc) == "adapter failed"
    else:
        raise AssertionError("adapter failure must be re-raised")

    assert service.store.active() == []


def test_service_finishes_running_workflow_after_orca_update(tmp_path: Path) -> None:
    service, running = _start_owned(tmp_path)
    updated: list[str] = []

    completed = service.finish(running.id, "completed", updated.append)

    assert updated == ["repo::/tmp/worktree"]
    assert completed.status == "completed"


def test_explicit_cleanup_reconciles_only_stale_owned_workflows(tmp_path: Path) -> None:
    service, running = _start_owned(tmp_path)
    with service.store._connect() as connection:
        connection.execute(
            "UPDATE workflows SET updated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", running.id),
        )
    closed: list[str] = []

    removed: list[str] = []
    reconciled = service.reconcile_stale(
        60, lambda reference, _terminal: closed.append(reference), removed.append
    )

    assert [workflow.id for workflow in reconciled] == [running.id]
    assert closed == ["repo::/tmp/worktree"]
    assert removed == ["repo::/tmp/worktree"]
    assert service.store.get(running.id).status == "cancelled"


def test_cleanup_requires_dry_run_or_apply() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["cleanup"])


def test_cleanup_requires_explicit_apply_threshold(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, state_dir=tmp_path / "state")

    result = main(["--config", str(config_path), "cleanup", "--apply"])

    assert result == 2
    assert "requires --older-than-seconds" in capsys.readouterr().err


def test_cleanup_apply_requires_explicit_age_force(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, state_dir=tmp_path / "state")

    result = main(
        ["--config", str(config_path), "cleanup", "--apply", "--older-than-seconds", "60"]
    )

    assert result == 2
    assert "requires --force-age" in capsys.readouterr().err


def test_cleanup_dry_run_reports_unreconciled_failed_worktree(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.jsonc"
    state_dir = tmp_path / "state"
    write_config(config_path, state_dir=state_dir)
    store = WorkflowStore(state_dir)
    workflow = store.create(tmp_path, "single", "failed-start", "Implement.")
    store.transition(workflow.id, "starting")
    store.attach_external(
        workflow.id,
        adapter_reference="partial-worktree",
        worktree_path="/tmp/partial",
        terminal_handle="terminal",
    )
    store.transition(workflow.id, "failed", error="start failed")

    assert main(["--config", str(config_path), "cleanup", "--dry-run"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["candidate_count"] == 1
    assert output["candidates"][0]["workflow_id"] == workflow.id


def test_start_renders_configured_skill_index_without_copying_content(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    skill = tmp_path / "manager.md"
    skill.write_text("private instruction content", encoding="utf-8")
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        state_dir=tmp_path / "state",
        orca={"agents": {"manager": "codex", "worker": "codex", "reviewer": "codex"}},
        skills={"roles": {"manager": [skill]}, "response_language": "Japanese"},
    )
    captured: dict[str, str] = {}

    class FakeOrcaClient:
        def __init__(self, executable: str) -> None:
            captured["executable"] = executable

        def start(self, repository, name, mode, agent, prompt, **_kwargs):
            captured["prompt"] = prompt
            return StartedWorkflow(name, "repo::/tmp/worktree", "/tmp/worktree", "term-1", mode)

        def set_lifecycle(self, *_args):
            return {"updated": True}

        def close_terminals(self, *_args):
            return {"closed": True}

        def remove_worktree(self, *_args):
            return {"removed": True}

        def prepare_repository(self, *_args):
            return None

        def register_repository(self, *_args):
            return {"registered": True}

    _install_orca(monkeypatch, FakeOrcaClient)

    result = main(
        [
            "--config",
            str(config_path),
            "start",
            str(tmp_path),
            "--mode",
            "orchestrated",
            "--objective",
            "Implement the requested change.",
        ]
    )

    assert result == 0
    assert "You are the manager role" in captured["prompt"]
    assert "Respond in the language named: Japanese." in captured["prompt"]
    assert str(skill) in captured["prompt"]
    assert "private instruction content" not in captured["prompt"]
    assert '"workflow"' in capsys.readouterr().out


def test_orchestrated_start_rejects_missing_future_role_skill_before_orca(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    state_dir = tmp_path / "state"
    missing = tmp_path / "worker.md"
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, state_dir=state_dir, skills={"roles": {"worker": [missing]}})
    called = False

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def prepare_repository(self, _repository: Path) -> None:
            nonlocal called
            called = True

        def register_repository(self, _repository: Path) -> None:
            nonlocal called
            called = True

    _install_orca(monkeypatch, FakeOrcaClient)

    assert (
        main(
            [
                "--config",
                str(config_path),
                "start",
                str(tmp_path),
                "--mode",
                "orchestrated",
                "--objective",
                "Inspect.",
            ]
        )
        == 2
    )
    assert "does not exist" in capsys.readouterr().err
    assert called is False
    assert not state_dir.exists()


def test_cli_uses_one_entry_and_cli_mode_overrides_configuration() -> None:
    parser = build_parser()

    arguments = parser.parse_args(
        ["start", ".", "--mode", "orchestrated", "--objective", "Inspect."]
    )

    assert parser.prog == "flybridge"
    assert arguments.mode == "orchestrated"


def test_cli_requires_an_objective_and_formats_options_consistently(capsys) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["start", "."])

    help_text = parser.format_help()
    assert "Options:" in help_text
    assert "  -c CONFIG, --config CONFIG" in help_text
    assert "JSONC user configuration" in help_text
    with pytest.raises(SystemExit):
        parser.parse_args(["start", "--help"])
    start_help = capsys.readouterr().out
    assert "  -o OBJECTIVE, --objective OBJECTIVE" in start_help
    assert "  -d, --allow-duplicate" in start_help
    assert "work objective" in start_help
    assert "ta" + "w" not in parser.format_help()
    assert "ow" + "f" not in parser.format_help()
    assert "kt" + "o" not in parser.format_help()
    observer = parser.parse_args(["queue", "observer", "-w", "repo::/tmp/worktree"])
    assert observer.worktree_id == "repo::/tmp/worktree"


def test_queue_watch_prints_snapshot_before_events(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, state_dir=tmp_path / "state")

    result = main(["--config", str(config_path), "queue", "watch", "--once"])

    assert result == 0
    assert json.loads(capsys.readouterr().out)["event"] == "snapshot"


@pytest.mark.parametrize("interval", ["0", "-1", "nan", "inf", "-inf"])
def test_queue_watch_rejects_invalid_numeric_intervals_before_output(
    tmp_path: Path, capsys, interval: str
) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, state_dir=tmp_path / "state")

    result = main(
        ["--config", str(config_path), "queue", "watch", "--once", f"--interval={interval}"]
    )

    assert result == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "finite positive" in captured.err
    assert "Traceback" not in captured.err


def test_queue_watch_rejects_non_numeric_interval_with_cli_input_status(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["queue", "watch", "--interval", "true"])

    assert exit_info.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invalid float value" in captured.err
    assert "Traceback" not in captured.err


def test_queue_entry_point_reconciles_missing_and_terminal_owners(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.jsonc"
    state_dir = tmp_path / "state"
    write_config(config_path, state_dir=state_dir)
    store = WorkflowStore(state_dir)
    workflow = store.create(tmp_path, "single", "failed", "Test recovery.")
    store.transition(workflow.id, "starting")
    store.transition(workflow.id, "failed", error="failed")
    queue = ResourceQueue(state_dir)
    terminal_request = queue.acquire("exclusive", workflow.id)
    unknown_request = queue.acquire("operator-only", "external-owner")

    assert main(["--config", str(config_path), "queue", "status"]) == 0
    capsys.readouterr()
    assert queue.inspect(terminal_request.request_id)["status"] == "cancelled"
    assert queue.inspect(unknown_request.request_id)["status"] == "cancelled"


def test_queue_recover_rejects_zero_without_releasing_a_lease(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.jsonc"
    state_dir = tmp_path / "state"
    write_config(config_path, state_dir=state_dir)
    store = WorkflowStore(state_dir)
    workflow = store.create(tmp_path, "single", "leased", "Recover.")
    store.transition(workflow.id, "starting")
    store.transition(workflow.id, "running")
    queue = ResourceQueue(state_dir)
    lease = queue.acquire("exclusive", workflow.id)

    assert (
        main(
            [
                "--config",
                str(config_path),
                "queue",
                "recover",
                "--older-than-seconds",
                "0",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "finite positive" in captured.err
    assert queue.inspect(lease.request_id)["status"] == "leased"


def test_cli_cancellation_uses_persisted_terminal_ownership(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = tmp_path / "config.jsonc"
    state_dir = tmp_path / "state"
    write_config(config_path, state_dir=state_dir)
    store = WorkflowStore(state_dir)
    workflow = store.create(tmp_path, "single", "example", "Implement the change.")
    store.transition(workflow.id, "starting")
    store.attach_external(
        workflow.id,
        adapter_reference="repo::/tmp/worktree",
        worktree_path="/tmp/worktree",
        terminal_handle="terminal-1",
    )
    store.transition(workflow.id, "running")
    calls: list[tuple[str, str]] = []

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def set_lifecycle(self, worktree_id: str, state: str, _detail=None) -> None:
            calls.append(("lifecycle", f"{worktree_id}:{state}"))

        def close_terminals(self, _worktree_id: str, terminal_handle: str) -> None:
            calls.append(("close", terminal_handle))

    _install_orca(monkeypatch, FakeOrcaClient)

    result = main(["--config", str(config_path), "workflow", "cancel", workflow.id])

    assert result == 0
    assert store.get(workflow.id).status == "cancelled"
    assert calls == [
        ("lifecycle", "repo::/tmp/worktree:cancelled"),
        ("close", "terminal-1"),
    ]
    assert json.loads(capsys.readouterr().out)["status"] == "cancelled"


@pytest.mark.parametrize("command", ["complete", "fail"])
def test_cli_terminal_finish_closes_every_owned_handle(
    tmp_path: Path, capsys, monkeypatch, command: str
) -> None:
    config_path = tmp_path / "config.jsonc"
    state_dir = tmp_path / "state"
    write_config(config_path, state_dir=state_dir)
    store = WorkflowStore(state_dir)
    workflow = store.create(tmp_path, "single", "example", "Implement the change.")
    store.transition(workflow.id, "starting")
    store.attach_external(
        workflow.id,
        adapter_reference="repo::/tmp/worktree",
        worktree_path="/tmp/worktree",
        terminal_handle="terminal-1",
    )
    store.add_owned_terminal(workflow.id, "observer-1", "observer")
    store.transition(workflow.id, "running")
    calls: list[tuple[str, str]] = []

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def set_lifecycle(self, worktree_id: str, state: str, _detail=None) -> None:
            calls.append(("lifecycle", f"{worktree_id}:{state}"))

        def close_terminals(self, _worktree_id: str, terminal_handle: str) -> None:
            calls.append(("close", terminal_handle))

    _install_orca(monkeypatch, FakeOrcaClient)
    arguments = ["--config", str(config_path), "workflow", command, workflow.id]
    if command == "fail":
        arguments.extend(["--error", "verification failed"])

    assert main(arguments) == 0
    state = "completed" if command == "complete" else "failed"
    assert calls == [
        ("lifecycle", f"repo::/tmp/worktree:{state}"),
        ("close", "terminal-1"),
        ("close", "observer-1"),
    ]


def test_observer_is_owned_by_its_active_workflow(tmp_path: Path, capsys, monkeypatch) -> None:
    config_path = tmp_path / "config.jsonc"
    state_dir = tmp_path / "state"
    write_config(config_path, state_dir=state_dir)
    store = WorkflowStore(state_dir)
    workflow = store.create(tmp_path, "single", "example", "Implement the change.")
    store.transition(workflow.id, "starting")
    store.attach_external(
        workflow.id,
        adapter_reference="repo::/tmp/worktree",
        worktree_path="/tmp/worktree",
        terminal_handle="agent-1",
    )
    store.transition(workflow.id, "running")

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def create_observer(self, _worktree_id: str, _command: str) -> str:
            return "observer-1"

    _install_orca(monkeypatch, FakeOrcaClient)

    result = main(
        ["--config", str(config_path), "queue", "observer", "--worktree-id", "repo::/tmp/worktree"]
    )

    assert result == 0
    assert store.owned_terminal_handles(workflow.id) == ["agent-1", "observer-1"]
    assert json.loads(capsys.readouterr().out)["terminal_handle"] == "observer-1"


def test_observer_rejects_unowned_worktree_before_creating_terminal(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, state_dir=tmp_path / "state")

    result = main(["--config", str(config_path), "queue", "observer", "--worktree-id", "unowned"])

    assert result == 2
    assert "active Flybridge workflow" in capsys.readouterr().err


def test_observer_command_uses_the_current_interpreter_and_quotes_config_path(
    tmp_path: Path,
) -> None:
    command = _observer_command(tmp_path / "a space;not-a-command.jsonc")

    parsed = __import__("shlex").split(command)
    assert parsed[-2:] == ["queue", "watch"]
    assert parsed[-3] == str(tmp_path / "a space;not-a-command.jsonc")
    assert parsed[1:3] == ["-m", "flybridge_cli.main"]


@pytest.mark.parametrize(
    "mode, expected_roles", [("single", ["single"]), ("orchestrated", ["manager"])]
)
def test_cli_start_smoke_for_both_modes(
    tmp_path: Path, capsys, monkeypatch, mode, expected_roles
) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        state_dir=tmp_path / "state",
        orca={
            "agents": {
                "single": "codex",
                "manager": "codex",
                "worker": "codex",
                "reviewer": "codex",
            }
        },
    )
    started: list[tuple[str, str]] = []

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def prepare_repository(self, _repository: Path) -> None:
            pass

        def register_repository(self, _repository: Path) -> None:
            pass

        def start(self, _repository, name, workflow_mode, _agent, _prompt, **_kwargs):
            started.append((name, workflow_mode))
            return StartedWorkflow(
                name, f"id:{name}", str(tmp_path / name), f"terminal:{name}", workflow_mode
            )

        def set_lifecycle(self, *_args) -> dict[str, bool]:
            return {"updated": True}

        def close_terminals(self, *_args) -> dict[str, bool]:
            return {"closed": True}

        def remove_worktree(self, *_args) -> dict[str, bool]:
            return {"removed": True}

    _install_orca(monkeypatch, FakeOrcaClient)
    assert (
        main(
            [
                "--config",
                str(config_path),
                "start",
                str(tmp_path),
                "--mode",
                mode,
                "--objective",
                "Inspect.",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["workflow"]["role"] in expected_roles
    assert len(started) == 1


def test_cli_completes_the_full_orchestrated_role_sequence(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        state_dir=tmp_path / "state",
        orca={"agents": {"manager": "codex", "worker": "codex", "reviewer": "codex"}},
    )
    prompts: list[str] = []
    started_roles: list[str] = []
    start_options: list[dict[str, str | None]] = []

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def prepare_repository(self, _repository: Path) -> None:
            pass

        def register_repository(self, _repository: Path) -> None:
            pass

        def start(self, _repository, name, workflow_mode, _agent, prompt, **kwargs):
            role = prompt.split("You are the ", 1)[1].split(" role", 1)[0]
            prompts.append(prompt)
            started_roles.append(role)
            start_options.append(kwargs)
            return StartedWorkflow(
                name,
                f"id:{role}",
                str(tmp_path / role),
                f"terminal:{role}",
                workflow_mode,
            )

        def current_branch(self, _worktree_path: str) -> str:
            return "main"

        def uncommitted_changes(self, _worktree_path: str) -> tuple[str, ...]:
            return ()

        def set_lifecycle(self, *_args) -> dict[str, bool]:
            return {"updated": True}

        def close_terminals(self, *_args) -> dict[str, bool]:
            return {"closed": True}

        def remove_worktree(self, *_args) -> dict[str, bool]:
            return {"removed": True}

    _install_orca(monkeypatch, FakeOrcaClient)

    assert (
        main(
            [
                "--config",
                str(config_path),
                "start",
                str(tmp_path),
                "--mode",
                "orchestrated",
                "--objective",
                "Inspect.",
            ]
        )
        == 0
    )
    start_output = json.loads(capsys.readouterr().out)
    manager_id = start_output["workflow"]["id"]
    worker_id = next(
        child["id"] for child in start_output["planned_children"] if child["role"] == "worker"
    )
    reviewer_id = next(
        child["id"] for child in start_output["planned_children"] if child["role"] == "reviewer"
    )

    assert main(["--config", str(config_path), "workflow", "complete", manager_id]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "--config",
                str(config_path),
                "workflow",
                "handoff",
                manager_id,
                worker_id,
                "--summary",
                "Manager plan is complete.",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["--config", str(config_path), "workflow", "advance", manager_id]) == 0
    capsys.readouterr()
    assert started_roles == ["manager", "worker"]
    assert "Manager plan is complete." in prompts[-1]
    assert start_options[-1] == {"parent_worktree_id": "id:manager", "base_branch": "main"}

    assert main(["--config", str(config_path), "workflow", "complete", worker_id]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "--config",
                str(config_path),
                "workflow",
                "handoff",
                worker_id,
                reviewer_id,
                "--summary",
                "Worker verification is ready for review.",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["--config", str(config_path), "workflow", "advance", manager_id]) == 0
    capsys.readouterr()
    assert started_roles == ["manager", "worker", "reviewer"]
    assert "Worker verification is ready for review." in prompts[-1]
    assert start_options[-1] == {"parent_worktree_id": "id:worker", "base_branch": "main"}
    assert main(["--config", str(config_path), "workflow", "complete", reviewer_id]) == 0
    capsys.readouterr()
    store = WorkflowStore(tmp_path / "state")
    assert store.get(manager_id).status == "completed"
    assert store.get(worker_id).parent_id == manager_id
    assert store.get(reviewer_id).status == "completed"


def test_advance_rejects_a_source_worktree_whose_changes_a_child_cannot_inherit(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        state_dir=tmp_path / "state",
        orca={"agents": {"manager": "codex", "worker": "codex", "reviewer": "codex"}},
    )
    started_roles: list[str] = []

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def prepare_repository(self, _repository: Path) -> None:
            pass

        def register_repository(self, _repository: Path) -> None:
            pass

        def start(self, _repository, name, workflow_mode, _agent, prompt, **_kwargs):
            started_roles.append(prompt.split("You are the ", 1)[1].split(" role", 1)[0])
            return StartedWorkflow(
                name, f"id:{name}", str(tmp_path / name), f"terminal:{name}", workflow_mode
            )

        def current_branch(self, _worktree_path: str) -> str:
            return "main"

        def uncommitted_changes(self, _worktree_path: str) -> tuple[str, ...]:
            return ("?? tmp/report.md", " M packages/core/src/flybridge_core/config.py")

        def set_lifecycle(self, *_args) -> dict[str, bool]:
            return {"updated": True}

        def close_terminals(self, *_args) -> dict[str, bool]:
            return {"closed": True}

        def remove_worktree(self, *_args) -> dict[str, bool]:
            return {"removed": True}

    _install_orca(monkeypatch, FakeOrcaClient)
    assert (
        main(
            [
                "--config",
                str(config_path),
                "start",
                str(tmp_path),
                "--mode",
                "orchestrated",
                "--objective",
                "Inspect.",
            ]
        )
        == 0
    )
    start_output = json.loads(capsys.readouterr().out)
    manager_id = start_output["workflow"]["id"]
    worker_id = next(
        child["id"] for child in start_output["planned_children"] if child["role"] == "worker"
    )

    assert main(["--config", str(config_path), "workflow", "complete", manager_id]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "--config",
                str(config_path),
                "workflow",
                "handoff",
                manager_id,
                worker_id,
                "--summary",
                "Manager plan is complete.",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["--config", str(config_path), "workflow", "advance", manager_id]) == 2
    error = capsys.readouterr().err
    assert "uncommitted changes" in error
    assert "tmp/report.md" in error
    assert started_roles == ["manager"]
    assert WorkflowStore(tmp_path / "state").get(worker_id).status == "requested"


@pytest.mark.parametrize("mode", ["single", "orchestrated"])
def test_root_start_opens_configured_queue_observer_and_indexes_resources(
    tmp_path: Path, capsys, monkeypatch, mode: str
) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        state_dir=tmp_path / "state",
        orca={
            "agents": {
                "single": "codex",
                "manager": "codex",
                "worker": "codex",
                "reviewer": "codex",
            }
        },
        queue={"observer": True, "resources": ["device"]},
    )
    captured: dict[str, str] = {}

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def prepare_repository(self, _repository: Path) -> None:
            pass

        def register_repository(self, _repository: Path) -> None:
            pass

        def start(self, _repository, name, workflow_mode, _agent, prompt, **_kwargs):
            captured["prompt"] = prompt
            return StartedWorkflow(
                name, f"id:{name}", str(tmp_path / name), "agent-terminal", workflow_mode
            )

        def set_lifecycle(self, *_args) -> dict[str, bool]:
            return {"updated": True}

        def create_observer(self, _worktree_id: str, command: str) -> str:
            captured["observer_command"] = command
            return "observer-terminal"

        def close_terminals(self, *_args) -> dict[str, bool]:
            return {"closed": True}

        def remove_worktree(self, *_args) -> dict[str, bool]:
            return {"removed": True}

    _install_orca(monkeypatch, FakeOrcaClient)

    assert (
        main(
            [
                "--config",
                str(config_path),
                "start",
                str(tmp_path),
                "--mode",
                mode,
                "--objective",
                "Run checks.",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    store = WorkflowStore(tmp_path / "state")
    assert "`device`" in captured["prompt"]
    assert captured["observer_command"].endswith("queue watch")
    assert store.owned_terminal_handles(output["workflow"]["id"]) == [
        "agent-terminal",
        "observer-terminal",
    ]


def test_cli_can_disable_the_configured_queue_observer(tmp_path: Path, capsys, monkeypatch) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        state_dir=tmp_path / "state",
        orca={"agents": {"single": "codex"}},
        queue={"observer": True},
    )

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def prepare_repository(self, _repository: Path) -> None:
            pass

        def register_repository(self, _repository: Path) -> None:
            pass

        def start(self, _repository, name, workflow_mode, _agent, _prompt, **_kwargs):
            return StartedWorkflow(
                name, f"id:{name}", str(tmp_path / name), "agent-terminal", workflow_mode
            )

        def set_lifecycle(self, *_args) -> dict[str, bool]:
            return {"updated": True}

        def close_terminals(self, *_args) -> dict[str, bool]:
            return {"closed": True}

        def remove_worktree(self, *_args) -> dict[str, bool]:
            return {"removed": True}

    _install_orca(monkeypatch, FakeOrcaClient)

    assert (
        main(
            [
                "--config",
                str(config_path),
                "start",
                str(tmp_path),
                "--objective",
                "Run checks.",
                "--no-queue-observer",
            ]
        )
        == 0
    )
    capsys.readouterr()


def test_workflow_retry_command_resets_a_failed_record(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, state_dir=tmp_path / "state")
    store = WorkflowStore(tmp_path / "state")
    workflow = store.create(tmp_path, "single", "retry", "Implement.")
    store.transition(workflow.id, "starting")
    store.transition(workflow.id, "failed", error="failed")

    assert main(["--config", str(config_path), "workflow", "retry", workflow.id]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["workflow"]["status"] == "requested"


def test_board_screen_cli_returns_candidates(tmp_path: Path, capsys, monkeypatch) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, github=ENABLED_GITHUB)

    class FakeProject:
        def __init__(self, _config) -> None:
            pass

        def screen(self):
            return [SimpleNamespace(repository="example/repo", number=1, title="Task", url="u")]

    monkeypatch.setattr("flybridge_cli.main.GitHubProject", FakeProject)

    assert main(["--config", str(config_path), "board", "screen"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["number"] == 1


def test_board_screen_cli_reports_malformed_responses_without_a_traceback(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, github=ENABLED_GITHUB)

    class FakeProject:
        def __init__(self, _config):
            pass

        def screen(self):
            raise GitHubProjectError("unexpected GitHub Project response")

    monkeypatch.setattr("flybridge_cli.main.GitHubProject", FakeProject)

    assert main(["--config", str(config_path), "board", "screen"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "flybridge: unexpected GitHub Project response\n"


def test_cli_rejects_retired_mcp_subcommand(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["mcp", "config"])
    assert exit_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_doctor_fails_for_missing_required_agent_and_enabled_github_cli(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, github=ENABLED_GITHUB)

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def verify(self) -> dict[str, str]:
            return {"app_version": "test"}

    _install_orca(monkeypatch, FakeOrcaClient)
    monkeypatch.setattr(
        "flybridge_cli.main.shutil.which",
        lambda executable: "/usr/bin/orca" if executable.startswith("orca") else None,
    )

    assert main(["--config", str(config_path), "doctor"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["missing_agents"] == ["single"]
    assert report["github_enabled"] is True
    assert report["gh"] is False


def test_doctor_validates_the_explicit_mode_only(tmp_path: Path, capsys, monkeypatch) -> None:
    missing_worker_skill = tmp_path / "missing-worker.md"
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        default_mode="orchestrated",
        orca={"agents": {"single": "codex"}},
        skills={"roles": {"worker": [missing_worker_skill]}},
    )

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def verify(self) -> dict[str, str]:
            return {"app_version": "test"}

    _install_orca(monkeypatch, FakeOrcaClient)
    monkeypatch.setattr("flybridge_cli.main.shutil.which", lambda _executable: "/usr/bin/tool")

    assert main(["--config", str(config_path), "doctor", "--mode", "single"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "single"
    assert report["skill_sources_valid"] is True
    assert report["required_agents_configured"] is True


def test_blank_objective_fails_before_orca_preflight(tmp_path: Path, capsys, monkeypatch) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, state_dir=tmp_path / "state", orca={"agents": {"single": "codex"}})
    called = False

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def prepare_repository(self, _repository: Path) -> None:
            nonlocal called
            called = True

    _install_orca(monkeypatch, FakeOrcaClient)

    assert main(["--config", str(config_path), "start", str(tmp_path), "--objective", "   "]) == 2
    assert called is False
    assert "objective is required" in capsys.readouterr().err


def test_workflow_resume_reuses_persisted_terminal_without_creating_worktree(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    state_dir = tmp_path / "state"
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, state_dir=state_dir, orca={"agents": {"single": "codex"}})
    store = WorkflowStore(state_dir)
    workflow = store.create(tmp_path, "single", "resume", "Continue implementation.")
    store.begin_start(workflow.id)
    store.attach_external(
        workflow.id,
        adapter_reference="repo::/tmp/existing",
        worktree_path="/tmp/existing",
        terminal_handle="agent-existing",
    )
    store.transition(workflow.id, "running")
    calls: list[tuple[str, str]] = []

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def verify_worktree(self, worktree_id: str, worktree_path: str) -> None:
            calls.append((worktree_id, worktree_path))

        def terminal_is_valid(self, worktree_id: str, terminal_handle: str) -> bool:
            calls.append((worktree_id, terminal_handle))
            return True

        def wait_for_agent(self, terminal_handle: str) -> None:
            calls.append(("wait", terminal_handle))

        def send_prompt(self, terminal_handle: str, prompt: str) -> None:
            calls.append((terminal_handle, prompt))

    _install_orca(monkeypatch, FakeOrcaClient)

    assert main(["--config", str(config_path), "workflow", "resume", workflow.id]) == 0
    assert json.loads(capsys.readouterr().out)["workflow"]["status"] == "running"
    assert calls[0] == ("repo::/tmp/existing", "/tmp/existing")
    assert calls[1] == ("repo::/tmp/existing", "agent-existing")
    assert calls[2] == ("wait", "agent-existing")
    assert calls[3][0] == "agent-existing"


def test_generated_workflow_names_are_unique_within_one_second() -> None:
    names = {_generated_workflow_name() for _ in range(100)}

    assert len(names) == 100
    assert all(name.startswith("flybridge-") for name in names)


def test_start_parser_exposes_explicit_duplicate_override() -> None:
    arguments = build_parser().parse_args(["start", ".", "--objective", "Inspect.", "-d"])

    assert arguments.allow_duplicate is True


def test_queue_acquire_requires_a_running_workflow_owner(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, state_dir=tmp_path / "state")

    result = main(
        [
            "--config",
            str(config_path),
            "queue",
            "acquire",
            "exclusive",
            "--owner",
            "missing",
        ]
    )

    assert result == 2
    assert "running workflow" in capsys.readouterr().err


def test_queue_acquire_cancels_request_when_owner_stops_during_acquisition(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = tmp_path / "config.jsonc"
    state_dir = tmp_path / "state"
    write_config(config_path, state_dir=state_dir)
    store = WorkflowStore(state_dir)
    workflow = store.create(tmp_path, "single", "racing-owner", "Implement.")
    store.transition(workflow.id, "starting")
    store.transition(workflow.id, "running")
    real_acquire = ResourceQueue.acquire
    requests: list[str] = []

    def acquire_then_stop(queue, resource: str, owner: str):
        result = real_acquire(queue, resource, owner)
        requests.append(result.request_id)
        store.transition(workflow.id, "completed")
        return result

    monkeypatch.setattr(ResourceQueue, "acquire", acquire_then_stop)

    result = main(
        [
            "--config",
            str(config_path),
            "queue",
            "acquire",
            "exclusive",
            "--owner",
            workflow.id,
        ]
    )

    assert result == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "stopped during resource acquisition" in captured.err
    assert ResourceQueue(state_dir).inspect(requests[0])["status"] == "cancelled"


def test_cleanup_apply_reconciles_stale_running_workflow(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = tmp_path / "config.jsonc"
    state_dir = tmp_path / "state"
    write_config(config_path, state_dir=state_dir)
    store = WorkflowStore(state_dir)
    workflow = store.create(tmp_path, "single", "stale", "Implement.")
    store.transition(workflow.id, "starting")
    store.attach_external(
        workflow.id,
        adapter_reference="repo::/tmp/stale",
        worktree_path="/tmp/stale",
        terminal_handle="term",
    )
    store.transition(workflow.id, "running")
    with store._connect() as connection:
        connection.execute(
            "UPDATE workflows SET updated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", workflow.id),
        )
    removed: list[str] = []

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def close_terminals(self, _worktree_id: str, _handle: str) -> None:
            return None

        def remove_worktree(self, worktree_id: str) -> dict[str, bool]:
            removed.append(worktree_id)
            return {"removed": True}

    _install_orca(monkeypatch, FakeOrcaClient)

    assert (
        main(
            [
                "--config",
                str(config_path),
                "cleanup",
                "--apply",
                "--older-than-seconds",
                "60",
                "--force-age",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["reconciled_count"] == 1
    assert output["workflow_ids"] == [workflow.id]
    assert removed == ["repo::/tmp/stale"]
    assert store.get(workflow.id).status == "cancelled"


def test_workflow_launch_restarts_a_requested_root(tmp_path: Path, capsys, monkeypatch) -> None:
    config_path = tmp_path / "config.jsonc"
    state_dir = tmp_path / "state"
    write_config(config_path, state_dir=state_dir, orca={"agents": {"single": "codex"}})
    store = WorkflowStore(state_dir)
    workflow = store.create(tmp_path, "single", "retried", "Implement.")

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def start(self, _repository, name, mode, _agent, _prompt, **_kwargs):
            return StartedWorkflow(name, "repo::/tmp/retry", "/tmp/retry", "term", mode)

        def set_lifecycle(self, *_args):
            return {"updated": True}

        def close_terminals(self, *_args):
            return None

        def remove_worktree(self, *_args):
            return {}

    _install_orca(monkeypatch, FakeOrcaClient)

    assert main(["--config", str(config_path), "workflow", "launch", workflow.id]) == 0
    assert json.loads(capsys.readouterr().out)["workflow"]["status"] == "running"


def test_workflow_advance_rejects_a_non_manager_identifier(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.jsonc"
    state_dir = tmp_path / "state"
    write_config(config_path, state_dir=state_dir)
    store = WorkflowStore(state_dir)
    workflow = store.create(tmp_path, "single", "single-root", "Implement.")

    result = main(["--config", str(config_path), "workflow", "advance", workflow.id])

    assert result == 2
    assert "orchestrated manager" in capsys.readouterr().err
