#!/usr/bin/env python3
"""Wire claude-compass into Claude Code's settings.json (idempotent, backed up).

  python3 install.py            # user-level  (~/.claude/settings.json)
  python3 install.py --project  # project-level (./.claude/settings.json)
  python3 install.py --impl go  # wire the Go binary instead of the Python script
  python3 install.py --uninstall # remove compass hook entries

Registers ONE command under PreToolUse (matcher "*") and Stop, both calling the
chosen implementation (--impl python|go; both read the same compass.toml and
pass the same conformance suite — python is the reference, go the fast path).
Existing hooks are preserved; compass entries are de-duplicated. A .bak copy of
the prior settings is written before any change.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# How we recognise our own entries, per implementation.
MARKS = {"python": "claude_compass.py", "go": "go/compass"}


def hook_cmd(impl: str) -> str:
    if impl == "go":
        # The Go binary resolves compass.toml next to the *executable* (go/),
        # but the toml lives at repo root — pin it explicitly so a Go install
        # can never silently run with everything off.
        return f'COMPASS_CONFIG="{HERE / "compass.toml"}" "{HERE / "go" / "compass"}"'
    return f'python3 "{HERE / "claude_compass.py"}"'


def _entry(impl: str) -> dict:
    return {"type": "command", "command": hook_cmd(impl)}


def _impl_of(cmd: str) -> str | None:
    for impl, mark in MARKS.items():
        if mark in cmd:
            return impl
    return None


def _compass_impls(group_list: list) -> set:
    found = set()
    for grp in group_list:
        for h in grp.get("hooks", []):
            impl = _impl_of(str(h.get("command", "")))
            if impl:
                found.add(impl)
    return found


def _strip_compass(group_list: list) -> list:
    out = []
    for grp in group_list:
        grp = dict(grp)
        grp["hooks"] = [h for h in grp.get("hooks", []) if _impl_of(str(h.get("command", ""))) is None]
        if grp["hooks"]:
            out.append(grp)
    return out


def install(settings_path: Path, uninstall: bool = False, impl: str = "python") -> None:
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
        present = _compass_impls(group_list)
        if not uninstall and impl in present:
            # A compass entry for this impl already exists — leave it exactly
            # as the user wired it. Re-registering here used to clobber
            # customizations (e.g. a COMPASS_CONFIG=... prefix), which once
            # silently disarmed a hardened install. Idempotent means hands off.
            print(f"  {event}: compass ({impl}) entry already present — left untouched.")
            continue
        group_list[:] = _strip_compass(group_list)  # always clean first
        if not uninstall:
            if present and impl not in present:
                # explicit switch: --impl replaces the other implementation's
                # entry (custom prefixes on the old entry do not carry over).
                print(f"  {event}: switched compass impl {'/'.join(sorted(present))} → {impl}.")
            grp = {"hooks": [_entry(impl)]}
            if matcher is not None:
                grp = {"matcher": matcher, "hooks": [_entry(impl)]}
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
    ap.add_argument("--impl", choices=("python", "go"), default="python",
                    help="which implementation to wire (default: python)")
    args = ap.parse_args()
    if args.impl == "go" and not args.uninstall and not (HERE / "go" / "compass").exists():
        sys.exit("✗ go/compass binary not found — build it first: cd go && go build -o compass .")
    target = (Path.cwd() / ".claude" if args.project else Path.home() / ".claude") / "settings.json"
    install(target, uninstall=args.uninstall, impl=args.impl)


if __name__ == "__main__":
    main()
