"""Shared argparse conventions for Flybridge command-line entry points."""

from __future__ import annotations

import argparse


class HelpFormatter(argparse.HelpFormatter):
    """Render option headings as Options and align help text in one column."""

    def __init__(
        self,
        prog: str,
        indent_increment: int = 2,
        max_help_position: int = 36,
        width: int | None = None,
    ) -> None:
        super().__init__(
            prog,
            indent_increment=indent_increment,
            max_help_position=max_help_position,
            width=width,
        )

    def start_section(self, heading: str | None) -> None:
        super().start_section("Options" if heading == "options" else heading)

    def _format_action_invocation(self, action: argparse.Action) -> str:
        if not action.option_strings:
            return super()._format_action_invocation(action)
        if action.nargs == 0:
            return ", ".join(action.option_strings)
        metavar = self._format_args(action, self._get_default_metavar_for_optional(action))
        return ", ".join(f"{option} {metavar}" for option in action.option_strings)


class ArgumentParser(argparse.ArgumentParser):
    """Argument parser with Flybridge's stable help presentation."""

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("formatter_class", HelpFormatter)
        super().__init__(*args, **kwargs)
