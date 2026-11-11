import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "hooks" / "deny_git.py"
spec = importlib.util.spec_from_file_location("deny_git", HOOK)
deny_git = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deny_git)


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "/usr/bin/git status",
        "env git status",
        "bash -lc 'git status'",
        "python3 -c 'import subprocess; subprocess.run([\"git\", \"status\"])'",
        "gh api repos/example/example",
        "curl https://api.github.com/repos/example/example",
        "git ls-remote https://example.com/repository.git",
        "cat .git/config",
        "python3 -c 'import dulwich'",
    ],
)
def test_git_command_variants_are_denied(command):
    assert deny_git.evaluate("Bash", {"command": command}) == deny_git.DENY_MESSAGE


@pytest.mark.parametrize(
    "command",
    [
        "python3 -m pytest",
        "cat skills/git-engineering.md",
        "cat .gitignore",
        "printf 'digital delivery'",
        "curl https://example.com/documentation",
    ],
)
def test_non_git_commands_remain_allowed(command):
    assert deny_git.evaluate("Bash", {"command": command}) is None


def test_git_mcp_tools_are_denied():
    assert deny_git.evaluate("mcp__github__create_commit", {}) == deny_git.DENY_MESSAGE
    assert deny_git.evaluate("mcp__local-git__status", {}) == deny_git.DENY_MESSAGE


def test_git_metadata_and_remote_urls_are_denied_for_other_tools():
    assert deny_git.evaluate("Read", {"file_path": ".git/config"}) == deny_git.DENY_MESSAGE
    assert deny_git.evaluate("WebFetch", {"url": "https://github.com/example/repository"}) == deny_git.DENY_MESSAGE


def test_code_search_about_git_remains_allowed():
    assert deny_git.evaluate("Grep", {"pattern": "git", "path": "skills"}) is None


@pytest.mark.parametrize("payload", ["", "not-json", "[]", "{}"])
def test_hook_fails_closed(payload):
    finished = subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload,
        text=True,
        capture_output=True,
        check=False,
    )
    assert finished.returncode == 2
    assert deny_git.DENY_MESSAGE in finished.stderr


def test_project_settings_install_fail_closed_guard():
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings["permissions"]["disableBypassPermissionsMode"] == "disable"
    assert "Bash(git:*)" in settings["permissions"]["deny"]
    assert "Bash(gh:*)" in settings["permissions"]["deny"]
    groups = settings["hooks"]["PreToolUse"]
    commands = [entry["command"] for group in groups for entry in group["hooks"]]
    assert len(commands) == 1
    script = commands[0].split()[-1]
    assert Path(script) == HOOK
    assert HOOK.is_file()
