#!/usr/bin/env python3
"""Fail-closed PreToolUse guard blocking Git access from Claude Code."""

from __future__ import annotations

import json
import re
import sys
from typing import Any

DENY_MESSAGE = "Git interaction is forbidden for Claude Code in this project."
GIT_TOOL = re.compile(r"(?:^|[-_])(?:git|github|gitlab|bitbucket)(?:$|[-_])", re.IGNORECASE)
GIT_EXECUTABLE = re.compile(r"(?<![\w.-])(?:/[\w./-]+/)?(?:git|gh)(?![\w.-])", re.IGNORECASE)
GIT_METADATA = re.compile(r"(?<![\w.-])\.git(?:/|$)")
GIT_REMOTE = re.compile(
    r"(?:git://|git@|ssh://git@?|https?://(?:api\.|raw\.)?github\.com|https?://\S+?\.git(?:[/?#]|$))",
    re.IGNORECASE,
)
GIT_LIBRARY = re.compile(r"(?<!\w)(?:pygit2|dulwich|libgit2)(?!\w)", re.IGNORECASE)
COMMAND_KEYS = {
    "args",
    "arguments",
    "cmd",
    "command",
    "command_line",
    "commandline",
    "input",
    "prompt",
    "script",
    "url",
}
PATH_KEYS = {"cwd", "directory", "file_path", "path"}


def _strings_for_keys(value: Any, keys: set[str]) -> list[str]:
    if isinstance(value, dict):
        return [item for key, child in value.items() if key in keys for item in _strings_for_keys(child, keys)]
    if isinstance(value, list):
        return [item for child in value for item in _strings_for_keys(child, keys)]
    if isinstance(value, str):
        return [value]
    return []


def evaluate(tool_name: str, tool_input: Any) -> str | None:
    if not tool_name or GIT_TOOL.search(tool_name):
        return DENY_MESSAGE
    if tool_name == "Bash" and not isinstance(tool_input, dict):
        return DENY_MESSAGE
    if not isinstance(tool_input, dict):
        return None
    for value in _strings_for_keys(tool_input, PATH_KEYS):
        if GIT_METADATA.search(value):
            return DENY_MESSAGE
    for value in _strings_for_keys(tool_input, COMMAND_KEYS):
        if GIT_EXECUTABLE.search(value) or GIT_REMOTE.search(value) or GIT_LIBRARY.search(value):
            return DENY_MESSAGE
    if tool_name == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str):
            return DENY_MESSAGE
        if GIT_METADATA.search(command):
            return DENY_MESSAGE
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("hook payload must be an object")
        reason = evaluate(str(payload.get("tool_name") or ""), payload.get("tool_input", {}))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError):
        reason = DENY_MESSAGE
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
