#!/usr/bin/env python3
"""Install, inspect or remove the local-rag hooks in a Claude Code settings file.

Hooks are the only mechanism by which retrieval can happen without the model
choosing to retrieve. That distinction is the whole economic argument for them:
a tool call costs output tokens to emit and a round trip to answer, and it only
happens when the model already suspects the memory exists. A hook runs first,
costs the model nothing to trigger, and delivers its result as prompt - the
cheap kind of token.

What this script must get right is smaller and more boring: settings.json is a
file the user owns and other things write to. So it

  * never rewrites the file it did not first parse,
  * backs up before every write,
  * merges into existing hook arrays instead of replacing them,
  * identifies its own entries by the absolute script path, so re-running
    updates them in place rather than stacking a second copy,
  * and can take all of it back out again.

Usage:
    python3 hooks/install.py --dry-run          # show what would change
    python3 hooks/install.py                    # install into ~/.claude/settings.json
    python3 hooks/install.py --with-harvest     # also install the Stop harvester
    python3 hooks/install.py --uninstall
    python3 hooks/install.py --settings .claude/settings.json   # project scope
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
PYTHON = sys.executable or "/usr/bin/python3"

#: event -> (script, timeout seconds, extra hook fields)
HOOK_PLAN: dict[str, tuple[str, int, dict]] = {
    "SessionStart": (
        "rag_session_start.py",
        8,
        {"statusMessage": "RAG sharing disabled for cloud isolation"},
    ),
    "UserPromptSubmit": (
        "rag_user_prompt.py",
        5,
        {"statusMessage": "RAG sharing disabled for cloud isolation"},
    ),
}

#: Installed only with --with-harvest. `async` matters here and nowhere else:
#: the harvester may call a model, and the end of a turn must not wait for it.
HARVEST_PLAN = {
    "Stop": ("rag_harvest.py", 300, {"async": True}),
}


def hook_command(script: str) -> str:
    return f"{PYTHON} {HOOKS_DIR / script}"


def is_ours(entry: dict) -> bool:
    """Recognize our own entries by path, not by position or by a comment.

    A marker comment would be lost the first time anything else rewrote the
    file; the path is what actually makes the entry ours.
    """
    command = str(entry.get("command") or "")
    return str(HOOKS_DIR) in command


def merge_event(settings: dict, event: str, script: str, timeout: int, extra: dict) -> str:
    hooks = settings.setdefault("hooks", {})
    groups = hooks.setdefault(event, [])
    if not isinstance(groups, list):
        raise SystemExit(f"settings.hooks.{event} is not a list; refusing to touch it")

    entry = {"type": "command", "command": hook_command(script), "timeout": timeout}
    entry.update(extra)

    for group in groups:
        if not isinstance(group, dict):
            continue
        entries = group.get("hooks")
        if not isinstance(entries, list):
            continue
        for index, existing in enumerate(entries):
            if isinstance(existing, dict) and is_ours(existing):
                if existing == entry:
                    return "unchanged"
                entries[index] = entry
                return "updated"

    groups.append({"hooks": [entry]})
    return "added"


def remove_event(settings: dict, event: str) -> int:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    groups = hooks.get(event)
    if not isinstance(groups, list):
        return 0
    removed = 0
    surviving_groups = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            surviving_groups.append(group)
            continue
        kept = [e for e in group["hooks"] if not (isinstance(e, dict) and is_ours(e))]
        removed += len(group["hooks"]) - len(kept)
        if kept:
            group["hooks"] = kept
            surviving_groups.append(group)
    if surviving_groups:
        hooks[event] = surviving_groups
    else:
        hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks", None)
    return removed


def load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"{path} is not valid JSON ({exc}). A malformed settings file disables "
            "every setting in it, so fix that first - this script will not rewrite it."
        )
    if not isinstance(data, dict):
        raise SystemExit(f"{path} does not contain a JSON object")
    return data


def write_settings(path: Path, settings: dict) -> Path | None:
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d_%H%M%S')}")
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
    json.loads(payload)  # never leave behind a file we cannot read back
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, path)
    return backup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--settings",
        default=str(Path.home() / ".claude" / "settings.json"),
        help="settings file to modify (default: user-scope ~/.claude/settings.json)",
    )
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--with-harvest",
        action="store_true",
        help="also install the Stop harvester (itself still gated by RAG_HOOK_HARVEST=1)",
    )
    args = parser.parse_args()

    path = Path(args.settings).expanduser()
    settings = load_settings(path)

    plan = dict(HOOK_PLAN)
    if args.with_harvest:
        plan.update(HARVEST_PLAN)

    if args.uninstall:
        removed = sum(
            remove_event(settings, event) for event in (*HOOK_PLAN, *HARVEST_PLAN)
        )
        print(f"removing {removed} hook entr{'y' if removed == 1 else 'ies'} from {path}")
    else:
        missing = [s for s, *_ in plan.values() if not (HOOKS_DIR / s).is_file()]
        if missing:
            raise SystemExit(f"missing hook scripts: {', '.join(missing)}")
        for event, (script, timeout, extra) in plan.items():
            action = merge_event(settings, event, script, timeout, extra)
            print(f"{action:<9} {event:<17} {script}")
        if not args.with_harvest:
            removed = sum(remove_event(settings, event) for event in HARVEST_PLAN)
            if removed:
                print("removed    Stop              rag_harvest.py (no --with-harvest)")

    if args.dry_run:
        print("\n--- resulting hooks section (not written) ---")
        print(json.dumps(settings.get("hooks", {}), indent=2, ensure_ascii=False))
        return 0

    backup = write_settings(path, settings)
    print(f"\nwrote {path}" + (f" (backup: {backup.name})" if backup else ""))
    print(
        "Hooks are read at session start: open a new session for them to take effect.\n"
        "Disable at any time with RAG_HOOK_DISABLE=1, or run this script with --uninstall."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
