#!/usr/bin/env python3
"""Wire claude-compass into Claude Code's settings.json (idempotent, backed up).

  python3 install.py            # user-level  (~/.claude/settings.json)
  python3 install.py --project  # project-level (./.claude/settings.json)
  python3 install.py --uninstall # remove compass hook entries

Registers ONE command under PreToolUse (matcher "*") and Stop, both calling this
repo's claude_compass.py. Existing hooks are preserved; compass entries are
de-duplicated. A .bak copy of the prior settings is written before any change.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = f'python3 "{HERE / "claude_compass.py"}"'
MARK = "claude_compass.py"  # how we recognise our own entries


def _entry() -> dict:
    return {"type": "command", "command": HOOK}


def _has_compass(group_list: list) -> bool:
    for grp in group_list:
        for h in grp.get("hooks", []):
            if MARK in str(h.get("command", "")):
                return True
    return False


def _strip_compass(group_list: list) -> list:
    out = []
    for grp in group_list:
        grp = dict(grp)
        grp["hooks"] = [h for h in grp.get("hooks", []) if MARK not in str(h.get("command", ""))]
        if grp["hooks"]:
            out.append(grp)
    return out


def install(settings_path: Path, uninstall: bool = False) -> None:
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        shutil.copy2(settings_path, settings_path.with_suffix(settings_path.suffix + ".bak"))
        try:
            data = json.loads(settings_path.read_text() or "{}")
        except json.JSONDecodeError:
            sys.exit(f"✗ {settings_path} is not valid JSON — fix or move it first.")
    else:
        data = {}

    hooks = data.setdefault("hooks", {})

    for event, matcher in (("PreToolUse", "*"), ("Stop", None)):
        group_list = hooks.setdefault(event, [])
        group_list[:] = _strip_compass(group_list)  # always clean first
        if not uninstall:
            grp = {"hooks": [_entry()]}
            if matcher is not None:
                grp = {"matcher": matcher, "hooks": [_entry()]}
            group_list.append(grp)
        if not group_list:
            hooks.pop(event, None)

    settings_path.write_text(json.dumps(data, indent=2) + "\n")
    verb = "Removed" if uninstall else "Installed"
    print(f"✓ {verb} claude-compass hooks in {settings_path}")
    if not uninstall:
        print("  Rules are all OFF. Edit compass.toml and flip `enabled = true` to arm them.")


def main() -> None:
    ap = argparse.ArgumentParser(description="install claude-compass hooks")
    ap.add_argument("--project", action="store_true", help="install to ./.claude (not ~/.claude)")
    ap.add_argument("--uninstall", action="store_true", help="remove compass hook entries")
    args = ap.parse_args()
    target = (Path.cwd() / ".claude" if args.project else Path.home() / ".claude") / "settings.json"
    install(target, uninstall=args.uninstall)


if __name__ == "__main__":
    main()
