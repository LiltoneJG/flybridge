#!/usr/bin/env python3
"""Format GitHub-Flavored Markdown tables with display-width-aware padding."""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

from _bootstrap import activate_project_environment

activate_project_environment()

from flybridge_core import ArgumentParser

DELIMITER = re.compile(r"^:?-+:?$")


def _is_double_width(character: str) -> bool:
    codepoint = ord(character)
    return (
        unicodedata.east_asian_width(character) in {"F", "W"}
        or 0x1F000 <= codepoint <= 0x1FAFF
        or 0x2600 <= codepoint <= 0x27BF
    )


def display_width(text: str) -> int:
    """Return the usual monospace width, including CJK and emoji cells."""
    width = 0
    cluster_width = 0
    join_next = False
    regional_indicators = 0
    for character in text:
        codepoint = ord(character)
        if 0x1F3FB <= codepoint <= 0x1F3FF:
            continue
        if character == "\u200d":
            join_next = True
            continue
        if (
            codepoint in range(0xFE00, 0xFE10)
            or codepoint == 0x20E3
            or unicodedata.combining(character)
        ):
            if codepoint in {0xFE0F, 0x20E3}:
                cluster_width = max(cluster_width, 2)
            continue
        if 0x1F1E6 <= codepoint <= 0x1F1FF:
            if regional_indicators == 0:
                width += cluster_width
                cluster_width = 2
                regional_indicators = 1
            else:
                width += max(cluster_width, 2)
                cluster_width = 0
                regional_indicators = 0
            continue
        character_width = 2 if _is_double_width(character) else 1
        if join_next:
            cluster_width = max(cluster_width, character_width)
            join_next = False
        else:
            width += cluster_width
            cluster_width = character_width
        regional_indicators = 0
    return width + cluster_width


def _split_row(line: str) -> tuple[str, list[str]] | None:
    stripped = line.lstrip(" \t")
    indent = line[: len(line) - len(stripped)]
    row = stripped.strip()
    if "|" not in row:
        return None
    row = row.removeprefix("|")
    if row.endswith("|") and not row.endswith(r"\|"):
        row = row[:-1]

    cells: list[str] = []
    cell: list[str] = []
    escaped = False
    for character in row:
        if character == "|" and not escaped:
            cells.append("".join(cell).strip())
            cell = []
        else:
            cell.append(character)
        if character == "\\" and not escaped:
            escaped = True
        else:
            escaped = False
    cells.append("".join(cell).strip())
    return indent, cells


def _alignment(cell: str) -> str | None:
    if not DELIMITER.fullmatch(cell):
        return None
    if cell.startswith(":") and cell.endswith(":"):
        return "center"
    if cell.endswith(":"):
        return "right"
    if cell.startswith(":"):
        return "left"
    return "none"


def _pad(cell: str, width: int, alignment: str) -> str:
    padding = width - display_width(cell)
    if alignment == "right":
        return " " * padding + cell
    if alignment == "center":
        left = padding // 2
        return " " * left + cell + " " * (padding - left)
    return cell + " " * padding


def _format_table(lines: list[str]) -> list[str] | None:
    parsed = [_split_row(line) for line in lines]
    if any(row is None for row in parsed):
        return None
    rows = [row for row in parsed if row is not None]
    column_count = len(rows[1][1])
    if column_count == 0 or any(len(cells) != column_count for _, cells in rows):
        return None
    alignments = [_alignment(cell) for cell in rows[1][1]]
    if any(alignment is None for alignment in alignments):
        return None

    widths = [0] * column_count
    for row_index, (_, cells) in enumerate(rows):
        if row_index == 1:
            continue
        for column, cell in enumerate(cells):
            widths[column] = max(widths[column], display_width(cell))
    for column, alignment in enumerate(alignments):
        minimum = 5 if alignment == "center" else 4 if alignment in {"left", "right"} else 3
        widths[column] = max(widths[column], minimum)

    delimiter_cells: list[str] = []
    for width, alignment in zip(widths, alignments, strict=True):
        if alignment == "center":
            delimiter_cells.append(":" + "-" * (width - 2) + ":")
        elif alignment == "left":
            delimiter_cells.append(":" + "-" * (width - 1))
        elif alignment == "right":
            delimiter_cells.append("-" * (width - 1) + ":")
        else:
            delimiter_cells.append("-" * width)

    result: list[str] = []
    indent = rows[0][0]
    for row_index, (_, cells) in enumerate(rows):
        if row_index == 1:
            formatted = delimiter_cells
        else:
            formatted = [
                _pad(cell, widths[column], alignments[column] or "none")
                for column, cell in enumerate(cells)
            ]
        result.append(f"{indent}| " + " | ".join(formatted) + " |")
    return result


def format_markdown(text: str) -> str:
    newline = "\r\n" if "\r\n" in text else "\n"
    trailing_newline = text.endswith(("\n", "\r"))
    lines = text.splitlines()
    output: list[str] = []
    index = 0
    in_fence = False
    fence = ""
    while index < len(lines):
        stripped = lines[index].lstrip()
        fence_match = re.match(r"(`{3,}|~{3,})", stripped)
        if fence_match:
            marker = fence_match.group(1)
            if not in_fence:
                in_fence = True
                fence = marker[0]
            elif marker[0] == fence:
                in_fence = False
            output.append(lines[index])
            index += 1
            continue
        if not in_fence and index + 1 < len(lines):
            header = _split_row(lines[index])
            delimiter = _split_row(lines[index + 1])
            if header and delimiter and all(_alignment(cell) for cell in delimiter[1]):
                end = index + 2
                while end < len(lines) and _split_row(lines[end]):
                    end += 1
                formatted = _format_table(lines[index:end])
                if formatted is not None:
                    output.extend(formatted)
                    index = end
                    continue
        output.append(lines[index])
        index += 1
    result = newline.join(output)
    return result + newline if trailing_newline else result


def main(arguments: list[str]) -> int:
    parser = ArgumentParser(prog="format_markdown_tables.py", description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="Markdown files to format")
    options = parser.parse_args(arguments)
    changed = False
    for path in options.paths:
        original = path.read_text(encoding="utf-8")
        formatted = format_markdown(original)
        if formatted != original:
            path.write_text(formatted, encoding="utf-8", newline="")
            print(f"formatted Markdown tables in {path}")
            changed = True
    return int(changed)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
