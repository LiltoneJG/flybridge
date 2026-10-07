"""Recovery fixtures never launch Orca, an agent, or an external check."""

import json
import os
import subprocess
from types import SimpleNamespace

import pytest
from flybridge_application import WorkflowService
from flybridge_cli.commands import workflow as cli
from flybridge_cli.main import main
from flybridge_core import BatchStore, ResourceQueue, WorkflowStore
from flybridge_orca.client import OrcaClient, OrcaError
from flybridge_orca.continuation import inspect_process

SESSION = "10000000-0000-4000-8000-000000000001"
HEAD = "a" * 40
PROOF = {"incarnation_id": "incarnation", "process_started": "boot:100"}


@pytest.fixture
def setup(tmp_path):
    store = WorkflowStore(tmp_path / "state")
    source = store.create(tmp_path, "single", "timed-out", "Old scope")
    store.begin_start(source.id)
    store.attach_external(
        source.id,
        adapter_reference="repo::checkout",
        worktree_path=str(tmp_path),
        terminal_handle="old",
        owns_worktree=False,
        implementation_repository="example/repo",
        runtime_repository_id="repo",
        start_sha="baseline",
    )
    store.transition(source.id, "running")
    store.transition(source.id, "cancelled", error="role-timeout")
    store.mark_external_reconciled(source.id)

    class Runtime:
        proof = PROOF.copy()
        fail = False
        inspections = 0
        changed = False

        def verify_worktree(self, *args):
            assert args == ("repo::checkout", str(tmp_path))

        def verify_implementation_identity(self, *args):
            pytest.fail("continuation must not require predecessor SHA ancestry")

        def implementation_identity(self, *args):
            return "example/repo", "repo", HEAD

        def uncommitted_changes(self, *args):
            return ()

        def inspect_timeout_continuation(self, *args):
            assert args == ("repo::checkout", str(tmp_path), "old", "new", SESSION, 123)
            self.inspections += 1
            if self.fail:
                raise OrcaError("busy or identity mismatch")
            if self.changed and self.inspections == 2:
                return {**self.proof, "process_started": "boot:200"}
            return self.proof.copy()

        def verify_timeout_continuation_local(self, *args):
            assert args[8] == HEAD and args[9] == self.proof
            assert self.implementation_identity() == ("example/repo", "repo", HEAD)
            assert not self.uncommitted_changes()

        def __getattr__(self, name):
            raise AssertionError("unexpected runtime operation: " + name)

    runtime = Runtime()
    service = WorkflowService(store)

    def adopt(**kwargs):
        return service.continue_timeout(
            source.id,
            runtime,
            terminal="new",
            session=SESSION,
            agent_pid=123,
            objective="Current scope",
            expected_head=HEAD,
            **kwargs,
        )

    return store, source.id, runtime, adopt


def test_dry_run_and_adoption_preserve_old_history_and_other_resources(setup, tmp_path):
    store, source, runtime, adopt = setup
    before = store.get(source)
    queue = ResourceQueue(tmp_path / "state")
    other = queue.acquire("unrelated", "other-owner")
    assert adopt()["eligible"]
    assert len(store.list_workflows()) == 1
    result = adopt(apply=True)
    assert store.get(source) == before
    owner = store.get(result["workflow_id"])
    assert owner.status == "running" and owner.objective == "Current scope"
    assert not owner.owns_worktree and owner.run_id == owner.id
    assert owner.start_sha == HEAD and owner.issue_url is None
    assert owner.terminal_handle == "new" and owner.activated_at > before.activated_at
    assert store.continuation(source)["workflow_id"] == owner.id
    assert store.owned_terminal_handles(owner.id) == ["new"]
    assert queue.acquire("checks", owner.id).granted
    assert queue.inspect(other.request_id)["status"] == "leased"
    batches = BatchStore(tmp_path / "state")
    assert batches.single_report(owner.id) is None
    queue.cancel_owner(owner.id)
    batches.report_single(owner.id, "blocked", "New blocker")
    assert batches.single_report(owner.id)["current"]
    again = adopt(apply=True)
    assert again["already_bound"] and again["workflow_id"] == owner.id
    assert store.get(owner.id).activated_at == owner.activated_at
    assert runtime.inspections >= 4


@pytest.mark.parametrize(
    "invalid",
    [
        "user-cancelled",
        "cleanup",
        "unreconciled",
        "lifecycle",
        "waiting",
        "leased",
        "pending-result",
        "recovery",
        "removing",
        "name",
        "terminal",
        "worktree",
    ],
)
def test_refusals_leave_actor_and_records_unchanged(setup, tmp_path, invalid):
    store, source, _runtime, adopt = setup
    queue = ResourceQueue(tmp_path / "state")
    with store._connect() as c:
        if invalid == "user-cancelled":
            c.execute("UPDATE workflows SET error='operator cancelled' WHERE id=?", (source,))
        elif invalid == "cleanup":
            c.execute("UPDATE workflows SET cleanup_error='failed' WHERE id=?", (source,))
        elif invalid == "unreconciled":
            c.execute("UPDATE workflows SET external_reconciled_at=NULL WHERE id=?", (source,))
        elif invalid == "lifecycle":
            c.execute(
                "INSERT INTO workflow_lifecycle_operations VALUES (?, 'terminal', 'cancelled', "
                "'repo::checkout', NULL, 'now', 'now')",
                (source,),
            )
    if invalid in {"waiting", "leased", "pending-result", "recovery"}:
        if invalid == "waiting":
            queue.acquire("checks", "other-owner")
        req = queue.acquire(
            "checks",
            source,
            **(
                {
                    "job_argv": ["mock"],
                    "cleanup_check": "/mock/check",
                    "worktree_path": str(tmp_path),
                }
                if invalid in {"pending-result", "recovery"}
                else {}
            ),
        )
        if invalid in {"pending-result", "recovery"}:
            queue.claim_job(req.request_id)
            queue.finish_job(
                req.request_id,
                command_exit_code=1,
                check_exit_code=0 if invalid == "pending-result" else 1,
            )
    elif invalid == "removing":
        store.record_retained_worktree(
            source, "repo::checkout", str(tmp_path), "baseline", owns_worktree=True
        )
        store.claim_worktree_removal(source, "repo::checkout")
    elif invalid in {"name", "terminal", "worktree"}:
        conflict = store.create(
            tmp_path,
            "single",
            "continuation-" + source if invalid == "name" else "other",
            "Unrelated",
        )
        if invalid != "name":
            store.begin_start(conflict.id)
            store.attach_external(
                conflict.id,
                adapter_reference="repo::checkout" if invalid == "worktree" else "other::checkout",
                worktree_path=str(tmp_path),
                terminal_handle="new" if invalid == "terminal" else "other",
            )
    before = store.get(source)
    count = len(store.list_workflows())
    with pytest.raises(ValueError):
        adopt(apply=True)
    assert store.get(source) == before
    assert len(store.list_workflows()) == count
    assert store.continuation(source) is None


@pytest.mark.parametrize("change", ["failed-inspection", "pid-reuse"])
def test_external_inspection_failure_rolls_back_without_closing_actor(setup, change):
    store, source, runtime, adopt = setup
    runtime.fail = change == "failed-inspection"
    runtime.changed = change == "pid-reuse"
    with pytest.raises((OrcaError, ValueError)):
        adopt(apply=True)
    assert len(store.list_workflows()) == 1
    assert store.get(source).status == "cancelled"
    assert store.continuation(source) is None


def test_different_second_binding_is_refused(setup):
    store, source, runtime, adopt = setup
    owner = adopt(apply=True)["workflow_id"]
    runtime.proof = {**PROOF, "incarnation_id": "replacement"}
    with pytest.raises(ValueError, match="different continuation"):
        adopt(apply=True)
    assert store.continuation(source)["workflow_id"] == owner


def test_schema_seven_migration_preserves_source(setup, tmp_path):
    store, source, _, _ = setup
    before = store.get(source)
    with store._connect() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 7
        assert (
            c.execute("SELECT 1 FROM sqlite_master WHERE name='workflow_continuations'").fetchone()
            is None
        )
    fresh = WorkflowStore(tmp_path / "state")
    assert fresh.get(source) == before and fresh.continuation(source) is None


def test_cli_binding_does_not_scan_launch_or_deliver(setup, tmp_path, monkeypatch, capsys):
    store, source, runtime, _ = setup
    monkeypatch.setattr(cli, "_config", lambda _: SimpleNamespace(state_dir=tmp_path / "state"))
    monkeypatch.setattr(cli, "_adapter", lambda _: runtime)
    monkeypatch.setattr(cli, "_sync_orca", lambda *a, **kw: pytest.fail("global scan"))
    args = [
        "workflow",
        "continue-timeout",
        source,
        "--terminal",
        "new",
        "--codex-session",
        SESSION,
        "--agent-pid",
        "123",
        "--expected-head",
        HEAD,
        "-o",
        "Current scope",
    ]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"]
    runtime.set_lifecycle = lambda *args: None
    assert main([*args, "--apply"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["metadata_updated"] and store.get(result["workflow_id"]).status == "running"


def proc_actor(proc, pid, path, terminal="new", session=SESSION, start="100"):
    d = proc / str(pid)
    d.mkdir()
    (d / "cmdline").write_bytes(("codex\0--no-daemon\0resume\0" + session + "\0").encode())
    (d / "environ").write_bytes(
        ("ORCA_TERMINAL_HANDLE=" + terminal + "\0ORCA_WORKTREE_ID=repo::checkout\0").encode()
    )
    (d / "stat").write_text(str(pid) + " (codex) S " + "0 " * 18 + start + " 0")
    (d / "cwd").symlink_to(path)
    executable = proc / "codex"
    executable.touch()
    (d / "exe").symlink_to(executable)
    return d


@pytest.mark.parametrize(
    "wrong",
    [None, "uuid", "handle", "worktree", "old-alive", "duplicate", "cwd", "zombie", "inspection"],
)
def test_local_process_identity_fails_closed(tmp_path, wrong):
    proc = tmp_path / "proc"
    boot = proc / "sys/kernel/random"
    boot.mkdir(parents=True)
    (boot / "boot_id").write_text("boot")
    actor = proc_actor(proc, 123, tmp_path)
    if wrong == "uuid":
        (actor / "cmdline").write_bytes(b"codex\0resume\0other-uuid\0")
    elif wrong == "handle":
        (actor / "environ").write_bytes(b"ORCA_TERMINAL_HANDLE=someone\0")
    elif wrong == "worktree":
        (actor / "environ").write_bytes(b"ORCA_TERMINAL_HANDLE=new\0ORCA_WORKTREE_ID=other\0")
    elif wrong in {"old-alive", "duplicate"}:
        proc_actor(proc, 124, tmp_path, terminal="old" if wrong == "old-alive" else "other")
    elif wrong == "cwd":
        (actor / "cwd").unlink()
        (actor / "cwd").symlink_to(proc)
    elif wrong == "zombie":
        (actor / "stat").write_text((actor / "stat").read_text().replace(" S ", " Z "))
    elif wrong == "inspection":
        (actor / "environ").unlink()
    if wrong:
        with pytest.raises((ValueError, OSError)):
            inspect_process("repo::checkout", str(tmp_path), "old", "new", SESSION, 123, proc=proc)
    else:
        assert (
            inspect_process("repo::checkout", str(tmp_path), "old", "new", SESSION, 123, proc=proc)
            == "boot:100"
        )


@pytest.mark.parametrize(
    "wrong", [None, "old-alive", "idle", "session-proof", "incarnation", "host"]
)
def test_orca_inspection_only_reads_and_waits(tmp_path, monkeypatch, wrong):
    import flybridge_orca.client as module

    monkeypatch.delenv("ORCA_ENVIRONMENT", raising=False)
    monkeypatch.delenv("ORCA_PAIRING_CODE", raising=False)
    calls = []
    shown = 0

    def runner(argv, **kwargs):
        nonlocal shown
        calls.append(argv)
        if argv[1:3] == ["terminal", "wait"]:
            result = {"wait": {"satisfied": wrong != "idle"}}
        elif "old" in argv:
            result = {
                "terminal": {
                    "handle": "old",
                    "worktreeId": "repo::checkout",
                    "connected": wrong == "old-alive",
                    "writable": False,
                    "executionHostId": "local",
                }
            }
        else:
            shown += 1
            result = {
                "terminal": {
                    "handle": "new",
                    "worktreeId": "repo::checkout",
                    "worktreePath": str(tmp_path),
                    "connected": True,
                    "writable": True,
                    "orphaned": False,
                    "agentIdentity": "codex",
                    "executionHostId": "remote" if wrong == "host" else "local",
                    "incarnationId": "changed"
                    if wrong == "incarnation" and shown == 2
                    else "incarnation",
                }
            }
        return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True, "result": result}), "")

    def process(*args):
        if wrong == "session-proof":
            raise ValueError("wrong session")
        return "boot:100"

    monkeypatch.setattr(module, "inspect_process", process)
    client = OrcaClient("fixture-orca", runner=runner)
    if wrong:
        with pytest.raises(OrcaError):
            client.inspect_timeout_continuation(
                "repo::checkout", str(tmp_path), "old", "new", SESSION, 123
            )
    else:
        assert (
            client.inspect_timeout_continuation(
                "repo::checkout", str(tmp_path), "old", "new", SESSION, 123
            )
            == PROOF
        )
    assert all(call[1:3] in (["terminal", "show"], ["terminal", "wait"]) for call in calls)


def test_unchanged_v7_consumers_use_same_db_without_restart(setup, tmp_path):
    """Pinned pre-correction source is the actual v7 consumer, not a modified reader."""
    import io
    import sys
    import tarfile

    store, source, _, adopt = setup
    with store._connect() as c:
        canonical = c.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
            "ORDER BY type,name"
        ).fetchall()
        canonical = [tuple(row) for row in canonical]
    result = adopt(apply=True)
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    archive = subprocess.run(
        ["git", "archive", "adedcdeb8eb6d8ca730a2bdeb62c6f568f424e91"],
        check=True,
        capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as files:
        files.extractall(legacy, filter="data")
    code = """
import sys
from pathlib import Path
from types import SimpleNamespace
for name in ('core','application','cli','orca','github'):
    sys.path.insert(0,str(Path(sys.argv[1])/'packages'/name/'src'))
from flybridge_core import WorkflowStore, ResourceQueue
from flybridge_core.database import SCHEMA_VERSION
from flybridge_cli.commands import queue as queue_cli
from flybridge_cli.runtime import _running_owner
from flybridge_cli.dispatcher import QueueDispatcher
assert SCHEMA_VERSION == 7
state=Path(sys.argv[2]); owner=sys.argv[3]; source=sys.argv[4]
s=WorkflowStore(state)
assert s.get(source).status == 'cancelled'
assert _running_owner(s,owner)==owner
try:
    _running_owner(s,source)
except ValueError:
    pass
else:
    raise AssertionError('cancelled owner unexpectedly runnable')
config=SimpleNamespace(state_dir=state, path=state/'fixture-config',orca_executable='mock-orca')
queue_cli._config=lambda _:config
queue_cli.ensure_dispatcher=lambda *args:None
queue_cli.handle(SimpleNamespace(queue_command='acquire',owner=owner,resource='legacy-check'))
q=ResourceQueue(state); request=q.owner_requests(owner)[0]
assert request['status']=='leased'
q.release(request['request_id'])
d=QueueDispatcher(config)
d._heartbeat()
assert q.queued_jobs()==[]
with s._connect() as c:
    assert c.execute('PRAGMA user_version').fetchone()[0]==7
print('unchanged v7 CLI/queue/dispatcher accepted continuation in the same database')
"""
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            code,
            str(legacy),
            str(tmp_path / "state"),
            result["workflow_id"],
            source,
        ],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "unchanged v7" in completed.stdout
    with store._connect() as c:
        after = c.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
            "AND name != 'workflow_continuations' ORDER BY type,name"
        ).fetchall()
        assert canonical == [tuple(row) for row in after]
        assert c.execute("PRAGMA user_version").fetchone()[0] == 7


def test_concurrent_identical_apply_binds_only_one_owner(setup):
    from concurrent.futures import ThreadPoolExecutor

    store, source, _, adopt = setup
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: adopt(apply=True), range(2)))
    assert results[0]["workflow_id"] == results[1]["workflow_id"]
    assert len(store.list_workflows()) == 2
    assert store.get(source).status == "cancelled"


@pytest.mark.parametrize("resource", ["cancelled-job", "resolved-job"])
def test_confirmed_cleanup_history_does_not_permanently_prevent_recovery(setup, tmp_path, resource):
    store, source, _, adopt = setup
    q = ResourceQueue(tmp_path / "state")
    request = q.acquire(
        "checks",
        source,
        job_argv=["mock"],
        cleanup_check="/mock/check",
        worktree_path=str(tmp_path),
    )
    if resource == "cancelled-job":
        q.cancel(request.request_id)
        q.resolve(request.request_id, cleanup_confirmed=True)
    else:
        q.claim_job(request.request_id)
        q.finish_job(request.request_id, command_exit_code=1, check_exit_code=1)
        q.resolve(request.request_id, cleanup_confirmed=True)
        q.acknowledge_result(request.request_id, source)
    assert adopt(apply=True)["workflow_id"] != source
    assert store.get(source).status == "cancelled"


@pytest.mark.parametrize(
    "mismatch", ["head", "repository", "guid", "dirty", "head-race", "dirty-race"]
)
def test_current_checkout_identity_fails_closed(setup, mismatch):
    store, source, runtime, adopt = setup
    before = store.get(source)
    calls = 0

    def identity(*args):
        nonlocal calls
        calls += 1
        if mismatch == "repository":
            return "other/repo", "repo", HEAD
        if mismatch == "guid":
            return "example/repo", "other", HEAD
        if mismatch == "head" or (mismatch == "head-race" and calls > 1):
            return "example/repo", "repo", "b" * 40
        return "example/repo", "repo", HEAD

    runtime.implementation_identity = identity
    runtime.uncommitted_changes = lambda *args: (
        ("dirty",) if mismatch == "dirty" or (mismatch == "dirty-race" and calls > 1) else ()
    )
    with pytest.raises(ValueError, match="identity"):
        adopt(apply=True)
    assert store.get(source) == before
    assert len(store.list_workflows()) == 1
    assert store.continuation(source) is None


def test_source_changed_during_preflight_is_rejected(setup):
    store, source, runtime, adopt = setup
    inspect = runtime.inspect_timeout_continuation

    def changed(*args):
        proof = inspect(*args)
        with store._connect() as connection:
            connection.execute("UPDATE workflows SET objective='changed' WHERE id=?", (source,))
        return proof

    runtime.inspect_timeout_continuation = changed
    with pytest.raises(ValueError, match="changed"):
        adopt(apply=True)
    assert len(store.list_workflows()) == 1
    assert store.continuation(source) is None


def test_slow_external_preflight_does_not_hold_shared_write_lock(setup):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    store, _, runtime, adopt = setup
    entered, release = Event(), Event()
    inspect = runtime.inspect_timeout_continuation

    def slow(*args):
        entered.set()
        assert release.wait(5)
        return inspect(*args)

    runtime.inspect_timeout_continuation = slow
    with ThreadPoolExecutor(max_workers=2) as pool:
        adoption = pool.submit(adopt, apply=True)
        assert entered.wait(2)

        def heartbeat():
            with store._connect() as connection:
                connection.execute("PRAGMA busy_timeout=100")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT OR REPLACE INTO queue_dispatcher_state VALUES (1, 999, 'test-heartbeat', NULL)"
                )

        try:
            pool.submit(heartbeat).result(timeout=1)
        finally:
            release.set()
        assert adoption.result(timeout=3)["workflow_id"]


@pytest.mark.parametrize("wrong", [None, "head", "dirty", "pid"])
def test_final_local_verification_has_no_orca_rpc(tmp_path, monkeypatch, wrong):
    import flybridge_orca.client as module

    replies = [
        "https://github.com/example/repo.git",
        "b" * 40 if wrong == "head" else HEAD,
        str(tmp_path),
        " M tracked" if wrong == "dirty" else "",
    ]
    calls = []

    def runner(argv, **kwargs):
        assert argv[0] == "git"
        assert 0 < kwargs["timeout"] <= 3
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, replies[len(calls) - 1], "")

    monkeypatch.setattr(
        module, "inspect_process", lambda *args: "boot:200" if wrong == "pid" else "boot:100"
    )
    client = OrcaClient("must-not-call-orca", runner=runner)
    args = (
        "repo::checkout",
        str(tmp_path),
        "old",
        "new",
        SESSION,
        123,
        "example/repo",
        "repo",
        HEAD,
        PROOF,
    )
    if wrong:
        with pytest.raises(OrcaError, match="final local"):
            client.verify_timeout_continuation_local(*args)
    else:
        client.verify_timeout_continuation_local(*args)
    assert len(calls) == 4
