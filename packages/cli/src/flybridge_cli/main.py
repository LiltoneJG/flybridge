from __future__ import annotations

import sqlite3
import sys

from flybridge_core import ConfigError

from .commands import auxiliary, inventory, queue, reconcile, workflow
from .parser import build_parser


def main(argv: list[str] | None = None) -> int:
    """Parse the public CLI and translate expected operational failures."""
    args = build_parser().parse_args(argv)
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
