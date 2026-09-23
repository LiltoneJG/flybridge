from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _formatter_module():
    script = Path(__file__).parents[1] / "scripts" / "format_markdown_tables.py"
    spec = importlib.util.spec_from_file_location("format_markdown_tables", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(script.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def test_formats_ascii_and_japanese_by_display_width() -> None:
    module = _formatter_module()

    source = """| Language | Description |
| --- | --- |
| English | text |
| 日本語 | 説明 |
"""

    assert (
        module.format_markdown(source)
        == """| Language | Description |
| -------- | ----------- |
| English  | text        |
| 日本語   | 説明        |
"""
    )


def test_treats_emoji_grapheme_clusters_as_double_width() -> None:
    module = _formatter_module()

    assert module.display_width("😀") == 2
    assert module.display_width("👩‍💻") == 2
    assert module.display_width("👍🏽") == 2
    assert module.display_width("🇯🇵") == 2
    assert module.display_width("1️⃣") == 2

    source = """| Symbol | Description |
| --- | --- |
| 😀 | face |
| 👩‍💻 | developer |
"""
    assert (
        module.format_markdown(source)
        == """| Symbol | Description |
| ------ | ----------- |
| 😀     | face        |
| 👩‍💻     | developer   |
"""
    )


def test_preserves_column_alignment_and_escaped_pipes() -> None:
    module = _formatter_module()

    source = """| Left | Center | Right |
| :--- | :---: | ---: |
| a\\|b | 日本 | 1 |
"""

    assert (
        module.format_markdown(source)
        == """| Left | Center | Right |
| :--- | :----: | ----: |
| a\\|b |  日本  |     1 |
"""
    )


def test_does_not_format_tables_in_fenced_code_blocks() -> None:
    module = _formatter_module()
    source = """```markdown
| A | B |
| --- | --- |
| x | y |
```
"""

    assert module.format_markdown(source) == source


def test_formatter_is_idempotent() -> None:
    module = _formatter_module()
    source = """| 項目 | Value |
| ---- | ----- |
| 名前 | Flybridge |
"""

    formatted = module.format_markdown(source)

    assert module.format_markdown(formatted) == formatted
