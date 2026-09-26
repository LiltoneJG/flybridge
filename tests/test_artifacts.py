from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

import pytest
from conftest import write_config
from flybridge_cli.main import main
from flybridge_core import MAX_ARTIFACT_BYTES, WorkflowArtifactStore, WorkflowStore


def _role_plan(tmp_path: Path):
    state_dir = tmp_path / "state"
    store = WorkflowStore(state_dir)
    manager, worker, reviewer = store.create_orchestrated_plan(
        tmp_path / "implementation", "durable", "Implement."
    )
    return state_dir, store, manager, worker, reviewer


def test_artifacts_are_private_durable_and_resolve_to_the_manager(tmp_path: Path) -> None:
    state_dir, store, manager, worker, reviewer = _role_plan(tmp_path)
    artifacts = WorkflowArtifactStore(state_dir, store)

    plan = artifacts.put(manager.id, "plan", "# Plan\n\nDo the work.\n")
    verification = artifacts.put(worker.id, "verification", "# Verification\n\nPassed.\n")
    review = artifacts.put(reviewer.id, "review", "# Review\n\nApproved.\n")

    assert [item.kind for item in artifacts.list_for_workflow(worker.id)] == [
        "plan",
        "verification",
        "review",
    ]
    assert {item.root_manager_id for item in (plan, verification, review)} == {manager.id}
    assert artifacts.read(reviewer.id, "plan")[1] == "# Plan\n\nDo the work.\n"
    directory = state_dir / "artifacts" / "workflows" / manager.id
    expected_name = f"plan.{plan.sha256}.md"
    assert plan.relative_path == f"workflows/{manager.id}/{expected_name}"
    assert (directory / expected_name).read_text(encoding="utf-8").startswith("# Plan")
    if os.name != "nt":
        assert (state_dir / "artifacts" / "workflows").stat().st_mode & 0o777 == 0o700
        assert directory.stat().st_mode & 0o777 == 0o700
        assert (directory / expected_name).stat().st_mode & 0o777 == 0o600
    assert plan.sha256 == artifacts.verify(manager.id, "plan").sha256
    assert plan.byte_size == len(b"# Plan\n\nDo the work.\n")


def test_multiple_reviewers_keep_separate_review_artifacts(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    store = WorkflowStore(state_dir)
    manager, worker, first, second = store.create_orchestrated_plan(
        tmp_path / "implementation", "multi", "Implement.", reviewer_count=2
    )
    artifacts = WorkflowArtifactStore(state_dir, store)
    artifacts.put(manager.id, "plan", "# Plan\n")
    artifacts.put(worker.id, "verification", "# Verification\n")
    first_review = artifacts.put(first.id, "review", "# First\n")
    second_review = artifacts.put(second.id, "review", "# Second\n")

    assert first_review.sha256 != second_review.sha256
    assert artifacts.read(first.id, "review")[1] == "# First\n"
    assert artifacts.read(second.id, "review")[1] == "# Second\n"
    assert [item.workflow_id for item in artifacts.list_for_kind(manager.id, "review")] == [
        first.id,
        second.id,
    ]


@pytest.mark.parametrize(
    ("role_index", "kind", "message"),
    [
        (1, "plan", "manager"),
        (0, "verification", "worker"),
        (1, "review", "reviewer"),
    ],
)
def test_artifact_writes_enforce_role_ownership(
    tmp_path: Path, role_index: int, kind: str, message: str
) -> None:
    state_dir, store, manager, worker, _reviewer = _role_plan(tmp_path)
    workflow = (manager, worker)[role_index]

    with pytest.raises(ValueError, match=message):
        WorkflowArtifactStore(state_dir, store).put(workflow.id, kind, "content")


def test_artifacts_reject_empty_oversized_and_single_workflow_content(tmp_path: Path) -> None:
    state_dir, store, manager, _worker, _reviewer = _role_plan(tmp_path)
    artifacts = WorkflowArtifactStore(state_dir, store)

    with pytest.raises(ValueError, match="empty"):
        artifacts.put(manager.id, "plan", " \n")
    with pytest.raises(ValueError, match="limit"):
        artifacts.put(manager.id, "plan", "x" * (MAX_ARTIFACT_BYTES + 1))
    single = store.create(tmp_path, "single", "single", "Do it.")
    with pytest.raises(ValueError, match="orchestrated"):
        artifacts.put(single.id, "plan", "content")
    with pytest.raises(ValueError, match="orchestrated"):
        artifacts.put(single.id, "verification", "content")


def test_artifact_replacement_is_atomic_and_preserves_creation_time(tmp_path: Path) -> None:
    state_dir, store, manager, _worker, _reviewer = _role_plan(tmp_path)
    artifacts = WorkflowArtifactStore(state_dir, store)
    first = artifacts.put(manager.id, "plan", "first")
    first_path = state_dir / "artifacts" / first.relative_path

    replaced = artifacts.put(manager.id, "plan", "second")

    assert replaced.created_at == first.created_at
    assert replaced.relative_path != first.relative_path
    assert artifacts.read(manager.id, "plan")[1] == "second"
    directory = state_dir / "artifacts" / "workflows" / manager.id
    assert first_path.exists()
    assert artifacts.read_digest(manager.id, "plan", first.sha256) == "first"
    assert (directory / f"plan.{replaced.sha256}.md").is_file()
    assert list(directory.glob(".*.tmp")) == []


def test_artifact_verification_detects_tampering_and_invalid_utf8(tmp_path: Path) -> None:
    state_dir, store, manager, _worker, _reviewer = _role_plan(tmp_path)
    artifacts = WorkflowArtifactStore(state_dir, store)
    artifact = artifacts.put(manager.id, "plan", "original")
    path = state_dir / "artifacts" / artifact.relative_path

    path.write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256"):
        artifacts.verify(manager.id, "plan")

    invalid = b"\xff" * artifact.byte_size
    digest = hashlib.sha256(invalid).hexdigest()
    invalid_path = path.with_name(f"plan.{digest}.md")
    path.write_bytes(invalid)
    path.rename(invalid_path)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            UPDATE workflow_artifacts
            SET sha256 = ?, relative_path = ?
            WHERE root_manager_id = ? AND kind = 'plan'
            """,
            (digest, f"workflows/{manager.id}/plan.{digest}.md", manager.id),
        )
    with pytest.raises(ValueError, match="UTF-8"):
        artifacts.read(manager.id, "plan")


def test_artifact_rejects_symlinks_and_traversal_metadata(tmp_path: Path) -> None:
    state_dir, store, manager, _worker, _reviewer = _role_plan(tmp_path)
    artifacts = WorkflowArtifactStore(state_dir, store)
    artifact = artifacts.put(manager.id, "plan", "safe")
    path = state_dir / "artifacts" / artifact.relative_path
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    path.unlink()
    path.symlink_to(outside)

    with pytest.raises(OSError, match="symbolic link"):
        artifacts.verify(manager.id, "plan")

    path.unlink()
    path.write_text("safe", encoding="utf-8")
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE workflow_artifacts SET relative_path = '../outside.md' "
            "WHERE root_manager_id = ? AND kind = 'plan'",
            (manager.id,),
        )
    with pytest.raises(ValueError, match="unsafe relative path"):
        artifacts.verify(manager.id, "plan")

    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE workflow_artifacts SET relative_path = ? "
            "WHERE root_manager_id = ? AND kind = 'plan'",
            (f"workflows/{manager.id}/plan.md", manager.id),
        )
    with pytest.raises(ValueError, match="unsafe relative path"):
        artifacts.verify(manager.id, "plan")

    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE workflow_artifacts SET sha256 = ? WHERE root_manager_id = ? AND kind = 'plan'",
            ("0" * 64, manager.id),
        )
    with pytest.raises(ValueError, match="unsafe relative path"):
        artifacts.verify(manager.id, "plan")


def test_put_keeps_previous_artifact_when_metadata_commit_fails(
    tmp_path: Path, monkeypatch
) -> None:
    state_dir, store, manager, _worker, _reviewer = _role_plan(tmp_path)
    artifacts = WorkflowArtifactStore(state_dir, store)
    first = artifacts.put(manager.id, "plan", "first")
    original_connect = artifacts._connect

    class FailingConnection:
        def __init__(self, inner: sqlite3.Connection) -> None:
            self._inner = inner

        def execute(self, sql, parameters=()):
            if "INSERT INTO workflow_artifacts" in " ".join(str(sql).split()):
                raise sqlite3.OperationalError("injected metadata failure")
            return self._inner.execute(sql, parameters)

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *args):
            return self._inner.__exit__(*args)

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

    def failing_connect():
        return FailingConnection(original_connect())

    monkeypatch.setattr(artifacts, "_connect", failing_connect)

    with pytest.raises(sqlite3.OperationalError, match="injected metadata failure"):
        artifacts.put(manager.id, "plan", "second")

    monkeypatch.setattr(artifacts, "_connect", original_connect)
    current = artifacts.read(manager.id, "plan")
    assert current[1] == "first"
    assert current[0].relative_path == first.relative_path
    assert (state_dir / "artifacts" / first.relative_path).is_file()
    directory = state_dir / "artifacts" / "workflows" / manager.id
    orphan = next(
        path for path in directory.glob("plan.*.md") if path.name != Path(first.relative_path).name
    )
    assert orphan.is_file()
    assert hashlib.sha256(orphan.read_bytes()).hexdigest() == hashlib.sha256(b"second").hexdigest()


def test_identical_content_reuses_the_immutable_file(tmp_path: Path) -> None:
    state_dir, store, manager, _worker, _reviewer = _role_plan(tmp_path)
    artifacts = WorkflowArtifactStore(state_dir, store)
    first = artifacts.put(manager.id, "plan", "same-plan")
    replaced = artifacts.put(manager.id, "plan", "same-plan")

    assert replaced.relative_path == first.relative_path
    directory = state_dir / "artifacts" / "workflows" / manager.id
    assert [path.name for path in directory.glob("plan.*.md")] == [Path(first.relative_path).name]


def test_artifact_store_rejects_a_symlinked_storage_root(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    store = WorkflowStore(state_dir)
    outside = tmp_path / "outside"
    outside.mkdir()
    (state_dir / "artifacts").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError, match="symbolic link"):
        WorkflowArtifactStore(state_dir, store)


def test_malformed_v1_schema_is_rejected_without_mutating_history(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "state")
    store.create(tmp_path, "single", "preserved", "Keep this record.")
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TABLE workflow_role_readiness")
        connection.execute("DROP TABLE orchestration_runs")
        connection.execute("DROP INDEX workflow_artifact_path")
        connection.execute("DROP TABLE workflow_artifacts")
        connection.execute("ALTER TABLE workflows DROP COLUMN runtime_repository_id")
        connection.execute("ALTER TABLE workflows DROP COLUMN start_sha")
        connection.execute("ALTER TABLE workflows DROP COLUMN implementation_repository")

    with pytest.raises(RuntimeError, match="malformed Flybridge state schema"):
        WorkflowStore(tmp_path / "state")


def test_future_schema_is_rejected_without_reclassifying_identity(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "state")
    workflow = store.create(tmp_path, "single", "v2-identity", "Preserve identity.")
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TABLE workflow_role_readiness")
        connection.execute("DROP TABLE orchestration_runs")
        connection.execute(
            "UPDATE workflows SET implementation_repository = ?, start_sha = ? WHERE id = ?",
            ("orca-runtime-repository", "abc123", workflow.id),
        )
        connection.execute("DROP INDEX workflow_artifact_path")
        connection.execute("DROP TABLE workflow_artifacts")
        connection.execute("ALTER TABLE workflows DROP COLUMN runtime_repository_id")
        connection.execute("PRAGMA user_version = 4")

    with pytest.raises(RuntimeError, match="unsupported Flybridge state schema"):
        WorkflowStore(tmp_path / "state")


def test_future_readiness_schema_is_rejected_without_mutating_the_database(
    tmp_path: Path,
) -> None:
    state_dir, store, _manager, _worker, reviewer = _role_plan(tmp_path)
    store.transition(reviewer.id, "starting")
    store.transition(reviewer.id, "running", adapter_reference="reviewer")
    readiness = store.record_role_readiness(
        reviewer.id,
        summary="Approved.",
        outcome="approved",
        artifact_kind="review",
        artifact_sha256="a" * 64,
        artifact_content="Approved.",
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute("ALTER TABLE workflow_role_readiness DROP COLUMN blocked_reason")
        connection.execute("PRAGMA user_version = 4")

    with pytest.raises(RuntimeError, match="unsupported Flybridge state schema"):
        WorkflowStore(state_dir)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        row = connection.execute(
            "SELECT outcome FROM workflow_role_readiness WHERE id = ?", (readiness.id,)
        ).fetchone()
        assert row == ("approved",)


def test_cli_put_show_verify_and_status_include_artifact_metadata(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    state_dir, store, manager, _worker, _reviewer = _role_plan(tmp_path)
    config = write_config(
        tmp_path / "flybridge.jsonc",
        state_dir=state_dir,
        reconcile={"auto_before_workflow_commands": False},
    )
    source = tmp_path / "plan-source.md"
    source.write_text("# Plan\n\nCLI file.\n", encoding="utf-8")

    assert (
        main(
            [
                "--config",
                str(config),
                "workflow",
                "artifact",
                "put",
                manager.id,
                "--kind",
                "plan",
                "--file",
                str(source),
            ]
        )
        == 0
    )
    put = json.loads(capsys.readouterr().out)
    assert put["artifact"]["kind"] == "plan"

    monkeypatch.setattr(sys, "stdin", io.StringIO("# Plan\n\nCLI stdin replacement.\n"))
    assert (
        main(
            [
                "--config",
                str(config),
                "workflow",
                "artifact",
                "put",
                manager.id,
                "--kind",
                "plan",
                "--stdin",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        main(
            [
                "--config",
                str(config),
                "workflow",
                "artifact",
                "show",
                manager.id,
                "--kind",
                "plan",
            ]
        )
        == 0
    )
    assert "stdin replacement" in json.loads(capsys.readouterr().out)["content"]

    assert (
        main(
            [
                "--config",
                str(config),
                "workflow",
                "artifact",
                "verify",
                manager.id,
                "--kind",
                "plan",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["verified"] is True

    assert main(["--config", str(config), "workflow", "status", manager.id]) == 0
    status = json.loads(capsys.readouterr().out)
    expected = asdict(WorkflowArtifactStore(state_dir, store).get(manager.id, "plan"))
    assert status["artifacts"] == [expected]
