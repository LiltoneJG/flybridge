import json
import sqlite3
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import ENABLED_GITHUB, write_config
from flybridge_application import WorkflowService
from flybridge_cli.main import main
from flybridge_cli.parser import build_parser
from flybridge_cli.runtime import _coordinator_command, _generated_workflow_name, _observer_command
from flybridge_core import ResourceQueue, WorkflowArtifactStore, WorkflowStore
from flybridge_github import GitHubProjectError, GitHubPullRequestError
from flybridge_github.project import Candidate
from flybridge_orca.client import StartedWorkflow

ISSUE_URL = "https://github.com/example/repo/issues/1"


def _install_orca(monkeypatch, client_cls) -> None:
    if getattr(client_cls, "verify", None) is None:
        client_cls.verify = lambda self: {"app_version": "test", "skill": "orchestration"}
    if getattr(client_cls, "worktree_comment", None) is None:
        client_cls.worktree_comment = lambda self, _worktree_id: ""
    if getattr(client_cls, "set_issue_comment", None) is None:
        client_cls.set_issue_comment = lambda self, *_args, **_kwargs: {}
    if getattr(client_cls, "create_coordinator", None) is None:
        client_cls.create_coordinator = lambda self, *_args, **_kwargs: "terminal:coordinator"
    if getattr(client_cls, "terminal_is_valid", None) is None:
        client_cls.terminal_is_valid = lambda self, *_args, **_kwargs: True
    if getattr(client_cls, "verify_worktree", None) is None:
        client_cls.verify_worktree = lambda self, *_args, **_kwargs: None
    if getattr(client_cls, "push_fast_forward", None) is None:
        client_cls.push_fast_forward = lambda self, *_args, **_kwargs: {
            "remote": "origin",
            "ref": "HEAD",
        }
    if getattr(client_cls, "integrate_worker_commit", None) is None:

        def _integrate(self, manager_path, worker_path, worker_sha, *, dry_run=False):
            return {"method": "already", "before": worker_sha, "after": worker_sha}

        client_cls.integrate_worker_commit = _integrate
    monkeypatch.setattr("flybridge_cli.runtime.OrcaClient", client_cls)


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
        [
            "--config",
            str(config_path),
            "workflow",
            "start",
            str(tmp_path / "missing"),
            "-o",
            "Inspect.",
        ]
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

    monkeypatch.setattr("flybridge_cli.commands.queue.ResourceQueue", fail_queue)

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

    assert (
        main(
            [
                "--config",
                str(config_path),
                "workflow",
                "start",
                str(tmp_path),
                "-o",
                "Inspect.",
                "--issue",
                ISSUE_URL,
            ]
        )
        == 2
    )
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
            "workflow",
            "start",
            str(tmp_path),
            "--mode",
            "orchestrated",
            "--objective",
            "Inspect.",
            "--issue",
            ISSUE_URL,
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


def test_cleanup_defaults_to_dry_run(tmp_path: Path, capsys) -> None:
    args = build_parser().parse_args(["workflow", "cleanup"])
    assert args.dry_run is False
    assert args.apply is False

    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        state_dir=tmp_path / "state",
        reconcile={"auto_before_workflow_commands": False},
    )
    assert main(["--config", str(config_path), "workflow", "cleanup"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["candidate_count"] == 0


@pytest.mark.parametrize(
    "arguments", (["start", "."], ["cleanup", "--dry-run"], ["queue", "observer"])
)
def test_removed_root_and_queue_observer_commands_are_rejected(arguments: list[str]) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(arguments)


def test_cleanup_requires_explicit_apply_threshold(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        state_dir=tmp_path / "state",
        reconcile={"auto_before_workflow_commands": False},
    )

    result = main(["--config", str(config_path), "workflow", "cleanup", "--apply"])

    assert result == 2
    assert "requires --older-than-seconds" in capsys.readouterr().err


def test_cleanup_apply_with_age_does_not_require_force_age(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        state_dir=tmp_path / "state",
        reconcile={"auto_before_workflow_commands": False},
    )

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def verify(self):
            return {"app_version": "test"}

        def close_terminals(self, *_args, **_kwargs):
            return {"closed": True}

        def remove_worktree(self, *_args, **_kwargs):
            return {"removed": True}

    _install_orca(monkeypatch, FakeOrcaClient)
    result = main(
        [
            "--config",
            str(config_path),
            "workflow",
            "cleanup",
            "--apply",
            "--older-than-seconds",
            "60",
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is False
    assert payload["reconciled_count"] == 0


def test_cleanup_dry_run_recommends_retire_for_leftover_children(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.jsonc"
    state_dir = tmp_path / "state"
    write_config(
        config_path,
        state_dir=state_dir,
        reconcile={"auto_before_workflow_commands": False},
    )
    store = WorkflowStore(state_dir)
    manager, worker, reviewer = store.create_orchestrated_plan(tmp_path, "task", "Implement.")
    for workflow_id, name in (
        (manager.id, "manager"),
        (worker.id, "worker"),
        (reviewer.id, "reviewer"),
    ):
        store.begin_start(workflow_id)
        store.attach_external(
            workflow_id,
            adapter_reference=f"repo::{name}",
            worktree_path=f"/tmp/{name}",
            terminal_handle=f"term-{name}",
            owns_worktree=workflow_id != manager.id,
            implementation_repository="example/repo",
            runtime_repository_id="repo",
            start_sha="aaa111",
        )
        store.transition(workflow_id, "running")
        store.transition(workflow_id, "completed")
    store.set_orchestration_outcome(manager.id, "completed")

    assert main(["--config", str(config_path), "workflow", "cleanup", "--dry-run"]) == 0
    output = json.loads(capsys.readouterr().out)
    leftover = [item for item in output["candidates"] if item["retire_recommended"]]
    assert leftover
    assert leftover[0]["workflow_id"] == manager.id
    assert leftover[0]["root_id"] == manager.id
    assert output["candidate_count"] >= 1


def test_cleanup_dry_run_reports_unreconciled_failed_worktree(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.jsonc"
    state_dir = tmp_path / "state"
    write_config(
        config_path,
        state_dir=state_dir,
        reconcile={"auto_before_workflow_commands": False},
    )
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

    assert main(["--config", str(config_path), "workflow", "cleanup", "--dry-run"]) == 0
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

        def set_lifecycle(self, *_args, **_kwargs):
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
            "workflow",
            "start",
            str(tmp_path),
            "--mode",
            "orchestrated",
            "--objective",
            "Implement the requested change.",
            "--issue",
            ISSUE_URL,
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
                "workflow",
                "start",
                str(tmp_path),
                "--mode",
                "orchestrated",
                "--objective",
                "Inspect.",
                "--issue",
                ISSUE_URL,
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
        ["workflow", "start", ".", "--mode", "orchestrated", "--objective", "Inspect."]
    )

    assert parser.prog == "flybridge"
    assert arguments.mode == "orchestrated"


def test_cli_requires_an_objective_and_formats_options_consistently(capsys) -> None:
    parser = build_parser()

    arguments = parser.parse_args(["workflow", "start", "."])
    assert arguments.objective is None
    assert arguments.objective_file is None

    help_text = parser.format_help()
    assert "Options:" in help_text
    assert "  -c CONFIG, --config CONFIG" in help_text
    assert "JSONC user configuration" in help_text
    with pytest.raises(SystemExit):
        parser.parse_args(["workflow", "start", "--help"])
    start_help = capsys.readouterr().out
    assert "  -o OBJECTIVE, --objective OBJECTIVE" in start_help
    assert "  -d, --allow-duplicate" in start_help
    assert "  --issue ISSUE" in start_help
    assert "work objective" in start_help
    assert "ta" + "w" not in parser.format_help()
    assert "ow" + "f" not in parser.format_help()
    assert "kt" + "o" not in parser.format_help()
    observer = parser.parse_args(["workflow", "observe", "workflow-123"])
    assert observer.workflow_id == "workflow-123"


def test_queue_watch_prints_snapshot_before_events(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, state_dir=tmp_path / "state")

    result = main(["--config", str(config_path), "queue", "watch", "--once"])

    assert result == 0
    assert json.loads(capsys.readouterr().out)["event"] == "snapshot"


def test_queue_watch_once_does_not_deliver_lease_notifications(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = tmp_path / "config.jsonc"
    state_dir = tmp_path / "state"
    write_config(config_path, state_dir=state_dir)
    store = WorkflowStore(state_dir)
    workflow = store.create(tmp_path, "single", "waiter", "Do work.")
    store.transition(workflow.id, "starting")
    store.attach_external(
        workflow.id,
        adapter_reference="repo::/tmp/waiter",
        worktree_path="/tmp/waiter",
        terminal_handle="term-waiter",
    )
    store.transition(workflow.id, "running")
    holder = store.create(tmp_path, "single", "holder", "Hold.")
    store.transition(holder.id, "starting")
    store.attach_external(
        holder.id,
        adapter_reference="repo::/tmp/holder",
        worktree_path="/tmp/holder",
        terminal_handle="term-holder",
    )
    store.transition(holder.id, "running")
    queue = ResourceQueue(state_dir)
    first = queue.acquire("probe", holder.id)
    queue.acquire("probe", workflow.id)
    queue.release(first.lease_id or "")
    sent: list[str] = []

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def send_prompt(self, _terminal_handle: str, prompt: str) -> None:
            sent.append(prompt)

        def wait_for_agent(self, _terminal_handle: str) -> None:
            return None

        def terminal_is_valid(self, _worktree_id: str, _handle: str | None) -> bool:
            return True

    _install_orca(monkeypatch, FakeOrcaClient)

    result = main(
        [
            "--config",
            str(config_path),
            "queue",
            "watch",
            "--once",
            "--notify-workflow",
            workflow.id,
        ]
    )

    assert result == 0
    output = capsys.readouterr().out
    assert "notify_sent" not in output
    assert sent == []


def test_queue_watcher_exits_for_an_unavailable_owner_without_releasing_lease(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = tmp_path / "config.jsonc"
    state_dir = tmp_path / "state"
    write_config(config_path, state_dir=state_dir)
    store = WorkflowStore(state_dir)
    workflow = store.create(tmp_path, "single", "owner", "Do work.")
    store.transition(workflow.id, "starting")
    store.attach_external(
        workflow.id,
        adapter_reference="repo::/tmp/owner",
        worktree_path="/tmp/owner",
        terminal_handle="term-owner",
    )
    store.transition(workflow.id, "running")
    store.add_owned_terminal(workflow.id, "observer", "observer")
    queue = ResourceQueue(state_dir)
    lease = queue.acquire("probe", workflow.id)

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def terminal_is_valid(self, _worktree_id: str, _handle: str | None) -> bool:
            return False

        def close_terminals(self, _worktree_id: str, _handle: str | None) -> None:
            pass

    _install_orca(monkeypatch, FakeOrcaClient)

    result = main(
        ["--config", str(config_path), "queue", "watch", "--notify-workflow", workflow.id]
    )

    assert result == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[-1] == {"event": "owner_terminal_unavailable", "workflow_id": workflow.id}
    assert store.owned_terminal_handles(workflow.id, kind="observer") == []
    recorded = store.get(workflow.id)
    assert recorded.observer_last_handle == "observer"
    assert recorded.observer_stop_reason == "owner_terminal_unavailable"
    assert recorded.observer_stopped_at is not None
    assert queue.inspect(lease.request_id)["status"] == "leased"


def test_queue_watcher_survives_a_detach_database_error(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = tmp_path / "config.jsonc"
    state_dir = tmp_path / "state"
    write_config(config_path, state_dir=state_dir)
    store = WorkflowStore(state_dir)
    workflow = store.create(tmp_path, "single", "owner", "Do work.")
    store.transition(workflow.id, "starting")
    store.attach_external(
        workflow.id,
        adapter_reference="repo::/tmp/owner",
        worktree_path="/tmp/owner",
        terminal_handle="term-owner",
    )
    store.transition(workflow.id, "running")
    store.add_owned_terminal(workflow.id, "observer", "observer")
    queue = ResourceQueue(state_dir)
    lease = queue.acquire("probe", workflow.id)

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def terminal_is_valid(self, _worktree_id: str, _handle: str | None) -> bool:
            return False

        def close_terminals(self, _worktree_id: str, _handle: str | None) -> None:
            pass

    def broken_detach(_notifier):
        raise sqlite3.OperationalError("no such table: workflows")

    _install_orca(monkeypatch, FakeOrcaClient)
    monkeypatch.setattr(
        "flybridge_cli.commands.queue.QueueLeaseNotifier.detach_if_owner_terminal_unavailable",
        broken_detach,
    )

    result = main(
        ["--config", str(config_path), "queue", "watch", "--notify-workflow", workflow.id]
    )

    assert result == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {
        "event": "observer_detach_failed",
        "workflow_id": workflow.id,
        "error": "no such table: workflows",
    } in events
    assert events[-1] == {
        "event": "observer_detach_failed",
        "workflow_id": workflow.id,
        "error": "no such table: workflows",
    }
    assert queue.inspect(lease.request_id)["status"] == "leased"


def test_queue_watcher_retries_a_transient_workflow_database_error(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = tmp_path / "config.jsonc"
    state_dir = tmp_path / "state"
    write_config(config_path, state_dir=state_dir)
    store = WorkflowStore(state_dir)
    workflow = store.create(tmp_path, "single", "owner", "Do work.")
    store.transition(workflow.id, "starting")
    store.attach_external(
        workflow.id,
        adapter_reference="repo::/tmp/owner",
        worktree_path="/tmp/owner",
        terminal_handle="term-owner",
    )
    store.transition(workflow.id, "running")
    store.add_owned_terminal(workflow.id, "observer", "observer")
    checks = iter((sqlite3.OperationalError("no such table: workflows"), False))

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def close_terminals(self, _worktree_id: str, _handle: str | None) -> None:
            pass

    def owner_terminal_is_available(_notifier) -> bool:
        result = next(checks)
        if isinstance(result, Exception):
            raise result
        return result

    _install_orca(monkeypatch, FakeOrcaClient)
    monkeypatch.setattr(
        "flybridge_cli.commands.queue.QueueLeaseNotifier.owner_terminal_is_available",
        owner_terminal_is_available,
    )
    monkeypatch.setattr("flybridge_cli.commands.queue.time.sleep", lambda _interval: None)

    result = main(
        ["--config", str(config_path), "queue", "watch", "--notify-workflow", workflow.id]
    )

    assert result == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {
        "event": "owner_terminal_check_failed",
        "workflow_id": workflow.id,
        "error": "no such table: workflows",
    } in events
    assert events[-1] == {"event": "owner_terminal_unavailable", "workflow_id": workflow.id}


def test_workflow_status_reports_persisted_observer_diagnostics(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.jsonc"
    state_dir = tmp_path / "state"
    write_config(config_path, state_dir=state_dir)
    store = WorkflowStore(state_dir)
    workflow = store.create(tmp_path, "single", "owner", "Do work.")
    store.transition(workflow.id, "starting")
    store.attach_external(
        workflow.id,
        adapter_reference="repo::/tmp/owner",
        worktree_path="/tmp/owner",
        terminal_handle="term-owner",
    )
    store.transition(workflow.id, "running")
    store.add_owned_terminal(workflow.id, "observer", "observer")
    store.detach_observers_for_unavailable_agent(workflow.id, "term-owner")

    assert main(["--config", str(config_path), "workflow", "status", workflow.id]) == 0

    payload = json.loads(capsys.readouterr().out)
    observer = payload["observer"]
    assert observer["enabled"] is True
    assert observer["owned_handles"] == []
    assert observer["owner_terminal_valid"] is False
    assert observer["last_termination"]["reason"] == "owner_terminal_unavailable"
    assert payload["progress"] == {
        "stage": "running",
        "current_review_cycle": None,
        "max_review_cycles": None,
        "roles": [
            {
                "id": workflow.id,
                "role": "single",
                "slot": 0,
                "status": "running",
                "error": None,
            }
        ],
        "readiness": [],
    }


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

        def set_lifecycle(self, worktree_id: str, state: str, _detail=None, **_kwargs) -> None:
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

        def set_lifecycle(self, worktree_id: str, state: str, _detail=None, **_kwargs) -> None:
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

        def terminal_is_valid(self, _worktree_id: str, _handle: str | None) -> bool:
            return True

    _install_orca(monkeypatch, FakeOrcaClient)

    result = main(["--config", str(config_path), "workflow", "observe", workflow.id])

    assert result == 0
    assert store.owned_terminal_handles(workflow.id) == ["agent-1", "observer-1"]
    assert json.loads(capsys.readouterr().out)["terminal_handle"] == "observer-1"
    assert store.get(workflow.id).queue_observer_enabled is True


def test_observe_rejects_a_missing_owner_terminal(tmp_path: Path, capsys, monkeypatch) -> None:
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

        def create_observer(self, *_args, **_kwargs):
            raise AssertionError("dead owner must not open an observer")

        def terminal_is_valid(self, _worktree_id: str, _handle: str | None) -> bool:
            return False

    _install_orca(monkeypatch, FakeOrcaClient)

    result = main(["--config", str(config_path), "workflow", "observe", workflow.id])

    assert result == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "workflow resume" in captured.err
    assert store.owned_terminal_handles(workflow.id, kind="observer") == []


def test_workflow_list_reports_observer_facts(tmp_path: Path, capsys, monkeypatch) -> None:
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
    store.set_queue_observer_enabled(workflow.id, True)

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def terminal_is_valid(self, _worktree_id: str, _handle: str | None) -> bool:
            return True

    _install_orca(monkeypatch, FakeOrcaClient)

    assert main(["--config", str(config_path), "workflow", "list", "--json"]) == 0
    row = json.loads(capsys.readouterr().out)["workflows"][0]
    assert row["id"] == workflow.id
    assert row["queue_observer_enabled"] is True
    assert row["owner_terminal_valid"] is True
    assert row["observer_stop_reason"] is None


def test_observer_command_uses_the_current_interpreter_and_quotes_config_path(
    tmp_path: Path,
) -> None:
    command = _observer_command(tmp_path / "a space;not-a-command.jsonc", "workflow-123")

    shell_command = __import__("shlex").split(command)
    assert shell_command[:2] == ["/bin/sh", "-c"]
    parsed = __import__("shlex").split(shell_command[2])
    assert parsed[-4:] == ["queue", "watch", "--notify-workflow", "workflow-123"]
    assert parsed[-5] == str(tmp_path / "a space;not-a-command.jsonc")
    assert parsed[1:3] == ["-m", "flybridge_cli.main"]


def test_coordinator_command_uses_posix_shell_for_zsh_hosts(tmp_path: Path) -> None:
    command = _coordinator_command(tmp_path / "a space.jsonc", "manager-123")
    shell_command = __import__("shlex").split(command)
    assert shell_command[:2] == ["/bin/sh", "-c"]
    parsed = __import__("shlex").split(shell_command[2])

    assert parsed[-2:] == ["supervise", "manager-123"]
    assert parsed[-3] == "workflow"


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

        def set_lifecycle(self, *_args, **_kwargs) -> dict[str, bool]:
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
                "workflow",
                "start",
                str(tmp_path),
                "--mode",
                mode,
                "--objective",
                "Inspect.",
                "--issue",
                ISSUE_URL,
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

        def set_lifecycle(self, *_args, **_kwargs) -> dict[str, bool]:
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
                "workflow",
                "start",
                str(tmp_path),
                "--mode",
                "orchestrated",
                "--objective",
                "Inspect.",
                "--issue",
                ISSUE_URL,
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
    artifacts = WorkflowArtifactStore(tmp_path / "state")
    artifacts.put(manager_id, "plan", "# Plan\n\nImplement the requested change.")

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
    assert main(["--config", str(config_path), "workflow", "complete", manager_id]) == 0
    capsys.readouterr()
    assert main(["--config", str(config_path), "workflow", "advance", manager_id]) == 0
    capsys.readouterr()
    assert started_roles == ["manager", "worker"]
    assert "Manager plan is complete." in prompts[-1]
    assert ISSUE_URL in prompts[0]
    assert ISSUE_URL in prompts[-1]
    artifacts.put(worker_id, "verification", "# Verification\n\nAll required checks passed.")
    assert start_options[-1]["role"] == "worker"
    assert start_options[-1]["parent_worktree_id"] == "id:manager"
    assert start_options[-1]["base_branch"] == "main"
    assert str(start_options[-1]["comment"]).startswith(ISSUE_URL)
    assert "<!-- flybridge:run=" in str(start_options[-1]["comment"])
    assert ";step=" in str(start_options[-1]["comment"])
    assert start_options[-1]["github_issue_number"] is None

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
    assert main(["--config", str(config_path), "workflow", "complete", worker_id]) == 0
    capsys.readouterr()
    assert main(["--config", str(config_path), "workflow", "advance", manager_id]) == 0
    capsys.readouterr()
    assert started_roles == ["manager", "worker", "reviewer"]
    assert "Worker verification is ready for review." in prompts[-1]
    assert ISSUE_URL in prompts[-1]
    assert start_options[-1]["role"] == "reviewer"
    assert start_options[-1]["parent_worktree_id"] == "id:worker"
    assert start_options[-1]["base_branch"] == "main"
    assert str(start_options[-1]["comment"]).startswith(ISSUE_URL)
    assert "<!-- flybridge:run=" in str(start_options[-1]["comment"])
    assert ";step=" in str(start_options[-1]["comment"])
    assert start_options[-1]["github_issue_number"] is None
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

        def set_lifecycle(self, *_args, **_kwargs) -> dict[str, bool]:
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
                "workflow",
                "start",
                str(tmp_path),
                "--mode",
                "orchestrated",
                "--objective",
                "Inspect.",
                "--issue",
                ISSUE_URL,
            ]
        )
        == 0
    )
    start_output = json.loads(capsys.readouterr().out)
    manager_id = start_output["workflow"]["id"]
    worker_id = next(
        child["id"] for child in start_output["planned_children"] if child["role"] == "worker"
    )
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
    assert main(["--config", str(config_path), "workflow", "complete", manager_id]) == 0
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

        def set_lifecycle(self, *_args, **_kwargs) -> dict[str, bool]:
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
                "workflow",
                "start",
                str(tmp_path),
                "--mode",
                mode,
                "--objective",
                "Run checks.",
                "--issue",
                ISSUE_URL,
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    store = WorkflowStore(tmp_path / "state")
    workflow_id = output["workflow"]["id"]
    assert "`device`" in captured["prompt"]
    assert "report the request-id and park" in captured["prompt"]
    assert "--notify-workflow" in captured["observer_command"]
    assert workflow_id in captured["observer_command"]
    observer_argv = __import__("shlex").split(captured["observer_command"])
    assert __import__("shlex").split(observer_argv[2])[-1] == workflow_id
    expected_handles = ["agent-terminal", "observer-terminal", "terminal:coordinator"]
    assert store.owned_terminal_handles(workflow_id) == expected_handles


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

        def set_lifecycle(self, *_args, **_kwargs) -> dict[str, bool]:
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
                "workflow",
                "start",
                str(tmp_path),
                "--objective",
                "Run checks.",
                "--no-queue-observer",
                "--issue",
                ISSUE_URL,
            ]
        )
        == 0
    )
    capsys.readouterr()


def test_workflow_retry_command_resets_a_failed_record(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        state_dir=tmp_path / "state",
        reconcile={"auto_before_workflow_commands": False},
    )
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
    received: dict[str, object] = {}

    class FakeProject:
        def __init__(self, _config) -> None:
            self.seen_status_names = ()
            self.seen_priority_names = ()

        def screen(self, **kwargs):
            received.update(kwargs)
            return [
                Candidate(
                    "example/repo",
                    1,
                    "Task",
                    "u",
                    "Todo",
                    "High",
                    ("alice",),
                    "example",
                    1,
                )
            ]

    monkeypatch.setattr("flybridge_cli.commands.auxiliary.GitHubProject", FakeProject)

    assert main(["--config", str(config_path), "board", "screen"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["issues"][0]["number"] == 1
    assert received["assignee"] == "alice"
    assert received["boards"] is None


def test_inventory_cli_prints_filtered_facts(tmp_path: Path, capsys, monkeypatch) -> None:
    repo = tmp_path / "keep"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.email", "a@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "A"], cwd=repo, check=True)
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, orca={"agents": {"single": "codex"}}, github=ENABLED_GITHUB)

    class FakeOrca:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def verify(self):
            return {"app_version": "test", "skill": "orchestration"}

        def list_worktrees(self):
            from flybridge_orca.client import ListedWorktree

            return (
                (
                    ListedWorktree(
                        "repo::" + str(repo),
                        str(repo),
                        "keep",
                        "todo",
                        "",
                        "main",
                        None,
                        None,
                    ),
                    ListedWorktree(
                        "repo::/tmp/other",
                        str(tmp_path / "other"),
                        "other",
                        "todo",
                        "",
                        "main",
                        None,
                        None,
                    ),
                ),
                False,
            )

    class FakePullRequests:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def list_open(self, repositories):
            assert repositories == []
            return {}

    monkeypatch.setattr("flybridge_cli.runtime.OrcaClient", FakeOrca)
    monkeypatch.setattr("flybridge_cli.commands.inventory.GitHubPullRequests", FakePullRequests)

    assert (
        main(
            [
                "--config",
                str(config_path),
                "inventory",
                "--path-prefix",
                str(repo),
                "--no-github",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert [row["orca"]["name"] for row in payload["worktrees"]] == ["keep"]
    assert "pull_requests" not in payload["worktrees"][0]


def test_inventory_cli_applies_configured_exclude_worktrees(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    keep = tmp_path / "keep"
    skipped = tmp_path / "flybridge"
    for repo in (keep, skipped):
        repo.mkdir()
        subprocess.run(
            ["git", "init", "--initial-branch=main"], cwd=repo, check=True, capture_output=True
        )
        subprocess.run(["git", "config", "user.email", "a@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "A"], cwd=repo, check=True)
        (repo / "README.md").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        orca={"agents": {"single": "codex"}},
        reconcile={"exclude_worktrees": ["flybridge"]},
    )

    class FakeOrca:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def verify(self):
            return {"app_version": "test", "skill": "orchestration"}

        def list_worktrees(self):
            from flybridge_orca.client import ListedWorktree

            return (
                (
                    ListedWorktree(
                        "repo::" + str(keep),
                        str(keep),
                        "keep",
                        "todo",
                        "",
                        "main",
                        None,
                        None,
                    ),
                    ListedWorktree(
                        "repo::" + str(skipped),
                        str(skipped),
                        "flybridge",
                        "todo",
                        "",
                        "main",
                        None,
                        None,
                    ),
                ),
                False,
            )

    monkeypatch.setattr("flybridge_cli.runtime.OrcaClient", FakeOrca)

    assert main(["--config", str(config_path), "inventory", "--no-github"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [row["orca"]["name"] for row in payload["worktrees"]] == ["keep"]


def test_inventory_cli_collapses_duplicate_checkout_paths(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    repo = tmp_path / "keep"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.email", "a@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "A"], cwd=repo, check=True)
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/example/repo.git"],
        cwd=repo,
        check=True,
    )
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, orca={"agents": {"single": "codex"}}, github=ENABLED_GITHUB)
    listed_calls = {"github": 0}

    class FakeOrca:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def verify(self):
            return {"app_version": "test", "skill": "orchestration"}

        def list_worktrees(self):
            from flybridge_orca.client import ListedWorktree

            return (
                (
                    ListedWorktree(
                        "repo::alias-a::" + str(repo),
                        str(repo),
                        "alias-a",
                        "todo",
                        "",
                        "main",
                        None,
                        None,
                    ),
                    ListedWorktree(
                        "repo::alias-b::" + str(repo),
                        str(repo),
                        "alias-b",
                        "in-progress",
                        "https://github.com/example/repo/issues/1",
                        "main",
                        1,
                        None,
                        "github:example/repo",
                    ),
                ),
                False,
            )

    class FakePullRequests:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def list_open(self, repositories):
            listed_calls["github"] += 1
            assert list(repositories) == ["example/repo"]
            return {}, ()

        def list_by_head(self, repository, head_ref_name):
            listed_calls["github"] += 1
            return (), None

        def get(self, repository, number):
            listed_calls["github"] += 1
            return None, None

    monkeypatch.setattr("flybridge_cli.runtime.OrcaClient", FakeOrca)
    monkeypatch.setattr("flybridge_cli.commands.inventory.GitHubPullRequests", FakePullRequests)

    assert main(["--config", str(config_path), "inventory"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["worktrees"]) == 1
    row = payload["worktrees"][0]["orca"]
    assert row["name"] == "alias-b"
    assert row["aliases"] == [
        {"id": "repo::alias-a::" + str(repo), "name": "alias-a", "workspace_status": "todo"}
    ]
    assert listed_calls["github"] == 2


def test_inventory_cli_isolates_github_repository_failures(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    good = tmp_path / "good"
    missing = tmp_path / "missing"
    for path, remote in (
        (good, "https://github.com/example/repo.git"),
        (missing, "https://github.com/example/missing.git"),
    ):
        path.mkdir()
        subprocess.run(
            ["git", "init", "--initial-branch=main"], cwd=path, check=True, capture_output=True
        )
        subprocess.run(["git", "config", "user.email", "a@example.invalid"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.name", "A"], cwd=path, check=True)
        (path / "README.md").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", remote], cwd=path, check=True)
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, orca={"agents": {"single": "codex"}}, github=ENABLED_GITHUB)

    class FakeOrca:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def verify(self):
            return {"app_version": "test", "skill": "orchestration"}

        def list_worktrees(self):
            from flybridge_orca.client import ListedWorktree

            return (
                (
                    ListedWorktree(
                        "repo::" + str(good),
                        str(good),
                        "good",
                        "todo",
                        "",
                        "main",
                        None,
                        None,
                    ),
                    ListedWorktree(
                        "repo::" + str(missing),
                        str(missing),
                        "missing",
                        "todo",
                        "",
                        "main",
                        None,
                        None,
                    ),
                ),
                False,
            )

    class FakePullRequests:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def list_open(self, repositories):
            from flybridge_github import PullRequestFact, PullRequestQueryFailure

            assert set(repositories) == {"example/repo", "example/missing"}
            fact = PullRequestFact(
                "example/repo",
                4,
                "Ready",
                "https://example.test/4",
                "OPEN",
                False,
                "MERGEABLE",
                "CLEAN",
                "APPROVED",
                "main",
                ("alice",),
                (),
                0,
                0,
            )
            return (
                {"example/repo": (fact,)},
                (
                    PullRequestQueryFailure(
                        "example/missing",
                        "gh: Could not resolve to a Repository with the name 'example/missing'.",
                    ),
                ),
            )

        def list_by_head(self, repository, head_ref_name):
            raise AssertionError(f"failed repository must not be re-queried: {repository}")

        def get(self, repository, number):
            return None, None

    monkeypatch.setattr("flybridge_cli.runtime.OrcaClient", FakeOrca)
    monkeypatch.setattr("flybridge_cli.commands.inventory.GitHubPullRequests", FakePullRequests)

    assert main(["--config", str(config_path), "inventory"]) == 2
    payload = json.loads(capsys.readouterr().out)
    rows = {row["orca"]["name"]: row for row in payload["worktrees"]}
    assert rows["good"]["pull_requests"][0]["number"] == 4
    assert rows["missing"]["pull_requests"] == []
    assert rows["missing"]["errors"] == [
        "GitHub pull requests were unavailable for example/missing"
    ]
    assert payload["failures"] == [
        "gh: Could not resolve to a Repository with the name 'example/missing'."
    ]


def test_inventory_cli_skips_configured_github_repositories(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    product = tmp_path / "product"
    fixture = tmp_path / "fixture"
    for path, remote in (
        (product, "https://github.com/example/repo.git"),
        (fixture, "https://github.com/flybridge-review-fixture/disposable.git"),
    ):
        path.mkdir()
        subprocess.run(
            ["git", "init", "--initial-branch=main"], cwd=path, check=True, capture_output=True
        )
        subprocess.run(["git", "config", "user.email", "a@example.invalid"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.name", "A"], cwd=path, check=True)
        (path / "README.md").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", remote], cwd=path, check=True)
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        orca={"agents": {"single": "codex"}},
        github={**ENABLED_GITHUB, "skip_repositories": ["flybridge-review-fixture/"]},
    )
    queried: list[tuple[str, ...]] = []

    class FakeOrca:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def verify(self):
            return {"app_version": "test", "skill": "orchestration"}

        def list_worktrees(self):
            from flybridge_orca.client import ListedWorktree

            return (
                (
                    ListedWorktree(
                        "repo::" + str(product),
                        str(product),
                        "product",
                        "todo",
                        "",
                        "main",
                        None,
                        None,
                    ),
                    ListedWorktree(
                        "repo::" + str(fixture),
                        str(fixture),
                        "fixture",
                        "todo",
                        "",
                        "main",
                        None,
                        None,
                    ),
                ),
                False,
            )

    class FakePullRequests:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def list_open(self, repositories):
            queried.append(tuple(repositories))
            from flybridge_github import PullRequestFact

            fact = PullRequestFact(
                "example/repo",
                4,
                "Ready",
                "https://example.test/4",
                "OPEN",
                False,
                "MERGEABLE",
                "CLEAN",
                "APPROVED",
                "main",
                ("alice",),
                (),
                0,
                0,
            )
            return {"example/repo": (fact,)}, ()

        def list_by_head(self, repository, head_ref_name):
            return (), None

        def get(self, repository, number):
            return None, None

    monkeypatch.setattr("flybridge_cli.runtime.OrcaClient", FakeOrca)
    monkeypatch.setattr("flybridge_cli.commands.inventory.GitHubPullRequests", FakePullRequests)

    assert main(["--config", str(config_path), "inventory"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert queried == [("example/repo",)]
    assert payload["skipped_github_repositories"] == ["flybridge-review-fixture/disposable"]
    assert payload["failures"] == []
    assert payload["warnings"] == []


def test_inventory_cli_treats_transient_github_errors_as_warnings(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    repo = tmp_path / "work"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.email", "a@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "A"], cwd=repo, check=True)
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/google/googletest.git"],
        cwd=repo,
        check=True,
    )
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, orca={"agents": {"single": "codex"}}, github=ENABLED_GITHUB)

    class FakeOrca:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def verify(self):
            return {"app_version": "test", "skill": "orchestration"}

        def list_worktrees(self):
            from flybridge_orca.client import ListedWorktree

            return (
                (
                    ListedWorktree(
                        "repo::" + str(repo),
                        str(repo),
                        "work",
                        "todo",
                        "",
                        "main",
                        None,
                        None,
                    ),
                ),
                False,
            )

    class FakePullRequests:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def list_open(self, repositories):
            from flybridge_github import PullRequestQueryFailure

            assert repositories == ("google/googletest",)
            return {}, (PullRequestQueryFailure("google/googletest", "gh: HTTP 502"),)

        def list_by_head(self, repository, head_ref_name):
            raise AssertionError("transient failures must not be re-queried by head")

        def get(self, repository, number):
            return None, None

    monkeypatch.setattr("flybridge_cli.runtime.OrcaClient", FakeOrca)
    monkeypatch.setattr("flybridge_cli.commands.inventory.GitHubPullRequests", FakePullRequests)

    assert main(["--config", str(config_path), "inventory"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["failures"] == []
    assert payload["warnings"] == ["google/googletest: gh: HTTP 502"]
    assert payload["worktrees"][0]["errors"] == []


def test_inventory_cli_exits_when_github_collection_raises(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    repo = tmp_path / "work"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.email", "a@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "A"], cwd=repo, check=True)
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/example/repo.git"],
        cwd=repo,
        check=True,
    )
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, orca={"agents": {"single": "codex"}}, github=ENABLED_GITHUB)

    class FakeOrca:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def verify(self):
            return {"app_version": "test", "skill": "orchestration"}

        def list_worktrees(self):
            from flybridge_orca.client import ListedWorktree

            return (
                (
                    ListedWorktree(
                        "repo::" + str(repo),
                        str(repo),
                        "work",
                        "todo",
                        "",
                        "main",
                        None,
                        None,
                    ),
                ),
                False,
            )

    class FakePullRequests:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def list_open(self, repositories):
            raise GitHubPullRequestError("gh: authentication failed")

    monkeypatch.setattr("flybridge_cli.runtime.OrcaClient", FakeOrca)
    monkeypatch.setattr("flybridge_cli.commands.inventory.GitHubPullRequests", FakePullRequests)

    assert main(["--config", str(config_path), "inventory"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["worktrees"][0]["pull_requests"] == []
    assert payload["failures"] == ["example/repo: gh: authentication failed"]


def test_inventory_cli_attaches_review_facts_only_when_requested(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    repo = tmp_path / "work"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.email", "a@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "A"], cwd=repo, check=True)
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/example/repo.git"],
        cwd=repo,
        check=True,
    )
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, orca={"agents": {"single": "codex"}}, github=ENABLED_GITHUB)
    review_calls: list[tuple[str, int]] = []

    class FakeOrca:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def verify(self):
            return {"app_version": "test", "skill": "orchestration"}

        def list_worktrees(self):
            from flybridge_orca.client import ListedWorktree

            return (
                (
                    ListedWorktree(
                        "repo::" + str(repo),
                        str(repo),
                        "work",
                        "todo",
                        "",
                        "main",
                        None,
                        None,
                    ),
                ),
                False,
            )

    class FakePullRequests:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def list_open(self, repositories):
            from flybridge_github import PullRequestFact

            assert repositories == ("example/repo",)
            fact = PullRequestFact(
                "example/repo",
                4,
                "Ready",
                "https://example.test/4",
                "OPEN",
                False,
                "MERGEABLE",
                "CLEAN",
                "APPROVED",
                "main",
                ("alice",),
                (),
                0,
                0,
                "main",
            )
            return {"example/repo": (fact,)}, ()

        def list_by_head(self, repository, head_ref_name):
            return (), None

        def get(self, repository, number):
            return None, None

        def get_review_facts(self, repository, number, *, head_ref_name=None):
            review_calls.append((repository, number))
            return (
                {
                    "body": "Summary",
                    "behind_by": 0,
                    "truncated": False,
                    "reviews": [
                        {
                            "login": "alice",
                            "is_bot": False,
                            "author_association": "MEMBER",
                            "state": "APPROVED",
                            "submitted_at": "2026-09-18T00:00:00Z",
                            "body": "ok",
                        }
                    ],
                    "comments": [],
                    "threads": [],
                },
                None,
            )

    monkeypatch.setattr("flybridge_cli.runtime.OrcaClient", FakeOrca)
    monkeypatch.setattr("flybridge_cli.commands.inventory.GitHubPullRequests", FakePullRequests)

    assert main(["--config", str(config_path), "inventory"]) == 0
    thin = json.loads(capsys.readouterr().out)
    assert "review_facts" not in thin["worktrees"][0]["pull_requests"][0]
    assert thin["worktrees"][0]["pull_requests"][0]["matched_from"] == "parent"
    assert review_calls == []

    assert main(["--config", str(config_path), "inventory", "--with-review-facts"]) == 0
    thick = json.loads(capsys.readouterr().out)
    assert review_calls == [("example/repo", 4)]
    assert thick["worktrees"][0]["pull_requests"][0]["review_facts"]["body"] == "Summary"


def test_board_screen_cli_forwards_filters(tmp_path: Path, capsys, monkeypatch) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, github=ENABLED_GITHUB)
    received: dict[str, object] = {}

    class FakeProject:
        def __init__(self, _config) -> None:
            self.seen_status_names = ()
            self.seen_priority_names = ()

        def screen(self, **kwargs):
            received.update(kwargs)
            self.seen_status_names = tuple(kwargs.get("statuses") or ())
            self.seen_priority_names = tuple(kwargs.get("priorities") or ())
            return []

    monkeypatch.setattr("flybridge_cli.commands.auxiliary.GitHubProject", FakeProject)

    assert (
        main(
            [
                "--config",
                str(config_path),
                "board",
                "screen",
                "--all-assignees",
                "--status",
                "Todo",
                "--priority",
                "High",
                "--board",
                "10",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {"count": 0, "issues": []}
    assert received["assignee"] is None
    assert received["statuses"] == ["Todo"]
    assert received["priorities"] == ["High"]
    assert received["boards"] == ["10"]


def test_board_screen_cli_warns_when_status_filter_is_unseen(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, github=ENABLED_GITHUB)

    class FakeProject:
        def __init__(self, _config) -> None:
            self.seen_status_names = ()
            self.seen_priority_names = ()

        def screen(self, **kwargs):
            self.seen_status_names = ("In progress",)
            self.seen_priority_names = ("High",)
            return []

    monkeypatch.setattr("flybridge_cli.commands.auxiliary.GitHubProject", FakeProject)

    assert (
        main(
            [
                "--config",
                str(config_path),
                "board",
                "screen",
                "--status",
                "Done",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 0
    assert payload["warnings"] == ["no Project Status matched Done; seen: In progress"]


def test_prs_screen_cli_lists_authored_pull_requests(tmp_path: Path, capsys, monkeypatch) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, github=ENABLED_GITHUB)
    captured: dict[str, object] = {}

    class FakePullRequests:
        def __init__(self, *args, **kwargs) -> None:
            captured["user"] = kwargs.get("user")

        def list_authored(self, author, states):
            from flybridge_github.pull_requests import PullRequestCheck, PullRequestFact

            captured["author"] = author
            captured["states"] = tuple(states)
            return (
                PullRequestFact(
                    "example/repo",
                    67,
                    "Heartbeat",
                    "https://example.test/67",
                    "OPEN",
                    False,
                    "MERGEABLE",
                    "CLEAN",
                    None,
                    "feature",
                    ("alice",),
                    (PullRequestCheck("review", "COMPLETED", "SUCCESS"),),
                    0,
                    0,
                    "develop",
                    "alice",
                ),
            )

        def get_review_facts(self, repository, number, *, head_ref_name=None):
            captured["review"] = (repository, number, head_ref_name)
            return None, None

    monkeypatch.setattr("flybridge_cli.commands.auxiliary.GitHubPullRequests", FakePullRequests)

    assert main(["--config", str(config_path), "prs", "screen"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert captured["user"] == "alice"
    assert captured["author"] == "alice"
    assert captured["states"] == ("OPEN",)
    assert payload["author"] == "alice"
    assert payload["pull_requests"][0]["number"] == 67
    assert payload["pull_requests"][0]["author"] == "alice"
    assert "review_facts" not in payload["pull_requests"][0]

    assert main(["--config", str(config_path), "prs", "screen", "--with-review-facts"]) == 0
    thick = json.loads(capsys.readouterr().out)
    assert captured["review"] == ("example/repo", 67, "feature")
    assert thick["pull_requests"][0]["checks"][0]["conclusion"] == "SUCCESS"


def test_prs_refresh_base_cli_patches_the_pull_request(tmp_path: Path, capsys, monkeypatch) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, github=ENABLED_GITHUB)
    captured: dict[str, object] = {}

    class FakePullRequests:
        def __init__(self, *args, **kwargs) -> None:
            captured["user"] = kwargs.get("user")

        def refresh_base(self, repository, number):
            captured["refresh"] = (repository, number)
            return {
                "repository": repository,
                "number": number,
                "base_ref_name": "main",
                "refreshed": True,
            }

    monkeypatch.setattr("flybridge_cli.commands.auxiliary.GitHubPullRequests", FakePullRequests)

    assert main(["--config", str(config_path), "prs", "refresh-base", "example/repo", "9"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert captured["user"] == "alice"
    assert captured["refresh"] == ("example/repo", 9)
    assert payload["refreshed"] is True
    assert payload["base_ref_name"] == "main"


def test_board_screen_cli_rejects_assignee_and_all_assignees(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, github=ENABLED_GITHUB)

    assert (
        main(
            [
                "--config",
                str(config_path),
                "board",
                "screen",
                "--assignee",
                "bob",
                "--all-assignees",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "pass only one of --assignee and --all-assignees" in captured.err


def test_board_screen_cli_reports_malformed_responses_without_a_traceback(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, github=ENABLED_GITHUB)

    class FakeProject:
        def __init__(self, _config):
            pass

        def screen(self, **_kwargs):
            raise GitHubProjectError("unexpected GitHub Project response")

    monkeypatch.setattr("flybridge_cli.commands.auxiliary.GitHubProject", FakeProject)

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
        "flybridge_cli.commands.auxiliary.shutil.which",
        lambda executable: "/usr/bin/orca" if executable.startswith("orca") else None,
    )

    assert main(["--config", str(config_path), "doctor"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["missing_agents"] == ["single"]
    assert report["github_enabled"] is True
    assert report["gh"] is False


def test_doctor_fails_when_cursor_cli_is_unresolved(tmp_path: Path, capsys, monkeypatch) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, orca={"agents": {"single": "cursor"}})

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def verify(self) -> dict[str, str]:
            return {"app_version": "test"}

    _install_orca(monkeypatch, FakeOrcaClient)
    monkeypatch.setattr(
        "flybridge_cli.commands.auxiliary.shutil.which",
        lambda executable: "/usr/bin/orca-ide" if executable.startswith("orca") else None,
    )

    assert main(["--config", str(config_path), "doctor", "--mode", "single"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["unresolved_agents"] == ["single"]
    assert report["agent_cli_resolvable"] is False


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

    assert (
        main(
            ["--config", str(config_path), "workflow", "start", str(tmp_path), "--objective", "   "]
        )
        == 2
    )
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


def test_workflow_resume_enables_the_configured_queue_observer(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    state_dir = tmp_path / "state"
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        state_dir=state_dir,
        orca={"agents": {"single": "codex"}},
        queue={"observer": True, "resources": ["heavy-check"]},
    )
    store = WorkflowStore(state_dir)
    workflow = store.create(tmp_path, "single", "resume-observer", "Continue implementation.")
    store.begin_start(workflow.id)
    store.attach_external(
        workflow.id,
        adapter_reference="repo::/tmp/existing",
        worktree_path="/tmp/existing",
        terminal_handle="agent-existing",
    )
    store.transition(workflow.id, "running")
    created: list[str] = []

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def verify_worktree(self, *_args) -> None:
            pass

        def terminal_is_valid(self, *_args) -> bool:
            return True

        def wait_for_agent(self, _terminal_handle: str) -> None:
            pass

        def send_prompt(self, *_args) -> None:
            pass

        def create_observer(self, _worktree_id: str, command: str) -> str:
            created.append(command)
            return "observer-new"

    _install_orca(monkeypatch, FakeOrcaClient)

    assert main(["--config", str(config_path), "workflow", "resume", workflow.id]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["workflow"]["queue_observer_enabled"] is True
    assert store.owned_terminal_handles(workflow.id, kind="observer") == ["observer-new"]
    assert "--notify-workflow" in created[0]
    assert workflow.id in created[0]


def test_generated_workflow_names_are_unique_within_one_second() -> None:
    names = {_generated_workflow_name() for _ in range(100)}

    assert len(names) == 100
    assert all(name.startswith("flybridge-") for name in names)


def test_start_parser_exposes_explicit_duplicate_override() -> None:
    arguments = build_parser().parse_args(
        ["workflow", "start", ".", "--objective", "Inspect.", "-d"]
    )

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
                "workflow",
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

        def set_lifecycle(self, *_args, **_kwargs):
            return {"updated": True}

        def close_terminals(self, *_args):
            return None

        def remove_worktree(self, *_args):
            return {}

    _install_orca(monkeypatch, FakeOrcaClient)

    assert main(["--config", str(config_path), "workflow", "launch", workflow.id]) == 0
    assert json.loads(capsys.readouterr().out)["workflow"]["status"] == "running"


def test_workflow_launch_and_advance_open_notifying_observers(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = tmp_path / "config.jsonc"
    state_dir = tmp_path / "state"
    write_config(
        config_path,
        state_dir=state_dir,
        orca={
            "agents": {
                "single": "codex",
                "manager": "codex",
                "worker": "codex",
                "reviewer": "codex",
            }
        },
        queue={"observer": True, "resources": []},
    )
    captured: list[str] = []

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def prepare_repository(self, _repository: Path) -> None:
            pass

        def register_repository(self, _repository: Path) -> None:
            pass

        def start(self, _repository, name, mode, _agent, _prompt, **_kwargs):
            return StartedWorkflow(name, f"id:{name}", str(tmp_path / name), f"term:{name}", mode)

        def current_branch(self, _worktree_path: str) -> str:
            return "main"

        def uncommitted_changes(self, _worktree_path: str) -> tuple[str, ...]:
            return ()

        def set_lifecycle(self, *_args, **_kwargs) -> dict[str, bool]:
            return {"updated": True}

        def create_observer(self, _worktree_id: str, command: str) -> str:
            captured.append(command)
            return f"observer-{len(captured)}"

        def close_terminals(self, *_args) -> dict[str, bool]:
            return {"closed": True}

        def remove_worktree(self, *_args) -> dict[str, bool]:
            return {"removed": True}

    _install_orca(monkeypatch, FakeOrcaClient)

    store = WorkflowStore(state_dir)
    single = store.create(tmp_path, "single", "retried-observer", "Implement.")
    assert main(["--config", str(config_path), "workflow", "launch", single.id]) == 0
    capsys.readouterr()
    assert any(single.id in command for command in captured)

    assert (
        main(
            [
                "--config",
                str(config_path),
                "workflow",
                "start",
                str(tmp_path),
                "--mode",
                "orchestrated",
                "--objective",
                "Inspect observer advance.",
                "--allow-duplicate",
                "--issue",
                ISSUE_URL,
            ]
        )
        == 0
    )
    start_output = json.loads(capsys.readouterr().out)
    manager_id = start_output["workflow"]["id"]
    worker_id = next(
        child["id"] for child in start_output["planned_children"] if child["role"] == "worker"
    )
    WorkflowArtifactStore(state_dir).put(
        manager_id, "plan", "# Plan\n\nInspect observer advance behavior."
    )
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
    assert main(["--config", str(config_path), "workflow", "complete", manager_id]) == 0
    capsys.readouterr()
    captured.clear()
    assert main(["--config", str(config_path), "workflow", "advance", manager_id]) == 0
    capsys.readouterr()
    assert captured
    assert worker_id in captured[0]
    assert "--notify-workflow" in captured[0]


def test_workflow_advance_rejects_a_non_manager_identifier(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.jsonc"
    state_dir = tmp_path / "state"
    write_config(
        config_path,
        state_dir=state_dir,
        reconcile={"auto_before_workflow_commands": False},
    )
    store = WorkflowStore(state_dir)
    workflow = store.create(tmp_path, "single", "single-root", "Implement.")

    result = main(["--config", str(config_path), "workflow", "advance", workflow.id])

    assert result == 2
    assert "orchestrated manager" in capsys.readouterr().err


def test_attach_existing_skips_submodule_prepare_and_does_not_create_worktree(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, state_dir=tmp_path / "state", orca={"agents": {"single": "cursor"}})
    prepared = False
    calls: list[str] = []
    issue_updates: list[tuple[str, str, int | None]] = []

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def prepare_repository(self, _repository: Path) -> None:
            nonlocal prepared
            prepared = True

        def register_repository(self, _repository: Path) -> dict[str, str]:
            calls.append("register")
            return {"registered": True}

        def start(self, *_args, **_kwargs):
            raise AssertionError("attach-existing must not create a worktree")

        def existing_worktree(self, repository: Path) -> tuple[str, str]:
            calls.append("existing")
            return "repo::" + str(repository), str(repository)

        def attach_new_agent(self, repository, name, mode, agent, prompt, **_kwargs):
            calls.append("attach")
            return StartedWorkflow(
                name,
                "repo::" + str(repository),
                str(repository),
                "term-attach",
                mode,
                owns_worktree=False,
            )

        def set_lifecycle(self, *_args, **_kwargs):
            return {"updated": True}

        def set_issue_comment(
            self,
            worktree_id: str,
            issue_url: str,
            *,
            github_issue_number: int | None = None,
        ):
            issue_updates.append((worktree_id, issue_url, github_issue_number))
            return {"updated": True}

        def close_terminals(self, *_args):
            return {"closed": True}

        def remove_worktree(self, *_args):
            raise AssertionError("attach-existing must not remove a worktree")

    _install_orca(monkeypatch, FakeOrcaClient)

    result = main(
        [
            "--config",
            str(config_path),
            "workflow",
            "start",
            str(tmp_path),
            "--mode",
            "single",
            "--attach-existing",
            "--objective",
            "Fix the existing PR.",
            "--issue",
            ISSUE_URL,
        ]
    )

    assert result == 0
    assert prepared is False
    assert calls == ["existing", "attach"]
    assert issue_updates == [
        ("repo::" + str(tmp_path), ISSUE_URL, None),
    ]
    payload = json.loads(capsys.readouterr().out)
    assert payload["workflow"]["owns_worktree"] is False


def test_attach_existing_opens_the_configured_queue_observer(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        state_dir=tmp_path / "state",
        orca={"agents": {"single": "cursor"}},
        queue={"observer": True, "resources": ["heavy-check"]},
    )
    captured: dict[str, str] = {}

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def existing_worktree(self, repository: Path) -> tuple[str, str]:
            return "repo::" + str(repository), str(repository)

        def attach_new_agent(self, repository, name, mode, agent, prompt, **_kwargs):
            captured["prompt"] = prompt
            return StartedWorkflow(
                name,
                "repo::" + str(repository),
                str(repository),
                "term-attach",
                mode,
                owns_worktree=False,
            )

        def create_observer(self, _worktree_id: str, command: str) -> str:
            captured["observer_command"] = command
            return "observer-terminal"

        def set_lifecycle(self, *_args, **_kwargs):
            return {"updated": True}

        def close_terminals(self, *_args):
            return {"closed": True}

        def remove_worktree(self, *_args):
            raise AssertionError("attach-existing must not remove a worktree")

    _install_orca(monkeypatch, FakeOrcaClient)
    result = main(
        [
            "--config",
            str(config_path),
            "workflow",
            "start",
            str(tmp_path),
            "--attach-existing",
            "--objective",
            "Fix the existing PR.",
            "--issue",
            ISSUE_URL,
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    workflow_id = payload["workflow"]["id"]
    store = WorkflowStore(tmp_path / "state")
    assert result == 0
    assert payload["workflow"]["owns_worktree"] is False
    assert payload["workflow"]["queue_observer_enabled"] is True
    assert "`heavy-check`" in captured["prompt"]
    assert "--notify-workflow" in captured["observer_command"]
    assert workflow_id in captured["observer_command"]
    assert store.owned_terminal_handles(workflow_id) == [
        "term-attach",
        "observer-terminal",
        "terminal:coordinator",
    ]


def test_attach_existing_can_disable_the_configured_queue_observer(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        state_dir=tmp_path / "state",
        orca={"agents": {"single": "cursor"}},
        queue={"observer": True, "resources": ["heavy-check"]},
    )

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def existing_worktree(self, repository: Path) -> tuple[str, str]:
            return "repo::" + str(repository), str(repository)

        def attach_new_agent(self, repository, name, mode, agent, prompt, **_kwargs):
            return StartedWorkflow(
                name,
                "repo::" + str(repository),
                str(repository),
                "term-attach",
                mode,
                owns_worktree=False,
            )

        def create_observer(self, *_args, **_kwargs):
            raise AssertionError("disabled attach must not create an observer")

        def set_lifecycle(self, *_args, **_kwargs):
            return {"updated": True}

        def close_terminals(self, *_args):
            return {"closed": True}

        def remove_worktree(self, *_args):
            raise AssertionError("attach-existing must not remove a worktree")

    _install_orca(monkeypatch, FakeOrcaClient)
    result = main(
        [
            "--config",
            str(config_path),
            "workflow",
            "start",
            str(tmp_path),
            "--attach-existing",
            "--no-queue-observer",
            "--objective",
            "Fix the existing PR.",
            "--issue",
            ISSUE_URL,
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    store = WorkflowStore(tmp_path / "state")
    workflow_id = payload["workflow"]["id"]
    assert result == 0
    assert payload["workflow"]["queue_observer_enabled"] is False
    assert store.owned_terminal_handles(workflow_id) == ["term-attach", "terminal:coordinator"]


def test_start_batch_opens_the_configured_queue_observer(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        state_dir=tmp_path / "state",
        orca={"agents": {"single": "codex"}},
        queue={"observer": True, "resources": ["heavy-check"]},
    )
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    batch = tmp_path / "batch.json"
    batch.write_text(
        json.dumps(
            [
                {
                    "path": str(checkout),
                    "mode": "single",
                    "name": "one",
                    "objective": "Fix one.",
                    "issue": ISSUE_URL,
                }
            ]
        ),
        encoding="utf-8",
    )
    observers: list[str] = []

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def existing_worktree(self, repository: Path) -> tuple[str, str]:
            return "repo::" + str(repository), str(repository)

        def attach_new_agent(self, repository, name, mode, agent, prompt, **_kwargs):
            return StartedWorkflow(
                name,
                "repo::" + str(repository),
                str(repository),
                f"term-{name}",
                mode,
                owns_worktree=False,
            )

        def create_observer(self, _worktree_id: str, command: str) -> str:
            observers.append(command)
            return "observer-terminal"

        def set_lifecycle(self, *_args, **_kwargs):
            return {"updated": True}

        def close_terminals(self, *_args):
            return {"closed": True}

        def remove_worktree(self, *_args):
            raise AssertionError("batch attach must not remove a worktree")

    _install_orca(monkeypatch, FakeOrcaClient)
    result = main(["--config", str(config_path), "workflow", "start", "--batch", str(batch)])
    payload = json.loads(capsys.readouterr().out)
    store = WorkflowStore(tmp_path / "state")
    workflow = next(item for item in store.active())
    assert result == 0
    assert payload["ok"] == 1
    assert payload["failed"] == 0
    assert len(observers) == 1
    assert "--notify-workflow" in observers[0]
    assert workflow.id in observers[0]
    assert workflow.queue_observer_enabled is True
    assert store.owned_terminal_handles(workflow.id) == [
        "term-one",
        "observer-terminal",
        "terminal:coordinator",
    ]


def test_start_batch_accepts_repository_as_path_alias(tmp_path: Path, capsys, monkeypatch) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        state_dir=tmp_path / "state",
        orca={"agents": {"single": "codex"}},
        queue={"observer": False, "resources": []},
        reconcile={"auto_before_workflow_commands": False},
    )
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    batch = tmp_path / "batch.json"
    batch.write_text(
        json.dumps(
            [
                {
                    "repository": str(checkout),
                    "mode": "single",
                    "name": "alias",
                    "objective": "Fix one.",
                    "issue": ISSUE_URL,
                }
            ]
        ),
        encoding="utf-8",
    )

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def existing_worktree(self, repository: Path) -> tuple[str, str]:
            return "repo::" + str(repository), str(repository)

        def attach_new_agent(self, repository, name, mode, agent, prompt, **_kwargs):
            return StartedWorkflow(
                name,
                "repo::" + str(repository),
                str(repository),
                f"term-{name}",
                mode,
                owns_worktree=False,
            )

        def set_lifecycle(self, *_args, **_kwargs):
            return {"updated": True}

        def close_terminals(self, *_args):
            return {"closed": True}

        def remove_worktree(self, *_args):
            raise AssertionError("batch attach must not remove a worktree")

    _install_orca(monkeypatch, FakeOrcaClient)
    result = main(["--config", str(config_path), "workflow", "start", "--batch", str(batch)])
    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["ok"] == 1
    assert payload["failed"] == 0


def test_start_batch_rejects_conflicting_path_and_repository(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        state_dir=tmp_path / "state",
        orca={"agents": {"single": "codex"}},
        reconcile={"auto_before_workflow_commands": False},
    )
    batch = tmp_path / "batch.json"
    batch.write_text(
        json.dumps(
            [
                {
                    "path": "/tmp/one",
                    "repository": "/tmp/two",
                    "objective": "Fix one.",
                    "issue": ISSUE_URL,
                }
            ]
        ),
        encoding="utf-8",
    )

    result = main(["--config", str(config_path), "workflow", "start", "--batch", str(batch)])
    payload = json.loads(capsys.readouterr().out)
    assert result == 2
    assert payload["failed"] == 1
    assert "cannot specify both path and repository" in payload["results"][0]["error"]


def test_attach_existing_replaces_the_managed_workflow_agent_terminal(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    state_dir = tmp_path / "state"
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, state_dir=state_dir, orca={"agents": {"single": "codex"}})
    store = WorkflowStore(state_dir)
    workflow = store.create(tmp_path, "single", "interrupted", "Continue implementation.")
    store.begin_start(workflow.id)
    store.attach_external(
        workflow.id,
        adapter_reference="repo::" + str(tmp_path),
        worktree_path=str(tmp_path),
        terminal_handle="agent-stale",
    )
    store.transition(workflow.id, "running")
    calls: list[str] = []
    captured: dict[str, str] = {}

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def existing_worktree(self, repository: Path) -> tuple[str, str]:
            calls.append("existing")
            return "repo::" + str(repository), str(repository)

        def verify_worktree(self, *_args) -> None:
            calls.append("verify-worktree")

        def create_new_agent_terminal(
            self, worktree_id, worktree_path, agent, prompt, **_kwargs
        ) -> str:
            calls.append("create-terminal")
            captured["prompt"] = prompt
            return "agent-replacement"

        def close_terminals(self, *_args) -> None:
            raise AssertionError("the replacement terminal must remain open")

    _install_orca(monkeypatch, FakeOrcaClient)

    result = main(
        [
            "--config",
            str(config_path),
            "workflow",
            "start",
            str(tmp_path),
            "--attach-existing",
            "--objective",
            "Replace the recorded objective.",
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["new_agent"] is True
    assert payload["workflow"]["id"] == workflow.id
    assert payload["workflow"]["terminal_handle"] == "agent-replacement"
    assert payload["workflow"]["objective"] == "Replace the recorded objective."
    assert calls == ["existing", "verify-worktree", "create-terminal"]
    assert "Replace the recorded objective." in captured["prompt"]
    assert "You are the single role" in captured["prompt"]


@pytest.mark.parametrize(
    ("initially_enabled", "arguments", "expected_enabled", "expected_observers"),
    [
        (False, (), True, ["observer-new"]),
        (True, ("--no-queue-observer",), False, []),
    ],
)
def test_reattach_existing_updates_the_queue_observer_policy(
    tmp_path: Path,
    capsys,
    monkeypatch,
    initially_enabled: bool,
    arguments: tuple[str, ...],
    expected_enabled: bool,
    expected_observers: list[str],
) -> None:
    state_dir = tmp_path / "state"
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        state_dir=state_dir,
        orca={"agents": {"single": "codex"}},
        queue={"observer": True, "resources": ["heavy-check"]},
    )
    store = WorkflowStore(state_dir)
    workflow = store.create(tmp_path, "single", "interrupted", "Continue implementation.")
    store.begin_start(workflow.id)
    store.attach_external(
        workflow.id,
        adapter_reference="repo::" + str(tmp_path),
        worktree_path=str(tmp_path),
        terminal_handle="agent-old",
    )
    store.transition(workflow.id, "running")
    if initially_enabled:
        store.add_owned_terminal(workflow.id, "observer-old", "observer")
    observer_calls: list[str] = []

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def existing_worktree(self, repository: Path) -> tuple[str, str]:
            return "repo::" + str(repository), str(repository)

        def verify_worktree(self, *_args) -> None:
            pass

        def create_new_agent_terminal(self, *_args, **_kwargs) -> str:
            return "agent-new"

        def create_observer(self, _worktree_id: str, command: str) -> str:
            assert "--notify-workflow" in command
            observer_calls.append("observer-new")
            return "observer-new"

        def close_terminals(self, *_args):
            return {"closed": True}

    _install_orca(monkeypatch, FakeOrcaClient)

    result = main(
        [
            "--config",
            str(config_path),
            "workflow",
            "start",
            str(tmp_path),
            "--attach-existing",
            *arguments,
            "--objective",
            "Continue safely.",
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["new_agent"] is True
    assert payload["workflow"]["queue_observer_enabled"] is expected_enabled
    assert observer_calls == expected_observers
    expected_handles = ["agent-new", *expected_observers]
    assert store.owned_terminal_handles(workflow.id) == expected_handles


def test_attach_existing_does_not_accept_the_removed_fresh_agent_flag(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    state_dir = tmp_path / "state"
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, state_dir=state_dir, orca={"agents": {"single": "codex"}})
    with pytest.raises(SystemExit, match="2"):
        main(
            [
                "--config",
                str(config_path),
                "workflow",
                "start",
                str(tmp_path),
                "--attach-existing",
                "--fresh-agent",
                "--objective",
                "Ignored because the persisted workflow retains its objective.",
            ]
        )
    assert "unrecognized arguments" in capsys.readouterr().err


def test_attach_existing_reconciles_a_cancelled_unreconciled_worktree(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    state_dir = tmp_path / "state"
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        state_dir=state_dir,
        default_mode="orchestrated",
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
    store = WorkflowStore(state_dir)
    previous = store.create(tmp_path, "single", "old-name", "Previous objective.")
    store.begin_start(previous.id)
    store.attach_external(
        previous.id,
        adapter_reference="repo::" + str(tmp_path),
        worktree_path=str(tmp_path),
        terminal_handle="agent-old",
        owns_worktree=False,
    )
    store.transition(previous.id, "running")
    WorkflowService(store).finish(previous.id, "cancelled", lambda _reference: None)
    observer_calls: list[str] = []

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def existing_worktree(self, repository: Path) -> tuple[str, str]:
            return "repo::" + str(repository), str(repository)

        def attach_new_agent(self, repository, name, mode, agent, prompt, **_kwargs):
            assert mode == "single"
            assert "Fresh attached brief." in prompt
            return StartedWorkflow(
                name,
                "repo::" + str(repository),
                str(repository),
                "term-attach",
                mode,
                owns_worktree=False,
            )

        def set_lifecycle(self, *_args, **_kwargs):
            return {"updated": True}

        def create_observer(self, *_args) -> str:
            observer_calls.append("observer")
            return "observer-terminal"

        def close_terminals(self, *_args):
            return {"closed": True}

        def remove_worktree(self, *_args):
            raise AssertionError("attach-existing must not remove a worktree")

    _install_orca(monkeypatch, FakeOrcaClient)
    brief = tmp_path / "brief.md"
    brief.write_text("Fresh attached brief.\n", encoding="utf-8")

    result = main(
        [
            "--config",
            str(config_path),
            "workflow",
            "start",
            str(tmp_path),
            "--attach-existing",
            "--name",
            "old-name",
            "-o",
            f"@{brief}",
            "--issue",
            ISSUE_URL,
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["workflow"]["id"] != previous.id
    assert payload["workflow"]["mode"] == "single"
    assert payload["workflow"]["owns_worktree"] is False
    assert payload["workflow"]["name"] == "old-name"
    assert observer_calls == ["observer"]
    assert main(["--config", str(config_path), "workflow", "list", "--json"]) == 0
    listing = json.loads(capsys.readouterr().out)
    statuses = {item["id"]: item["status"] for item in listing["workflows"]}
    assert statuses[previous.id] == "cancelled"
    assert statuses[payload["workflow"]["id"]] == "running"


def test_objective_file_flags_and_missing_path(tmp_path: Path, capsys, monkeypatch) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, state_dir=tmp_path / "state", orca={"agents": {"single": "codex"}})
    brief = tmp_path / "objective.md"
    brief.write_text("From a file.\n", encoding="utf-8")
    captured: dict[str, str] = {}

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def prepare_repository(self, _repository: Path) -> None:
            pass

        def register_repository(self, _repository: Path) -> None:
            pass

        def start(self, _repository, name, mode, _agent, prompt, **_kwargs):
            captured["prompt"] = prompt
            return StartedWorkflow(name, "repo::/tmp/file", "/tmp/file", "term", mode)

        def set_lifecycle(self, *_args, **_kwargs):
            return {"updated": True}

        def close_terminals(self, *_args):
            return {"closed": True}

        def remove_worktree(self, *_args):
            return {"removed": True}

    _install_orca(monkeypatch, FakeOrcaClient)
    assert (
        main(
            [
                "--config",
                str(config_path),
                "workflow",
                "start",
                str(tmp_path),
                "--objective-file",
                str(brief),
                "--issue",
                ISSUE_URL,
            ]
        )
        == 0
    )
    assert "From a file." in json.loads(capsys.readouterr().out)["workflow"]["objective"]
    assert "From a file." in captured["prompt"]
    assert str(brief) not in captured["prompt"]
    assert (
        main(
            ["--config", str(config_path), "workflow", "start", str(tmp_path), "-o", "@missing.md"]
        )
        == 2
    )
    assert "objective file does not exist" in capsys.readouterr().err


def test_start_batch_continues_after_a_failed_item(tmp_path: Path, capsys, monkeypatch) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, state_dir=tmp_path / "state", orca={"agents": {"single": "codex"}})
    first = tmp_path / "first"
    third = tmp_path / "third"
    first.mkdir()
    third.mkdir()
    brief = tmp_path / "brief.md"
    brief.write_text("Batch brief.\n", encoding="utf-8")
    batch = tmp_path / "batch.json"
    batch.write_text(
        json.dumps(
            [
                {
                    "path": str(first),
                    "mode": "single",
                    "name": "one",
                    "objective_file": str(brief),
                    "issue": ISSUE_URL,
                },
                {"path": str(tmp_path / "missing"), "name": "two", "objective": "Missing."},
                {
                    "path": str(third),
                    "name": "three",
                    "objective": "Third.",
                    "issue": ISSUE_URL,
                },
            ]
        ),
        encoding="utf-8",
    )
    attached: list[str] = []

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def existing_worktree(self, repository: Path) -> tuple[str, str]:
            return "repo::" + str(repository), str(repository)

        def attach_new_agent(self, repository, name, mode, agent, prompt, **_kwargs):
            attached.append(name)
            return StartedWorkflow(
                name,
                "repo::" + str(repository),
                str(repository),
                f"term-{name}",
                mode,
                owns_worktree=False,
            )

        def set_lifecycle(self, *_args, **_kwargs):
            return {"updated": True}

        def close_terminals(self, *_args):
            return {"closed": True}

        def remove_worktree(self, *_args):
            raise AssertionError("batch attach must not remove a worktree")

    _install_orca(monkeypatch, FakeOrcaClient)
    result = main(["--config", str(config_path), "workflow", "start", "--batch", str(batch)])
    payload = json.loads(capsys.readouterr().out)
    assert result == 2
    assert payload["ok"] == 2
    assert payload["failed"] == 1
    assert attached == ["one", "three"]
    assert payload["results"][1]["ok"] is False


def test_workflow_proceed_starts_the_worker(tmp_path: Path, capsys, monkeypatch) -> None:
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
            role = prompt.split("You are the ", 1)[1].split(" role", 1)[0]
            started_roles.append(role)
            return StartedWorkflow(
                name, f"id:{role}", str(tmp_path / role), f"terminal:{role}", workflow_mode
            )

        def current_branch(self, _worktree_path: str) -> str:
            return "main"

        def uncommitted_changes(self, _worktree_path: str) -> tuple[str, ...]:
            return ()

        def set_lifecycle(self, *_args, **_kwargs) -> dict[str, bool]:
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
                "workflow",
                "start",
                str(tmp_path),
                "--mode",
                "orchestrated",
                "--objective",
                "Inspect.",
                "--issue",
                ISSUE_URL,
            ]
        )
        == 0
    )
    manager_id = json.loads(capsys.readouterr().out)["workflow"]["id"]
    WorkflowArtifactStore(tmp_path / "state").put(
        manager_id, "plan", "# Plan\n\nInspect the implementation."
    )
    assert (
        main(
            [
                "--config",
                str(config_path),
                "workflow",
                "proceed",
                manager_id,
                "--summary",
                "Manager plan is complete.",
            ]
        )
        == 0
    )
    started = json.loads(capsys.readouterr().out)["workflow"]
    assert started["role"] == "worker"
    assert started["status"] == "running"
    assert started_roles == ["manager", "worker"]


def test_workflow_cancel_clears_a_leased_queue_request(tmp_path: Path, capsys, monkeypatch) -> None:
    state_dir = tmp_path / "state"
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, state_dir=state_dir, orca={"agents": {"single": "codex"}})
    store = WorkflowStore(state_dir)
    queue = ResourceQueue(state_dir)
    workflow = store.create(tmp_path, "single", "leased", "Implement.")
    store.transition(workflow.id, "starting")
    store.attach_external(
        workflow.id,
        adapter_reference="repo::/tmp/leased",
        worktree_path="/tmp/leased",
        terminal_handle="term",
    )
    store.transition(workflow.id, "running")
    lease = queue.acquire("exclusive", workflow.id)

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def set_lifecycle(self, *_args, **_kwargs) -> dict[str, bool]:
            return {"updated": True}

        def close_terminals(self, *_args) -> dict[str, bool]:
            return {"closed": True}

    _install_orca(monkeypatch, FakeOrcaClient)
    assert main(["--config", str(config_path), "workflow", "cancel", workflow.id]) == 0
    capsys.readouterr()
    assert main(["--config", str(config_path), "queue", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    leased = [row for row in status if row.get("status") == "leased"]
    assert leased == []
    assert queue.inspect(lease.request_id)["status"] == "cancelled"


def test_cleanup_apply_reports_per_workflow_errors(tmp_path: Path, capsys, monkeypatch) -> None:
    config_path = tmp_path / "config.jsonc"
    state_dir = tmp_path / "state"
    write_config(config_path, state_dir=state_dir)
    store = WorkflowStore(state_dir)
    first = store.create(tmp_path, "single", "one", "Implement.")
    second = store.create(tmp_path, "single", "two", "Implement.")
    for workflow in (first, second):
        store.transition(workflow.id, "starting")
        store.attach_external(
            workflow.id,
            adapter_reference=f"repo::{workflow.name}",
            worktree_path=f"/tmp/{workflow.name}",
            terminal_handle="term",
        )
        store.transition(workflow.id, "running")
        with store._connect() as connection:
            connection.execute(
                "UPDATE workflows SET updated_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", workflow.id),
            )

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def close_terminals(self, *_args) -> None:
            return None

        def remove_worktree(self, worktree_id: str) -> dict[str, bool]:
            if worktree_id.endswith("one"):
                raise RuntimeError("Orca unavailable")
            return {"removed": True}

    _install_orca(monkeypatch, FakeOrcaClient)
    result = main(
        [
            "--config",
            str(config_path),
            "workflow",
            "cleanup",
            "--apply",
            "--older-than-seconds",
            "60",
            "--force-age",
        ]
    )
    output = json.loads(capsys.readouterr().out)
    assert result == 2
    assert second.id in output["workflow_ids"]
    assert first.id not in output["workflow_ids"]
    assert output["errors"]
    assert store.get(second.id).status == "cancelled"
    assert store.get(first.id).status == "running"


def test_start_requires_issue_url_for_new_worktrees(tmp_path: Path, capsys, monkeypatch) -> None:
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, state_dir=tmp_path / "state", orca={"agents": {"single": "codex"}})

    class FakeOrcaClient:
        def __init__(self, _executable: str) -> None:
            pass

        def prepare_repository(self, *_args) -> None:
            raise AssertionError("missing --issue must fail before Orca start")

    _install_orca(monkeypatch, FakeOrcaClient)
    assert (
        main(
            [
                "--config",
                str(config_path),
                "workflow",
                "start",
                str(tmp_path),
                "--objective",
                "Inspect.",
            ]
        )
        == 2
    )
    assert "GitHub issue URL is required" in capsys.readouterr().err


def test_board_screen_with_refs_joins_comment_urls(tmp_path: Path, capsys, monkeypatch) -> None:
    repo = tmp_path / "keep"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.email", "a@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "A"], cwd=repo, check=True)
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/example/repo.git"],
        cwd=repo,
        check=True,
    )
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, github=ENABLED_GITHUB)

    class FakeProject:
        def __init__(self, _config) -> None:
            self.seen_status_names = ()
            self.seen_priority_names = ()

        def screen(self, **_kwargs):
            return [
                Candidate(
                    "example/repo",
                    1,
                    "Task",
                    "https://github.com/example/repo/issues/1",
                    "Todo",
                    None,
                    ("alice",),
                    "example",
                    1,
                )
            ]

    class FakeOrca:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def verify(self):
            return {"app_version": "test", "skill": "orchestration"}

        def list_worktrees(self):
            from flybridge_orca.client import ListedWorktree

            return (
                (
                    ListedWorktree(
                        "repo::" + str(repo),
                        str(repo),
                        "keep",
                        "todo",
                        "https://github.com/example/repo/issues/1",
                        "main",
                        None,
                        None,
                        "github:example/repo",
                    ),
                ),
                False,
            )

    class FakeDevelopment:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def fetch_many(self, urls):
            assert "https://github.com/example/repo/issues/1" in list(urls)
            from flybridge_github.issues import IssueDevelopment

            return {
                "example/repo#1": IssueDevelopment(
                    "https://github.com/example/repo/issues/1",
                    (),
                    (),
                    (),
                )
            }

    class FakePullRequests:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def list_open(self, repositories):
            return {name: () for name in repositories}, ()

        def list_by_head(self, repository, head_ref_name):
            return (), None

        def get(self, repository, number):
            return None, None

    monkeypatch.setattr("flybridge_cli.commands.auxiliary.GitHubProject", FakeProject)
    monkeypatch.setattr("flybridge_cli.runtime.OrcaClient", FakeOrca)
    monkeypatch.setattr("flybridge_cli.commands.auxiliary.GitHubIssueDevelopment", FakeDevelopment)
    monkeypatch.setattr("flybridge_cli.commands.auxiliary.GitHubPullRequests", FakePullRequests)

    assert main(["--config", str(config_path), "board", "screen", "--with-refs"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["issues"][0]["worktrees"][0]["path"] == str(repo)
    assert payload["issues"][0]["worktrees"][0]["match"] == ["issue_url"]
    assert payload["issues"][0]["development"]["pull_requests"] == []
    assert payload["unmatched_worktrees"] == []


def test_board_screen_with_refs_applies_exclude_name(tmp_path: Path, capsys, monkeypatch) -> None:
    keep = tmp_path / "keep"
    skipped = tmp_path / "vendor-checkout"
    for repo in (keep, skipped):
        repo.mkdir()
        subprocess.run(
            ["git", "init", "--initial-branch=main"], cwd=repo, check=True, capture_output=True
        )
        subprocess.run(["git", "config", "user.email", "a@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "A"], cwd=repo, check=True)
        (repo / "README.md").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/example/repo.git"],
            cwd=repo,
            check=True,
        )
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, github=ENABLED_GITHUB)

    class FakeProject:
        def __init__(self, _config) -> None:
            self.seen_status_names = ()
            self.seen_priority_names = ()

        def screen(self, **_kwargs):
            return [
                Candidate(
                    "example/repo",
                    1,
                    "Task",
                    "https://github.com/example/repo/issues/1",
                    "Todo",
                    None,
                    ("alice",),
                    "example",
                    1,
                )
            ]

    class FakeOrca:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def verify(self):
            return {"app_version": "test", "skill": "orchestration"}

        def list_worktrees(self):
            from flybridge_orca.client import ListedWorktree

            return (
                (
                    ListedWorktree(
                        "repo::" + str(keep),
                        str(keep),
                        "keep",
                        "todo",
                        "https://github.com/example/repo/issues/1",
                        "main",
                        None,
                        None,
                        "github:example/repo",
                    ),
                    ListedWorktree(
                        "repo::" + str(skipped),
                        str(skipped),
                        "vendor-checkout",
                        "todo",
                        "",
                        "main",
                        None,
                        None,
                        "github:example/repo",
                    ),
                ),
                False,
            )

    class FakeDevelopment:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def fetch_many(self, urls):
            from flybridge_github.issues import IssueDevelopment

            return {
                "example/repo#1": IssueDevelopment(
                    "https://github.com/example/repo/issues/1",
                    (),
                    (),
                    (),
                )
            }

    class FakePullRequests:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def list_open(self, repositories):
            return {name: () for name in repositories}, ()

        def list_by_head(self, repository, head_ref_name):
            return (), None

        def get(self, repository, number):
            return None, None

    monkeypatch.setattr("flybridge_cli.commands.auxiliary.GitHubProject", FakeProject)
    monkeypatch.setattr("flybridge_cli.runtime.OrcaClient", FakeOrca)
    monkeypatch.setattr("flybridge_cli.commands.auxiliary.GitHubIssueDevelopment", FakeDevelopment)
    monkeypatch.setattr("flybridge_cli.commands.auxiliary.GitHubPullRequests", FakePullRequests)

    assert (
        main(
            [
                "--config",
                str(config_path),
                "board",
                "screen",
                "--with-refs",
                "--exclude-name",
                "vendor-checkout",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["issues"][0]["worktrees"][0]["path"] == str(keep)
    assert payload["unmatched_worktrees"] == []


def _autonomous_orca_client(
    tmp_path: Path,
    prompts: list[str],
    closed: set[str],
    repository_head: dict[str, str],
    removed: list[str],
):
    """Fake Orca whose readiness gates follow the real HEAD versus start-SHA rules."""

    class FakeOrcaClient:
        terminal_number = 0
        coordinator_number = 0

        def __init__(self, _executable: str) -> None:
            pass

        def prepare_repository(self, _repository: Path) -> None:
            pass

        def register_repository(self, _repository: Path) -> None:
            pass

        def start(self, _repository, name, mode, _agent, prompt, **_kwargs):
            self.__class__.terminal_number += 1
            terminal = f"agent-{self.terminal_number}"
            prompts.append(prompt)
            return StartedWorkflow(name, f"repo::{name}", str(tmp_path / name), terminal, mode)

        def create_coordinator(self, _worktree_id: str, _command: str) -> str:
            self.__class__.coordinator_number += 1
            if self.coordinator_number == 1:
                return "coordinator"
            return f"coordinator-{self.coordinator_number}"

        def implementation_identity(self, _worktree_id: str, _worktree_path: str):
            return "example/repo", "repo", repository_head["sha"]

        def verify_implementation_identity(self, *_args) -> None:
            pass

        def verify_worktree(self, *_args) -> None:
            pass

        def verify_pristine_start(self, _path: str, start_sha: str) -> None:
            if repository_head["sha"] != start_sha:
                raise RuntimeError("implementation HEAD does not equal the persisted start SHA")

        def verify_worker_ready(self, _path: str, start_sha: str) -> None:
            if repository_head["sha"] == start_sha:
                raise RuntimeError(
                    "worker must create at least one implementation commit beyond start SHA"
                )

        def current_branch(self, _worktree_path: str) -> str:
            return "feature"

        def uncommitted_changes(self, _worktree_path: str) -> tuple[str, ...]:
            return ()

        def set_lifecycle(self, *_args, **_kwargs) -> dict[str, bool]:
            return {"updated": True}

        def close_terminals(self, _worktree_id: str, handle: str) -> dict[str, bool]:
            closed.add(handle)
            return {"closed": True}

        def remove_worktree(self, worktree_id: str) -> dict[str, bool]:
            removed.append(worktree_id)
            return {"removed": True}

        def terminal_is_valid(self, _worktree_id: str, handle: str | None) -> bool:
            return bool(handle and handle not in closed)

        def create_new_agent_terminal(
            self, _worktree_id: str, _path: str, _agent: str, prompt: str, **_kwargs
        ) -> str:
            self.__class__.terminal_number += 1
            prompts.append(prompt)
            return f"agent-{self.terminal_number}"

        def integrate_worker_commit(self, manager_path, worker_path, worker_sha, *, dry_run=False):
            return {
                "method": "already",
                "before": repository_head["sha"],
                "after": repository_head["sha"],
            }

        def push_fast_forward(self, _worktree_path: str) -> dict[str, str]:
            self.__class__.pushed = getattr(self.__class__, "pushed", [])
            self.__class__.pushed.append(_worktree_path)
            return {"remote": "origin", "ref": "HEAD"}

    return FakeOrcaClient


def _autonomous_root(tmp_path: Path, capsys, monkeypatch, **overrides):
    """Start an orchestrated root that a coordinator can drive without an operator."""
    prompts: list[str] = []
    closed: set[str] = set()
    removed: list[str] = []
    repository_head = {"sha": "sha-0"}
    config_path = write_config(
        tmp_path / "config.jsonc",
        state_dir=tmp_path / "state",
        default_mode="orchestrated",
        orca={"agents": {"manager": "codex", "worker": "codex", "reviewer": "codex"}},
        **overrides,
    )
    client_cls = _autonomous_orca_client(tmp_path, prompts, closed, repository_head, removed)
    _install_orca(monkeypatch, client_cls)
    assert (
        main(
            [
                "--config",
                str(config_path),
                "workflow",
                "start",
                str(tmp_path),
                "--objective",
                "Implement.",
                "--issue",
                ISSUE_URL,
            ]
        )
        == 0
    )
    manager_id = json.loads(capsys.readouterr().out)["workflow"]["id"]
    service = WorkflowService(WorkflowStore(tmp_path / "state"))

    def ready(workflow_id, kind, content, summary, outcome=None, expected=0) -> None:
        service.put_artifact(workflow_id, kind, content)
        arguments = [
            "--config",
            str(config_path),
            "workflow",
            "role-ready",
            workflow_id,
            "--summary",
            summary,
        ]
        if outcome:
            arguments.extend(["--outcome", outcome])
        assert main(arguments) == expected
        capsys.readouterr()

    def supervise(expected: int = 0) -> dict[str, object]:
        arguments = ["--config", str(config_path), "workflow", "supervise", manager_id, "--once"]
        assert main(arguments) == expected
        return json.loads(capsys.readouterr().out)

    return SimpleNamespace(
        config_path=config_path,
        service=service,
        manager_id=manager_id,
        client_cls=client_cls,
        prompts=prompts,
        closed=closed,
        removed=removed,
        repository_head=repository_head,
        ready=ready,
        supervise=supervise,
    )


def _progress(config_path: Path, workflow_id: str, capsys) -> dict[str, object]:
    assert main(["--config", str(config_path), "workflow", "status", workflow_id]) == 0
    payload = json.loads(capsys.readouterr().out)["progress"]
    assert all("artifact_content" not in item for item in payload["readiness"])
    return payload


def test_workflow_status_progress_tracks_orchestrated_stages(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    root = _autonomous_root(tmp_path, capsys, monkeypatch, orchestration={"max_review_cycles": 3})
    manager = root.service.store.get(root.manager_id)
    worker, reviewer = root.service.store.children(root.manager_id)

    planning = _progress(root.config_path, manager.id, capsys)
    assert planning["stage"] == "planning"
    assert planning["current_review_cycle"] == 1
    assert planning["max_review_cycles"] == 3
    assert [role["role"] for role in planning["roles"]] == ["manager", "worker", "reviewer"]
    assert planning["readiness"] == []

    root.ready(manager.id, "plan", "Plan.", "Implement the plan.")
    root.supervise()
    implementing = _progress(root.config_path, worker.id, capsys)
    assert implementing["stage"] == "implementing"
    assert any(role["id"] == manager.id for role in implementing["roles"])
    assert implementing["readiness"][0]["summary"] == "Implement the plan."
    assert implementing["readiness"][0]["role"] == "manager"

    root.repository_head["sha"] = "sha-1"
    root.ready(worker.id, "verification", "Checks passed.", "Review the implementation.")
    root.supervise()
    reviewing = _progress(root.config_path, reviewer.id, capsys)
    assert reviewing["stage"] == "reviewing"
    assert [item["role"] for item in reviewing["readiness"]] == ["manager", "worker"]

    root.ready(reviewer.id, "review", "Changes needed.", "Fix the issue.", "changes-requested")
    root.supervise()
    rework = _progress(root.config_path, manager.id, capsys)
    assert rework["stage"] == "addressing_review"
    assert rework["current_review_cycle"] == 2
    assert rework["readiness"][-1]["outcome"] == "changes-requested"

    root.repository_head["sha"] = "sha-2"
    root.ready(worker.id, "verification", "Fix verified.", "Review the fix.")
    root.supervise()
    root.ready(reviewer.id, "review", "Approved.", "Approved.", "approved")
    root.supervise()
    completed = _progress(root.config_path, manager.id, capsys)
    assert completed["stage"] == "completed"
    assert [item["attempt"] for item in completed["readiness"]] == [1, 1, 1, 2, 2]


def test_autonomous_coordinator_requires_a_new_commit_in_the_second_review_cycle(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    root = _autonomous_root(tmp_path, capsys, monkeypatch, orchestration={"max_review_cycles": 3})
    service = root.service
    manager = service.store.get(root.manager_id)
    worker, reviewer = service.store.children(root.manager_id)

    root.ready(manager.id, "plan", "Plan.", "Implement the plan.")
    assert root.supervise()["action"] == "launched-worker"
    assert "agent-1" not in root.closed
    terminal_count = root.client_cls.terminal_number
    assert root.supervise()["action"] == "waiting"
    assert root.client_cls.terminal_number == terminal_count

    root.repository_head["sha"] = "sha-1"
    root.ready(worker.id, "verification", "Checks passed.", "Review the implementation.")
    assert root.supervise()["action"] == "launched-reviewer"
    assert "agent-2" not in root.closed
    root.ready(reviewer.id, "review", "Changes needed.", "Fix the issue.", "changes-requested")
    assert root.supervise()["action"] == "returned-to-worker"
    assert "Reviewer feedback artifact:" in root.prompts[-1]
    assert service.store.get(worker.id).start_sha == "sha-1"

    root.ready(worker.id, "verification", "Nothing changed.", "Review the fix.", expected=2)
    with pytest.raises(ValueError, match="readiness was not found"):
        service.store.role_readiness(root.manager_id, "worker", 2)
    assert root.supervise()["action"] == "waiting"

    root.repository_head["sha"] = "sha-2"
    root.ready(worker.id, "verification", "Fix verified.", "Review the fix.")
    assert root.supervise()["action"] == "launched-reviewer"
    root.ready(reviewer.id, "review", "Approved.", "Approved.", "approved")
    assert root.supervise()["action"] == "completed"
    assert "agent-5" in root.closed
    assert root.supervise()["action"] == "terminal"

    run = service.store.orchestration_run(root.manager_id)
    assert run.status == "completed"
    assert run.current_review_cycle == 2


def test_supervisor_flushes_its_outcome_before_closing_its_own_terminal(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    root = _autonomous_root(tmp_path, capsys, monkeypatch)
    service = root.service
    manager = service.store.get(root.manager_id)
    worker, reviewer = service.store.children(root.manager_id)

    root.ready(manager.id, "plan", "Plan.", "Implement the plan.")
    root.supervise()
    root.repository_head["sha"] = "sha-1"
    root.ready(worker.id, "verification", "Checks passed.", "Review it.")
    root.supervise()
    root.ready(reviewer.id, "review", "Approved.", "Approved.", "approved")

    assert root.supervise()["action"] == "completed"

    run = service.store.orchestration_run(root.manager_id)
    assert run.status == "completed"
    assert run.coordinator_handle == "coordinator"
    assert run.coordinator_release_reason == "orchestration_completed"
    assert run.coordinator_released_at is not None
    assert service.store.owned_terminal_handles(root.manager_id, kind="coordinator") == []
    assert "coordinator" in root.closed
    worker, reviewer = service.store.children(root.manager_id)
    assert worker.external_reconciled_at is not None
    assert reviewer.external_reconciled_at is not None
    assert service.store.get(root.manager_id).external_reconciled_at is None
    assert any("worker" in item for item in root.removed)
    assert any("reviewer" in item for item in root.removed)


def test_supervisor_fails_the_run_instead_of_polling_a_dead_role(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    root = _autonomous_root(tmp_path, capsys, monkeypatch)
    service = root.service
    manager = service.store.get(root.manager_id)
    worker, _reviewer = service.store.children(root.manager_id)

    root.ready(manager.id, "plan", "Plan.", "Implement the plan.")
    assert root.supervise()["action"] == "launched-worker"
    assert (
        main(
            [
                "--config",
                str(root.config_path),
                "workflow",
                "fail",
                worker.id,
                "--error",
                "worker crashed",
            ]
        )
        == 0
    )
    capsys.readouterr()

    result = root.supervise()

    assert result["action"] == "failed"
    run = service.store.orchestration_run(root.manager_id)
    assert run.status == "failed"
    assert "worker is failed" in run.error
    assert run.coordinator_release_reason == "orchestration_failed"
    assert "coordinator" in root.closed


def test_transient_supervisor_error_retries_from_persisted_run_state(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    root = _autonomous_root(
        tmp_path,
        capsys,
        monkeypatch,
        orchestration={
            "max_coordinator_errors": 3,
            "retry_initial_seconds": 0.01,
            "retry_max_seconds": 0.02,
        },
    )
    manager = root.service.store.get(root.manager_id)
    root.ready(manager.id, "plan", "Plan.", "Implement the plan.")

    def timeout(_self):
        raise RuntimeError("temporary Orca timeout")

    root.client_cls.verify = timeout
    first = root.supervise()

    assert first["action"] == "retry-wait"
    restarted_store = WorkflowStore(tmp_path / "state")
    persisted = restarted_store.orchestration_run(root.manager_id)
    assert persisted.status == "running"
    assert persisted.coordinator_error_count == 1
    assert "temporary Orca timeout" in persisted.coordinator_last_error

    root.client_cls.verify = lambda _self: {"app_version": "test", "skill": "orchestration"}
    time.sleep(0.02)
    assert root.supervise()["action"] == "launched-worker"
    recovered = WorkflowStore(tmp_path / "state").orchestration_run(root.manager_id)
    assert recovered.status == "running"
    assert recovered.coordinator_error_count == 0
    assert recovered.coordinator_retry_at is None


def test_missing_reviewer_selector_is_successful_remove_replay(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    root = _autonomous_root(tmp_path, capsys, monkeypatch)
    manager = root.service.store.get(root.manager_id)
    worker, reviewer = root.service.store.children(root.manager_id)
    root.ready(manager.id, "plan", "Plan.", "Implement the plan.")
    root.supervise()
    root.repository_head["sha"] = "sha-1"
    root.ready(worker.id, "verification", "Verified.", "Review it.")
    root.supervise()
    root.ready(reviewer.id, "review", "Changes.", "Fix it.", "changes-requested")

    class MissingSelector(RuntimeError):
        code = "selector_not_found"

    root.client_cls.remove_worktree = lambda *_args: (_ for _ in ()).throw(
        MissingSelector("already removed")
    )

    assert root.supervise()["action"] == "returned-to-worker"
    assert root.service.store.get(reviewer.id).status == "requested"


def test_rework_agent_launch_replays_after_cycle_transaction_crash(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    root = _autonomous_root(
        tmp_path,
        capsys,
        monkeypatch,
        orchestration={
            "max_coordinator_errors": 3,
            "retry_initial_seconds": 0.01,
            "retry_max_seconds": 0.02,
        },
    )
    manager = root.service.store.get(root.manager_id)
    worker, reviewer = root.service.store.children(root.manager_id)
    root.ready(manager.id, "plan", "Plan.", "Implement the plan.")
    root.supervise()
    root.repository_head["sha"] = "sha-1"
    root.ready(worker.id, "verification", "Verified.", "Review it.")
    root.supervise()
    root.ready(reviewer.id, "review", "Changes.", "Fix it.", "changes-requested")
    original_create = root.client_cls.create_new_agent_terminal

    def crash_after_cycle(*_args, **_kwargs):
        raise RuntimeError("temporary terminal creation timeout")

    root.client_cls.create_new_agent_terminal = crash_after_cycle
    first = root.supervise()

    assert first["action"] == "retry-wait"
    run = WorkflowStore(tmp_path / "state").orchestration_run(root.manager_id)
    assert run.current_review_cycle == 2
    assert root.service.store.role_readiness(root.manager_id, "reviewer", 1).consumed_at is not None

    root.client_cls.create_new_agent_terminal = original_create
    time.sleep(0.02)
    terminal_count = root.client_cls.terminal_number
    assert root.supervise()["action"] == "waiting"
    assert root.client_cls.terminal_number == terminal_count + 1
    assert root.service.store.get(worker.id).terminal_handle not in root.closed


@pytest.mark.parametrize("blocked_role", ["manager", "worker", "reviewer"])
def test_declared_role_blocker_terminates_each_orchestrated_stage(
    tmp_path: Path, capsys, monkeypatch, blocked_role: str
) -> None:
    root = _autonomous_root(tmp_path, capsys, monkeypatch)
    manager = root.service.store.get(root.manager_id)
    worker, reviewer = root.service.store.children(root.manager_id)
    blocker = "Required live environment is unavailable."

    if blocked_role == "manager":
        root.ready(manager.id, "plan", "Blocked plan.", blocker, "blocked")
        blocked_id = manager.id
    else:
        root.ready(manager.id, "plan", "Plan.", "Implement it.")
        root.supervise()
        root.repository_head["sha"] = "sha-1"
        if blocked_role == "worker":
            root.ready(worker.id, "verification", "Partial verification.", blocker, "blocked")
            blocked_id = worker.id
        else:
            root.ready(worker.id, "verification", "Verified.", "Review it.")
            root.supervise()
            root.ready(reviewer.id, "review", "Blocked review.", blocker, "blocked")
            blocked_id = reviewer.id

    result = root.supervise()

    assert result["action"] == "blocked"
    assert result["blocker"]["blocked_reason"] == blocker
    assert "retire" in result or "retire_error" in result
    assert root.service.store.get(blocked_id).status == "cancelled"
    assert root.service.store.orchestration_run(root.manager_id).status == "blocked"
    if blocked_role != "manager":
        assert root.service.store.get(worker.id).external_reconciled_at is not None
    if blocked_role == "manager":
        assert "progress_push" not in result
    else:
        assert result["progress_push"]["pushed"]["remote"] == "origin"
        assert root.client_cls.pushed
    if blocked_role == "manager":
        assert root.service.store.get(worker.id).status == "cancelled"
        assert root.service.store.get(reviewer.id).status == "cancelled"
    elif blocked_role == "worker":
        assert root.service.store.get(reviewer.id).status == "cancelled"
    status_code = main(
        [
            "--config",
            str(root.config_path),
            "workflow",
            "status",
            root.manager_id,
        ]
    )
    assert status_code == 0
    status = json.loads(capsys.readouterr().out)
    assert status["blocker"]["workflow_id"] == blocked_id
    assert status["blocker"]["blocked_reason"] == blocker
    assert status["progress"]["stage"] == "blocked"
    assert any(item["blocked_reason"] == blocker for item in status["progress"]["readiness"])
    assert "coordinator" in root.closed


def test_declared_blocker_replays_after_role_close_before_atomic_run_outcome(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    root = _autonomous_root(
        tmp_path,
        capsys,
        monkeypatch,
        orchestration={
            "max_coordinator_errors": 3,
            "retry_initial_seconds": 0.01,
            "retry_max_seconds": 0.02,
        },
    )
    manager = root.service.store.get(root.manager_id)
    blocker = "Required live environment is unavailable."
    root.ready(manager.id, "plan", "Blocked plan.", blocker, "blocked")
    original_finalize = WorkflowStore.finalize_blocked_readiness
    attempts = 0

    def crash_once(store, readiness_id):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("simulated crash before aggregate outcome")
        return original_finalize(store, readiness_id)

    monkeypatch.setattr(WorkflowStore, "finalize_blocked_readiness", crash_once)
    first = root.supervise()

    assert first["action"] == "retry-wait"
    assert root.service.store.get(manager.id).status == "cancelled"
    assert root.service.store.pending_blocker(root.manager_id) is not None
    assert root.service.store.orchestration_run(root.manager_id).status == "running"

    time.sleep(0.02)
    second = root.supervise()

    assert second["action"] == "blocked"
    assert attempts == 2
    assert root.service.store.pending_blocker(root.manager_id) is None
    assert root.service.store.orchestration_run(root.manager_id).status == "blocked"


def test_coordinator_retry_recovers_hot_upgrade_blocker_with_current_code(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    root = _autonomous_root(tmp_path, capsys, monkeypatch)
    manager = root.service.store.get(root.manager_id)
    worker, _reviewer = root.service.store.children(root.manager_id)
    blocker = "Live environment unavailable."
    root.ready(manager.id, "plan", "Plan.", "Implement it.")
    assert root.supervise()["action"] == "launched-worker"
    root.repository_head["sha"] = "sha-1"
    root.ready(worker.id, "verification", "Blocked scope.", blocker, "blocked")
    root.service.store.set_orchestration_outcome(
        root.manager_id,
        "blocked",
        error="TypeError: RoleReadiness.__init__() got an unexpected keyword argument "
        "'blocked_reason'",
    )
    root.service.release_coordinator(root.manager_id, "orchestration_blocked")

    assert (
        main(
            [
                "--config",
                str(root.config_path),
                "workflow",
                "coordinator-retry",
                root.manager_id,
            ]
        )
        == 0
    )
    recovered = json.loads(capsys.readouterr().out)

    assert recovered["run"]["status"] == "running"
    assert recovered["run"]["error"] is None
    assert recovered["run"]["coordinator_released_at"] is None
    assert recovered["coordinator_handle"] == "coordinator-2"
    assert "coordinator" in root.closed
    assert root.service.store.pending_blocker(root.manager_id) is not None
    assert root.service.store.owned_terminal_handles(root.manager_id, kind="coordinator") == [
        "coordinator-2"
    ]

    replayed = root.supervise()

    assert replayed["action"] == "blocked"
    assert replayed["blocker"]["blocked_reason"] == blocker
    assert root.service.store.get(manager.id).status == "completed"
    assert root.service.store.get(worker.id).status == "cancelled"
    assert "coordinator-2" in root.closed


def test_workflow_link_accepts_step_id_and_rejects_unknown_ids(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    state_dir = tmp_path / "state"
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        state_dir=state_dir,
        reconcile={"auto_before_workflow_commands": False},
    )
    store = WorkflowStore(state_dir)
    workflow = store.create(tmp_path, "single", "task", "Do it")

    class FakeOrca:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def verify(self):
            return {"app_version": "test", "skill": "orchestration"}

        def list_worktrees(self):
            return (), False

    monkeypatch.setattr("flybridge_cli.runtime.OrcaClient", FakeOrca)

    assert (
        main(
            [
                "--config",
                str(config_path),
                "workflow",
                "link",
                workflow.id,
                "https://github.com/example/project/issues/9",
                "--relation",
                "related",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_id"] == workflow.run_id
    assert payload["url"] == "https://github.com/example/project/issues/9"

    assert (
        main(
            [
                "--config",
                str(config_path),
                "workflow",
                "link",
                "missing-id",
                "https://github.com/example/project/issues/10",
                "--relation",
                "related",
            ]
        )
        == 2
    )
    assert "was not found" in capsys.readouterr().err


def _attach_finished_roles(store: WorkflowStore, tmp_path: Path, *, manager_owns: bool = True):
    manager, worker, reviewer = store.create_orchestrated_plan(tmp_path, "task", "Implement.")
    roles = (
        (manager, "manager", manager_owns),
        (worker, "worker", True),
        (reviewer, "reviewer", True),
    )
    for record, name, owns in roles:
        store.begin_start(record.id)
        store.attach_external(
            record.id,
            adapter_reference=f"repo::{name}",
            worktree_path=f"/tmp/{name}",
            terminal_handle=f"term-{name}",
            owns_worktree=owns,
            implementation_repository="example/repo",
            runtime_repository_id="repo",
            start_sha="bbb222" if name == "reviewer" else "aaa111",
        )
        store.transition(record.id, "running")
        store.transition(record.id, "completed")
    store.set_orchestration_outcome(manager.id, "completed")
    return manager, worker, reviewer


class _HarvestOrcaClient:
    def __init__(self, _executable: str) -> None:
        self.closed: list[tuple[str, str | None]] = []
        self.removed: list[str] = []
        self.integrated = 0

    def verify(self) -> dict[str, str]:
        return {"app_version": "test", "skill": "orchestration"}

    def verify_implementation_identity(self, *_args) -> None:
        return None

    def implementation_identity(self, _worktree_id: str, worktree_path: str):
        sha = "aaa111" if worktree_path.endswith("manager") else "bbb222"
        return ("example/repo", "repo", sha)

    def integrate_worker_commit(self, *_args, **_kwargs) -> dict[str, str | None]:
        self.integrated += 1
        return {"method": "ff", "before": "aaa111", "after": "bbb222"}

    def close_terminals(self, worktree_id: str, handle: str | None = None) -> dict[str, bool]:
        self.closed.append((worktree_id, handle))
        return {"closed": True}

    def remove_worktree(self, worktree_id: str) -> dict[str, bool]:
        self.removed.append(worktree_id)
        return {"removed": True}


def test_cli_retire_keep_manager_closes_children_only(tmp_path: Path, capsys, monkeypatch) -> None:
    state_dir = tmp_path / "state"
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, state_dir=state_dir)
    store = WorkflowStore(state_dir)
    manager, worker, _reviewer = _attach_finished_roles(store, tmp_path)
    client_cls = type("Client", (_HarvestOrcaClient,), {})
    _install_orca(monkeypatch, client_cls)

    result = main(
        [
            "--config",
            str(config_path),
            "workflow",
            "retire",
            manager.id,
            worker.id,
            "--keep",
            "manager",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload["keep"] == "manager"
    assert len(payload["results"]) == 1
    closed = {
        (item["adapter_reference"], item["handle"])
        for item in payload["results"][0]["closed_handles"]
    }
    assert ("repo::worker", "term-worker") in closed
    assert ("repo::manager", "term-manager") not in closed
    assert "repo::manager" not in payload["results"][0]["removed_worktrees"]


def test_cli_delivery_check_requires_manager_at_approved_sha(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    state_dir = tmp_path / "state"
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, state_dir=state_dir)
    store = WorkflowStore(state_dir)
    manager, _worker, _reviewer = _attach_finished_roles(store, tmp_path)

    _install_orca(monkeypatch, _HarvestOrcaClient)
    refused = main(["--config", str(config_path), "workflow", "delivery-check", manager.id])
    payload = json.loads(capsys.readouterr().out)
    assert refused == 2
    assert payload["eligible"] is False
    assert payload["results"][0]["approved_sha"] == "bbb222"
    assert payload["results"][0]["actual_sha"] == "aaa111"

    class HarvestedClient(_HarvestOrcaClient):
        def implementation_identity(self, _worktree_id: str, _worktree_path: str):
            return ("example/repo", "repo", "bbb222")

    _install_orca(monkeypatch, HarvestedClient)
    allowed = main(["--config", str(config_path), "workflow", "delivery-check", manager.id])
    payload = json.loads(capsys.readouterr().out)
    assert allowed == 0
    assert payload["eligible"] is True
    assert payload["results"][0]["approved_sha"] == "bbb222"


def test_cli_retire_refuses_running_and_failed_harvest_does_not_remove(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    state_dir = tmp_path / "state"
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, state_dir=state_dir)
    store = WorkflowStore(state_dir)
    running, *_ = store.create_orchestrated_plan(tmp_path, "live", "Implement.")
    manager, worker, _reviewer = _attach_finished_roles(store, tmp_path / "done")

    class FailingClient(_HarvestOrcaClient):
        def integrate_worker_commit(self, *_args, **_kwargs):
            raise RuntimeError("harvest failed")

    _install_orca(monkeypatch, FailingClient)
    refused = main(["--config", str(config_path), "workflow", "harvest", running.id, manager.id])
    payload = json.loads(capsys.readouterr().out)
    assert refused == 2
    assert any("completed, blocked, or failed" in error for error in payload["errors"])
    assert any("harvest failed" in error for error in payload["errors"])
    assert payload["results"] == []
    finished = WorkflowStore(state_dir).get(worker.id)
    assert finished.external_reconciled_at is None


def test_supervise_keeps_queue_wait_as_waiting_resource(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    root = _autonomous_root(tmp_path, capsys, monkeypatch)
    worker, _reviewer = root.service.store.children(root.manager_id)
    root.ready(root.manager_id, "plan", "Plan.", "Implement the plan.")
    assert root.supervise()["action"] == "launched-worker"
    queue = ResourceQueue(tmp_path / "state")
    holder_workflow = root.service.store.create(tmp_path, "single", "holder", "Hold the lease.")
    root.service.store.transition(holder_workflow.id, "starting")
    root.service.store.transition(holder_workflow.id, "running")
    holder = queue.acquire("heavy-check", holder_workflow.id)
    waiting = queue.acquire("heavy-check", worker.id)
    assert holder.granted is True
    assert waiting.granted is False
    parked = root.supervise()
    assert parked["action"] == "waiting_resource"
    progress = _progress(root.config_path, worker.id, capsys)
    assert progress["stage"] == "waiting_resource"


def test_supervise_wait_timeout_keeps_waiting_resource(tmp_path: Path, capsys, monkeypatch) -> None:
    root = _autonomous_root(
        tmp_path,
        capsys,
        monkeypatch,
        queue={"observer": False, "resources": ["heavy-check"], "wait_timeout_seconds": 1},
    )
    worker, _reviewer = root.service.store.children(root.manager_id)
    root.ready(root.manager_id, "plan", "Plan.", "Implement the plan.")
    assert root.supervise()["action"] == "launched-worker"
    queue = ResourceQueue(tmp_path / "state")
    holder_workflow = root.service.store.create(tmp_path, "single", "holder", "Hold the lease.")
    root.service.store.transition(holder_workflow.id, "starting")
    root.service.store.transition(holder_workflow.id, "running")
    queue.acquire("heavy-check", holder_workflow.id)
    waiting = queue.acquire("heavy-check", worker.id)
    assert waiting.granted is False
    with queue._connect() as connection:
        connection.execute(
            "UPDATE queue_requests SET created_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", waiting.request_id),
        )
        connection.commit()
    parked = root.supervise()
    assert parked["action"] == "waiting_resource"
    assert queue.inspect(waiting.request_id)["status"] == "waiting"
    assert root.service.store.orchestration_run(root.manager_id).status == "running"


def test_supervise_does_not_expire_a_live_lease(tmp_path: Path, capsys, monkeypatch) -> None:
    root = _autonomous_root(
        tmp_path,
        capsys,
        monkeypatch,
        queue={"observer": False, "resources": ["heavy-check"], "lease_timeout_seconds": 1},
    )
    worker, _reviewer = root.service.store.children(root.manager_id)
    root.ready(root.manager_id, "plan", "Plan.", "Implement the plan.")
    assert root.supervise()["action"] == "launched-worker"
    queue = ResourceQueue(tmp_path / "state")
    lease = queue.acquire("heavy-check", worker.id)
    assert lease.granted is True
    with queue._connect() as connection:
        connection.execute(
            "UPDATE queue_requests SET updated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", lease.request_id),
        )
        connection.commit()
    parked = root.supervise()
    assert parked["action"] == "waiting_resource"
    assert queue.inspect(lease.request_id)["status"] == "leased"
    assert root.service.store.orchestration_run(root.manager_id).status == "running"


def test_supervise_expires_dead_holder_lease_and_promotes(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    root = _autonomous_root(
        tmp_path,
        capsys,
        monkeypatch,
        queue={"observer": False, "resources": ["heavy-check"]},
    )
    worker, _reviewer = root.service.store.children(root.manager_id)
    root.ready(root.manager_id, "plan", "Plan.", "Implement the plan.")
    assert root.supervise()["action"] == "launched-worker"
    worker = root.service.store.get(worker.id)
    queue = ResourceQueue(tmp_path / "state")
    lease = queue.acquire("heavy-check", worker.id)
    waiter = root.service.store.create(tmp_path, "single", "waiter", "Wait for the lease.")
    root.service.store.transition(waiter.id, "starting")
    root.service.store.transition(waiter.id, "running")
    waiting = queue.acquire("heavy-check", waiter.id)
    assert waiting.granted is False
    root.closed.add(worker.terminal_handle)
    blocked = root.supervise()
    assert blocked["action"] == "blocked"
    assert "resource-timeout: lease" in blocked["reason"]
    assert queue.inspect(lease.request_id)["status"] == "cancelled"
    assert queue.inspect(waiting.request_id)["status"] == "leased"
    assert root.service.store.get(waiter.id).status.value == "running"


def test_supervise_skips_role_timeout_when_role_ready_is_pending(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    root = _autonomous_root(tmp_path, capsys, monkeypatch, timeouts={"role_seconds": 1})
    worker, _reviewer = root.service.store.children(root.manager_id)
    root.ready(root.manager_id, "plan", "Plan.", "Implement the plan.")
    assert root.supervise()["action"] == "launched-worker"
    root.repository_head["sha"] = "sha-1"
    root.ready(worker.id, "verification", "Checks passed.", "Review the implementation.")
    with root.service.store._connect() as connection:
        connection.execute(
            "UPDATE workflows SET activated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", worker.id),
        )
    launched = root.supervise()
    assert launched["action"] == "launched-reviewer"
    assert root.service.store.orchestration_run(root.manager_id).status == "running"


def test_queue_acquire_and_release_refresh_activated_at(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    root = _autonomous_root(
        tmp_path,
        capsys,
        monkeypatch,
        queue={"observer": False, "resources": ["heavy-check"]},
    )
    worker, _reviewer = root.service.store.children(root.manager_id)
    root.ready(root.manager_id, "plan", "Plan.", "Implement the plan.")
    assert root.supervise()["action"] == "launched-worker"
    worker = root.service.store.get(worker.id)
    with root.service.store._connect() as connection:
        connection.execute(
            "UPDATE workflows SET activated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", worker.id),
        )
    assert (
        main(
            [
                "--config",
                str(root.config_path),
                "queue",
                "acquire",
                "heavy-check",
                "--owner",
                worker.id,
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    granted = root.service.store.get(worker.id)
    assert granted.activated_at is not None
    assert granted.activated_at.startswith("2000") is False
    waiter = root.service.store.create(tmp_path, "single", "waiter", "Wait.")
    root.service.store.transition(waiter.id, "starting")
    root.service.store.transition(waiter.id, "running")
    with root.service.store._connect() as connection:
        connection.execute(
            "UPDATE workflows SET activated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", waiter.id),
        )
    waiting = ResourceQueue(tmp_path / "state").acquire("heavy-check", waiter.id)
    assert waiting.granted is False
    assert (
        main(
            [
                "--config",
                str(root.config_path),
                "queue",
                "release",
                "heavy-check",
                "--lease",
                payload["request_id"],
                "--owner",
                worker.id,
            ]
        )
        == 0
    )
    capsys.readouterr()
    promoted = root.service.store.get(waiter.id)
    assert promoted.activated_at is not None
    assert promoted.activated_at.startswith("2000") is False


def test_supervise_role_timeout_blocks_running_role(tmp_path: Path, capsys, monkeypatch) -> None:
    root = _autonomous_root(tmp_path, capsys, monkeypatch, timeouts={"role_seconds": 1})
    with root.service.store._connect() as connection:
        connection.execute(
            "UPDATE workflows SET activated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", root.manager_id),
        )
    blocked = root.supervise()
    assert blocked["action"] == "blocked"
    assert blocked["reason"] == "role-timeout"


def test_cli_supervise_single_timeout_does_not_require_orchestration_run(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    state_dir = tmp_path / "state"
    config_path = tmp_path / "config.jsonc"
    write_config(config_path, state_dir=state_dir, timeouts={"role_seconds": 1})
    store = WorkflowStore(state_dir)
    workflow = store.create(tmp_path, "single", "single-timeout", "Implement.")
    store.transition(workflow.id, "starting")
    store.attach_external(
        workflow.id,
        adapter_reference="repo::single",
        worktree_path=str(tmp_path),
        terminal_handle="term-single",
    )
    store.transition(workflow.id, "running")
    with store._connect() as connection:
        connection.execute(
            "UPDATE workflows SET activated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", workflow.id),
        )

    class SingleWatchdogClient(_HarvestOrcaClient):
        def set_lifecycle(self, *_args) -> None:
            return None

    _install_orca(monkeypatch, SingleWatchdogClient)

    result = main(
        [
            "--config",
            str(config_path),
            "workflow",
            "supervise",
            "--once",
            workflow.id,
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload["action"] == "blocked"
    assert payload["reason"] == "role-timeout"
    assert WorkflowStore(state_dir).get(workflow.id).status.value == "cancelled"


def test_cli_supervise_watchdog_does_not_expire_another_run_queue_wait(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    state_dir = tmp_path / "state"
    config_path = tmp_path / "config.jsonc"
    write_config(
        config_path,
        state_dir=state_dir,
        queue={"observer": False, "resources": ["heavy-check"], "wait_timeout_seconds": 1},
    )
    store = WorkflowStore(state_dir)
    supervised = store.create(tmp_path, "single", "supervised", "Implement.")
    other = store.create(tmp_path, "single", "other", "Implement.")
    for workflow in (supervised, other):
        store.transition(workflow.id, "starting")
        store.attach_external(
            workflow.id,
            adapter_reference=f"repo::{workflow.id}",
            worktree_path=str(tmp_path / workflow.id),
            terminal_handle=f"term-{workflow.id}",
        )
        store.transition(workflow.id, "running")
    queue = ResourceQueue(state_dir)
    queue.acquire("heavy-check", supervised.id)
    waiting = queue.acquire("heavy-check", other.id)
    with queue._connect() as connection:
        connection.execute(
            "UPDATE queue_requests SET created_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", waiting.request_id),
        )
    _install_orca(monkeypatch, _HarvestOrcaClient)

    result = main(
        [
            "--config",
            str(config_path),
            "workflow",
            "supervise",
            "--once",
            supervised.id,
        ]
    )
    captured = capsys.readouterr()

    assert result == 0, captured.err
    payload = json.loads(captured.out)
    assert payload == {"action": "waiting_resource", "workflow_id": supervised.id}
    assert queue.inspect(waiting.request_id)["status"] == "waiting"
    assert WorkflowStore(state_dir).get(other.id).status.value == "running"


def test_supervise_completed_run_pushes_from_manager(tmp_path: Path, capsys, monkeypatch) -> None:
    root = _autonomous_root(tmp_path, capsys, monkeypatch)
    manager = root.service.store.get(root.manager_id)
    worker, reviewer = root.service.store.children(root.manager_id)
    root.ready(manager.id, "plan", "Plan.", "Implement the plan.")
    assert root.supervise()["action"] == "launched-worker"
    root.repository_head["sha"] = "sha-1"
    root.ready(worker.id, "verification", "Checks passed.", "Review the implementation.")
    assert root.supervise()["action"] == "launched-reviewer"
    root.ready(reviewer.id, "review", "Approved.", "Approved.", "approved")
    completed = root.supervise()
    assert completed["action"] == "completed"
    assert completed["delivery"]["pushed"]["remote"] == "origin"
    assert root.client_cls.pushed
