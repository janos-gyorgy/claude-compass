#!/usr/bin/env python3
"""claude-compass — a personal, deterministic principle-guard for Claude Code.

It is a single Claude Code *hook*. On each hook event it reads the event JSON on
stdin, checks it against ``compass.toml`` (where **every rule ships OFF**), and
then either:

  * **blocks** a dangerous action (``permissionDecision: "deny"``),
  * **warns** you without blocking (``systemMessage``), or
  * stays completely silent (the default for everything, until you opt in).

Design contract:
  * **Zero dependencies, zero network.** Pure stdlib. Your session never leaves
    the machine — this is the deterministic, sovereign half of the idea on
    purpose (no LLM judge, no API).
  * **Fail-open.** Any error → no-op, exit 0. A guard must never brick a session.
  * **Off by default.** A fresh install changes nothing until you flip a toggle.

Wire it up once (see install.py / README) and it runs on PreToolUse + Stop.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
from pathlib import Path

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore

HERE = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    """Load compass.toml. Missing/broken config → {} (everything stays off)."""
    path = os.environ.get("COMPASS_CONFIG") or str(HERE / "compass.toml")
    p = Path(path)
    if tomllib is None or not p.exists():
        return {}
    try:
        with p.open("rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# emit helpers (the Claude Code hook output contract)
# --------------------------------------------------------------------------- #
def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def deny_pretool(reason: str) -> None:
    _emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"compass: {reason}",
            }
        }
    )


def warn_user(reason: str) -> None:
    # systemMessage is shown to YOU in the transcript; Claude does not see it.
    _emit({"systemMessage": f"compass ⚠ {reason}"})


def block_stop(reason: str) -> None:
    # On Stop, decision:block feeds the reason back and makes Claude continue
    # (i.e. revise). Used only when a soft rule's action is set to "block".
    _emit({"decision": "block", "reason": f"compass: {reason}"})


def act(action: str, reason: str, *, on: str) -> None:
    """Apply a rule's configured action ('block' | 'warn')."""
    if action == "warn":
        warn_user(reason)
    elif action == "block":
        if on == "pretool":
            deny_pretool(reason)
        elif on == "stop":
            block_stop(reason)


# --------------------------------------------------------------------------- #
# rule checks  (each returns a human reason string on a hit, else "")
# --------------------------------------------------------------------------- #
_RM = re.compile(r"\brm\s+(?:-\S*[rf]\S*|--recursive|--force)")
_DISK = re.compile(r"\bdd\b[^\n]*\bof=/dev/|\bmkfs\b|>\s*/dev/sd|>\s*/dev/nvme")
_CURL_SH = re.compile(r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba|z|d)?sh\b")
_CHMOD777 = re.compile(r"\bchmod\s+(?:-R\s+)?0?777\b")
_FORKBOMB = re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;:")


def check_dangerous(tool: str, tinput: dict, g: dict) -> str:
    """Dangerous shell commands + edits to secret files."""
    if tool == "Bash":
        cmd = str(tinput.get("command", ""))
        if g.get("rm_rf", True) and _RM.search(cmd):
            return f"destructive rm blocked → {cmd.strip()[:120]}"
        if g.get("disk_destroyers", True) and _DISK.search(cmd):
            return f"disk-destroying command blocked → {cmd.strip()[:120]}"
        if g.get("curl_pipe_shell", True) and _CURL_SH.search(cmd):
            return f"curl|sh pipe-to-shell blocked → {cmd.strip()[:120]}"
        if g.get("chmod_777", True) and _CHMOD777.search(cmd):
            return f"chmod 777 blocked → {cmd.strip()[:120]}"
        if _FORKBOMB.search(cmd):
            return "fork bomb blocked"
        for pat in g.get("extra_command_patterns", []) or []:
            try:
                if re.search(pat, cmd):
                    return f"matched extra_command_pattern /{pat}/ → {cmd.strip()[:100]}"
            except re.error:
                continue
        return ""

    # edits to secret files
    if tool in ("Edit", "Write", "MultiEdit") and g.get("secret_file_edits", True):
        fp = str(tinput.get("file_path", "") or tinput.get("path", ""))
        if fp:
            name = os.path.basename(fp)
            for pat in g.get("secret_path_globs", []) or []:
                if fnmatch.fnmatch(fp, pat) or fnmatch.fnmatch(name, pat):
                    return f"edit to secret file blocked → {fp}"
    return ""


def check_git(cmd: str, g: dict) -> str:
    """git push to a protected branch / force-push."""
    if "git push" not in cmd:
        return ""
    if g.get("force_push", True) and re.search(r"--force(?:-with-lease)?\b|\s-f\b", cmd):
        return f"force-push blocked → {cmd.strip()[:120]}"
    if g.get("push_to_protected", True):
        protected = g.get("protected_branches", ["main", "master"]) or []
        branch_alt = "|".join(re.escape(b) for b in protected)
        if branch_alt and re.search(rf"git push\s+\S+\s+(?:{branch_alt})\b", cmd):
            return f"push to protected branch blocked → {cmd.strip()[:120]}"
    return ""


_SUPER = re.compile(
    r"\b(amazing|incredible|fantastic|excellent|perfect|brilliant|wonderful|"
    r"awesome|superb|stellar|exceptional|flawless|phenomenal)\b",
    re.I,
)
_DEFAULT_SYC = [
    "great question",
    "you're absolutely right",
    "you are absolutely right",
    "i'm thrilled",
    "i am thrilled",
    "happy to help",
    "what a great",
    "excellent question",
    "that's a fantastic",
]
_CLOSER = re.compile(
    r"(happy to help|always here|let me know if you|feel free to|great work|"
    r"you've got this|excited to|i'm here to help)",
    re.I,
)


def check_sycophancy(text: str, g: dict) -> str:
    low = text.lower()
    found = [p for p in (g.get("phrases") or _DEFAULT_SYC) if p.lower() in low]
    if found:
        return "flattery phrase(s): " + ", ".join(found[:3])
    if g.get("flag_superlative_pileups", True):
        n = len(_SUPER.findall(text))
        if n >= int(g.get("superlative_threshold", 3)):
            return f"superlative pile-up ({n} in one message)"
    if g.get("flag_gushing_closers", True):
        tail = text.strip().splitlines()[-1] if text.strip() else ""
        if _CLOSER.search(tail):
            return "gushing closer"
    return ""


_DEFAULT_EXPANSION = [
    "while i was at it",
    "went ahead and also",
    "took the liberty",
    "as a bonus",
    "i also added",
    "also refactored",
    "additionally, i",
    "i also went ahead",
    "for good measure",
]


def check_scope_drift(text: str, g: dict) -> str:
    """Deterministic *proxy* for unrequested scope expansion — NOT true intent
    drift (that needs the optional LLM judge). Flags language that signals the
    agent did more than asked."""
    low = text.lower()
    found = [p for p in (g.get("expansion_phrases") or _DEFAULT_EXPANSION) if p in low]
    if found:
        return "unrequested scope-expansion language: " + ", ".join(found[:3])
    return ""


# --------------------------------------------------------------------------- #
# transcript reading (for Stop)
# --------------------------------------------------------------------------- #
def _block_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text", "")))
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(parts)
    return ""


def last_assistant_text(transcript_path: str) -> str:
    if not transcript_path:
        return ""
    p = Path(transcript_path)
    if not p.exists():
        return ""
    last = ""
    try:
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
                role = msg.get("role") or obj.get("type")
                if role == "assistant":
                    txt = _block_text(msg.get("content"))
                    if txt.strip():
                        last = txt
    except Exception:
        return ""
    return last


# --------------------------------------------------------------------------- #
# event handlers
# --------------------------------------------------------------------------- #
def handle_pretool(ev: dict, cfg: dict) -> None:
    tool = ev.get("tool_name", "")
    tinput = ev.get("tool_input", {}) or {}

    g = cfg.get("dangerous_tools", {})
    if g.get("enabled"):
        hit = check_dangerous(tool, tinput, g)
        if hit:
            return act(g.get("action", "block"), hit, on="pretool")

    g = cfg.get("git_safety", {})
    if g.get("enabled") and tool == "Bash":
        hit = check_git(str(tinput.get("command", "")), g)
        if hit:
            return act(g.get("action", "block"), hit, on="pretool")


def handle_stop(ev: dict, cfg: dict) -> None:
    syc = cfg.get("sycophancy", {})
    drift = cfg.get("scope_drift", {})
    if not (syc.get("enabled") or drift.get("enabled")):
        return
    text = last_assistant_text(ev.get("transcript_path", ""))
    if not text:
        return
    if syc.get("enabled"):
        hit = check_sycophancy(text, syc)
        if hit:
            return act(syc.get("action", "warn"), hit, on="stop")
    if drift.get("enabled"):
        hit = check_scope_drift(text, drift)
        if hit:
            return act(drift.get("action", "warn"), hit, on="stop")


def main() -> int:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        ev = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0
    cfg = load_config()
    if not cfg or not isinstance(ev, dict):
        return 0
    name = ev.get("hook_event_name", "")
    try:
        if name == "PreToolUse":
            handle_pretool(ev, cfg)
        elif name in ("Stop", "SubagentStop"):
            handle_stop(ev, cfg)
    except Exception:
        return 0  # fail-open: never break the session
    return 0


if __name__ == "__main__":
    sys.exit(main())
