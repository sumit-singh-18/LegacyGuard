"""Apply an Edit or Write in memory (FR-12 step 3)."""
from __future__ import annotations

from pathlib import Path


def read_current(path: str) -> str | None:
    try:
        return Path(path).read_text()
    except (OSError, UnicodeDecodeError):
        return None


def apply(tool_name: str, tool_input: dict, current: str | None) -> str | None:
    """The file's text after the tool would run, or None if the tool would fail on its own."""
    if tool_name == "Write":
        return tool_input.get("content", "")
    if tool_name == "Edit":
        if current is None:
            return None
        old, new = tool_input.get("old_string", ""), tool_input.get("new_string", "")
        count = current.count(old) if old else 0
        if count == 0 or (count > 1 and not tool_input.get("replace_all")):
            return None
        return current.replace(old, new) if tool_input.get("replace_all") else current.replace(old, new, 1)
    return None
