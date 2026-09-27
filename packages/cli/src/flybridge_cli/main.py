from __future__ import annotations

import sqlite3
import sys

from flybridge_core import ConfigError

from .commands import auxiliary, inventory, queue, reconcile, workflow
from .parser import build_parser


def main(argv: list[str] | None = None) -> int:
    """Parse the public CLI and translate expected operational failures."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    separator = arguments.index("--") if "--" in arguments else -1
    is_queue_run = any(
        arguments[index : index + 2] == ["queue", "run"] for index in range(max(separator - 1, 0))
    )
    if separator >= 0 and is_queue_run:
        args = parser.parse_args(arguments[:separator])
        args.argv = arguments[separator + 1 :]
    else:
        args = parser.parse_args(arguments)
    handlers = {
        "queue": queue.handle,
        "workflow": workflow.handle,
        "board": auxiliary.handle_board,
        "prs": auxiliary.handle_prs,
        "operator": auxiliary.handle_operator,
        "inventory": inventory.handle_inventory,
        "doctor": auxiliary.handle_doctor,
        "reconcile": reconcile.handle_reconcile,
        "worktree": reconcile.handle_worktree,
    }
    try:
        return handlers[args.command](args)
    except (ConfigError, OSError, TypeError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f"flybridge: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
