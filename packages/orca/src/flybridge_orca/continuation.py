"""Local Linux process evidence for adoption without terminal input."""

from __future__ import annotations

import os
from pathlib import Path


def inspect_process(
    worktree_id: str,
    worktree_path: str,
    old_terminal: str,
    terminal: str,
    session: str,
    agent_pid: int,
    *,
    proc: Path = Path("/proc"),
) -> str:
    """Return a boot/start identity only for one exact local Codex resume actor.

    Read only selected ownership environment keys; never return process secrets.
    Inspection failures are uncertainty, not evidence that an old actor exited.
    """
    target = proc / str(agent_pid)
    if target.stat().st_uid != os.getuid():
        raise ValueError("continuation process belongs to a different OS user")
    boot = (proc / "sys/kernel/random/boot_id").read_text().strip()
    if not boot:
        raise ValueError("local process boot identity is unavailable")
    matches = []
    started = None
    for directory in proc.iterdir():
        if not directory.name.isdigit():
            continue
        try:
            if directory.stat().st_uid != os.getuid():
                continue
            raw = (directory / "cmdline").read_bytes()
        except FileNotFoundError:
            if int(directory.name) == agent_pid:
                raise
            continue
        argv = [arg.decode() for arg in raw.split(b"\0") if arg]
        if not argv or Path(argv[0]).name != "codex":
            continue
        try:
            fields = (directory / "stat").read_text().rsplit(") ", 1)[1].split()
            environment = dict(
                entry.split(b"=", 1)
                for entry in (directory / "environ").read_bytes().split(b"\0")
                if b"=" in entry
            )
        except FileNotFoundError:
            # A disappearing process is rechecked by the caller; the target must survive.
            continue
        handle = environment.get(b"ORCA_TERMINAL_HANDLE", b"").decode()
        if handle == old_terminal and fields[0] != "Z":
            raise ValueError("previous actor process is still alive")
        if session in argv:
            matches.append(int(directory.name))
        if int(directory.name) != agent_pid:
            continue
        if fields[0] == "Z" or argv.count(session) != 1 or "resume" not in argv:
            raise ValueError("continuation is not a live exact Codex resume session")
        if argv[argv.index("resume") + 1] != session:
            raise ValueError("continuation resume UUID differs")
        if (
            handle != terminal
            or environment.get(b"ORCA_WORKTREE_ID", b"").decode() != worktree_id
            or (directory / "cwd").resolve() != Path(worktree_path).resolve()
            or (directory / "exe").resolve().name != "codex"
        ):
            raise ValueError("continuation process terminal/worktree/executable identity differs")
        started = boot + ":" + fields[19]
    if matches != [agent_pid] or started is None:
        raise ValueError("continuation session/process is absent or ambiguous")
    return started
