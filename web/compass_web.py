#!/usr/bin/env python3
"""compass-web: a local web page for claude-compass.

Shows the same things as the terminal monitor (tui/) and edits the same
config, for machines where nobody keeps a terminal open. Python 3.11+,
standard library only, one file: the page, its script and its stylesheet are
served from the strings at the bottom.

    python3 web/compass_web.py [--host 127.0.0.1] [--port 8377] [--check] [--version]

Who the page is protected from: the agent compass guards. It can reach this
port, it decides what text the page shows (a blocked command is logged
verbatim), and it can read every file its own user can read. It cannot read a
0600 file owned by another user, so the edit token lives in one, read once at
start. Reads are open; every write needs the token, and anything that goes
wrong on the write path refuses the write (fail safe, the opposite of the hook,
which fails open).
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import hmac
import ipaddress
import json
import os
import re
import shutil
import socket
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - py < 3.11
    sys.exit("compass-web needs Python 3.11+ (tomllib)")

VERSION = "0.1.0"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8377
MAX_BODY = 64 * 1024
TAIL_CAP = 5 * 1024 * 1024  # read at most the last 5 MB of a log
MIN_TOKEN = 32
MAX_EVENTS = 5000
MAX_NET_TAIL = 200
MAX_DOMAINS = 100_000

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

_CTRL = re.compile(r"[\x00-\x1f\x7f]+")


def clean(s: str) -> str:
    """One line, no control characters (as tui/src/parse.ts does)."""
    return _CTRL.sub(" ", s)


# =========================================================================== #
# Config editor: a port of tui/src/config.ts. Both must agree; the shared
# vectors in tests/conformance/config_vectors.json are asserted against both.
# Line-surgical by design: only a value token is ever rewritten.
# =========================================================================== #
HEADER = re.compile(r"^\s*\[([A-Za-z0-9_]+)\]\s*(?:#.*)?$")
ARRAY_HEADER = re.compile(r"^\s*\[\[([A-Za-z0-9_]+)\]\]\s*(?:#.*)?$")
ENABLED = re.compile(r"^\s*enabled\s*=\s*(true|false)\b")
ACTION = re.compile(r"^\s*action\s*=\s*['\"](block|warn)['\"]")
RULE_KEYS = ["name", "enabled", "on", "pattern", "action", "reason"]
STR_KEYS = ["name", "on", "pattern", "action", "reason"]
# a TOML string token: 'literal' or "basic" (with backslash escapes)
STR_TOKEN = r"""('[^']*'|"(?:[^"\\]|\\.)*")"""
_STR_KEY = {k: re.compile(rf"^\s*{k}\s*=\s*{STR_TOKEN}") for k in STR_KEYS}


class EditError(Exception):
    pass


def config_path() -> Path:
    return Path(os.environ.get("COMPASS_CONFIG") or Path.home() / ".claude" / "compass.toml")


def decode_str_token(tok: str) -> str:
    if tok.startswith("'"):
        return tok[1:-1]
    try:  # TOML basic escapes we ever write are a subset of JSON's
        return json.loads(tok)
    except ValueError:
        return tok[1:-1]


def parse_groups(text: str) -> list[dict]:
    groups: list[dict] = []
    current = None
    for i, line in enumerate(text.split("\n")):
        if ARRAY_HEADER.match(line):
            current = None  # [[custom_rules]] keys never belong to a group
            continue
        h = HEADER.match(line)
        if h:
            current = {"name": h.group(1), "enabled": False, "action": None,
                       "enabledLine": -1, "actionLine": -1}
            continue
        if current is None:
            continue
        e = ENABLED.match(line)
        if e and current["enabledLine"] < 0:
            current["enabled"] = e.group(1) == "true"
            current["enabledLine"] = i
            if current not in groups:
                groups.append(current)
            continue
        a = ACTION.match(line)
        if a and current["actionLine"] < 0:
            current["action"] = a.group(1)
            current["actionLine"] = i
            if current not in groups:
                groups.append(current)
    return [g for g in groups if g["enabledLine"] >= 0]


def set_value(text: str, group: str, key: str, value: str) -> str:
    g = next((x for x in parse_groups(text) if x["name"] == group), None)
    if g is None:
        raise EditError(f"no group [{group}] with an enabled key")
    if key not in ("enabled", "action"):
        raise EditError(f"unknown key {key}")
    line_no = g["enabledLine"] if key == "enabled" else g["actionLine"]
    if line_no < 0:
        raise EditError(f"[{group}] has no {key} key")
    lines = text.split("\n")
    before = lines[line_no]
    if key == "enabled":
        rx, repl = r"(=\s*)(true|false)\b", lambda m: m.group(1) + value
    else:
        rx, repl = r"(=\s*)(['\"])(block|warn)\2", lambda m: m.group(1) + m.group(2) + value + m.group(2)
    if not re.search(rx, before):
        raise EditError(f"could not rewrite {key} on line {line_no + 1}")
    lines[line_no] = re.sub(rx, repl, before, count=1)
    return "\n".join(lines)


def empty_rule(header_line: int = -1) -> dict:
    return {"name": "", "enabled": True, "on": "pretool", "pattern": "", "action": "warn",
            "reason": "", "headerLine": header_line, "lines": {k: -1 for k in RULE_KEYS}}


def parse_custom_rules(text: str) -> list[dict]:
    rules: list[dict] = []
    current = None
    for i, line in enumerate(text.split("\n")):
        ah = ARRAY_HEADER.match(line)
        if ah or HEADER.match(line):
            current = empty_rule(i) if ah and ah.group(1) == "custom_rules" else None
            if current is not None:
                rules.append(current)
            continue
        if current is None:
            continue
        e = ENABLED.match(line)
        if e and current["lines"]["enabled"] < 0:
            current["enabled"] = e.group(1) == "true"
            current["lines"]["enabled"] = i
            continue
        for k in STR_KEYS:
            m = _STR_KEY[k].match(line)
            if m and current["lines"][k] < 0:
                current[k] = decode_str_token(m.group(1))
                current["lines"][k] = i
                break
    return rules


def toml_string(v: str) -> str:
    """Literal single quotes unless the value contains one (same as config.ts)."""
    return json.dumps(v, ensure_ascii=False) if "'" in v else f"'{v}'"


def _rule_line(key: str, rule: dict) -> str:
    if key == "enabled":
        return f"{key:<7} = {'true' if rule['enabled'] else 'false'}"
    return f"{key:<7} = {toml_string(str(rule[key]))}"


def upsert_rule(text: str, index: int | None, rule: dict) -> str:
    if index is None:
        keys = [k for k in RULE_KEYS if k != "reason" or rule["reason"] != ""]
        block = "\n".join(["[[custom_rules]]"] + [_rule_line(k, rule) for k in keys])
        sep = "\n" if text.endswith("\n") else "\n\n"
        return text + sep + block + "\n"
    rules = parse_custom_rules(text)
    if not 0 <= index < len(rules):
        raise EditError(f"no custom rule #{index + 1}")
    existing = rules[index]
    lines = text.split("\n")
    inserts = []
    for k in RULE_KEYS:
        line_no = existing["lines"][k]
        if line_no < 0:
            if k == "reason" and rule["reason"] == "":
                continue
            inserts.append(_rule_line(k, rule))
            continue
        before = lines[line_no]
        if k == "enabled":
            value = "true" if rule["enabled"] else "false"
            rx = r"(=\s*)(true|false)\b"
        else:
            value = toml_string(str(rule[k]))
            rx = r"(=\s*)" + STR_TOKEN
        if not re.search(rx, before):
            raise EditError(f"could not rewrite {k} on line {line_no + 1}")
        lines[line_no] = re.sub(rx, lambda m: m.group(1) + value, before, count=1)
    lines[existing["headerLine"] + 1:existing["headerLine"] + 1] = inserts
    return "\n".join(lines)


def save_config(file: Path, new_text: str) -> None:
    """Atomic save with a .bak of the previous version.

    The new file keeps the old one's mode and (when allowed) group: the hook
    may run as another user, and a config it can no longer read means every
    rule silently off.
    """
    st = os.stat(file)
    bak = file.with_name(file.name + ".bak")
    shutil.copyfile(file, bak)
    os.chmod(bak, st.st_mode & 0o7777)
    tmp = file.with_name(f".{file.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(new_text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, st.st_mode & 0o7777)
        try:
            os.chown(tmp, -1, st.st_gid)
        except OSError:
            pass
        os.replace(tmp, file)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# Pattern check: the Python hook compiles with `re`, the Go hook with RE2. A
# pattern only one of them accepts silently never fires under the other, so
# refuse the constructs RE2 lacks as well as anything `re` rejects.
# --------------------------------------------------------------------------- #
def pattern_error(pat: str) -> str:
    try:
        re.compile(pat)
    except re.error as e:
        return f"does not compile: {e}"
    i, n = 0, len(pat)
    while i < n:
        c = pat[i]
        if c == "\\":
            nxt = pat[i + 1] if i + 1 < n else ""
            if nxt in "123456789":
                return "backreferences are not supported by the Go hook"
            if nxt == "Z":
                return r"\Z is not supported by the Go hook (use \z or $)"
            i += 2
            continue
        if c == "[":  # skip a character class
            j = i + 1
            if j < n and pat[j] == "^":
                j += 1
            if j < n and pat[j] == "]":
                j += 1
            while j < n and pat[j] != "]":
                j += 2 if pat[j] == "\\" else 1
            i = j + 1
            continue
        if c == "(" and pat.startswith(("(?=", "(?!", "(?<=", "(?<!"), i):
            return "lookaround is not supported by the Go hook"
        if c == "(" and pat.startswith(("(?>", "(?P="), i):
            return "this group type is not supported by the Go hook"
        if c in "*+?}" and i + 1 < n and pat[i + 1] == "+":
            return "possessive quantifiers are not supported by the Go hook"
        i += 1
    return ""


# =========================================================================== #
# Log tailing. A cursor is "<dev>:<ino>:<offset>"; a different file identity
# (rotation) or a shorter file (truncation) restarts cleanly, like tail.ts.
# Only whole lines are consumed; a partial last line waits for the next poll.
# =========================================================================== #
def read_since(path: str | None, cursor: str, cap: int = TAIL_CAP) -> tuple[list[str], str, bool]:
    """Return (lines, next_cursor, restarted)."""
    if not path:
        return [], cursor, False
    try:
        f = open(path, "rb")
    except OSError:
        return [], cursor, False  # missing: keep the cursor, wait for it
    with f:
        st = os.fstat(f.fileno())
        ident, size = f"{st.st_dev}:{st.st_ino}", st.st_size
        start, restarted, skip_partial = 0, False, False
        parts = cursor.split(":") if cursor else []
        if len(parts) == 3 and f"{parts[0]}:{parts[1]}" == ident and parts[2].isdigit():
            start = int(parts[2])
            if start > size:  # truncated
                start, restarted = 0, True
        else:
            restarted = bool(cursor)  # rotated (or first read when no cursor)
        if size - start > cap:
            start, skip_partial = size - cap, True
        f.seek(start)
        data = f.read(size - start)
    if skip_partial:
        nl = data.find(b"\n")
        start += nl + 1 if nl >= 0 else len(data)
        data = data[nl + 1:] if nl >= 0 else b""
    end = data.rfind(b"\n")
    consumed = end + 1 if end >= 0 else 0
    lines = [ln for ln in data[:consumed].decode("utf-8", "replace").split("\n")[:-1] if ln.strip()]
    return lines, f"{ident}:{start + consumed}", restarted


# --------------------------------------------------------------------------- #
# Guard log lines (port of tui/src/parse.ts, plus the CONFIG / AUTH lines this
# server writes).
# --------------------------------------------------------------------------- #
LOG_LINE = re.compile(r"^\[([^\]]+)\]\s+(BLOCK|WARN|CONFIG|AUTH)\s+on=(\S+)\s+(.*)$")
GROUP_PHRASES = [
    ("destructive rm blocked", "dangerous_tools"),
    ("disk-destroying command blocked", "dangerous_tools"),
    ("curl|sh pipe-to-shell blocked", "dangerous_tools"),
    ("chmod 777 blocked", "dangerous_tools"),
    ("fork bomb blocked", "dangerous_tools"),
    ("matched extra_command_pattern", "dangerous_tools"),
    ("edit to secret file blocked", "dangerous_tools"),
    ("force-push blocked", "git_safety"),
    ("push to protected branch blocked", "git_safety"),
    ("flattery phrase(s):", "sycophancy"),
    ("superlative pile-up", "sycophancy"),
    ("gushing closer", "sycophancy"),
    ("unrequested scope-expansion language", "scope_drift"),
    ("self-flagged:", "self_report"),
    ("custom '", "custom_rules"),
]


def classify(reason: str) -> str:
    for phrase, group in GROUP_PHRASES:
        if reason.startswith(phrase):
            return group
    return "other"


def parse_line(line: str) -> dict | None:
    m = LOG_LINE.match(line)
    if not m:
        return None
    ts, action, on, reason = m.groups()
    group = {"CONFIG": "config", "AUTH": "auth"}.get(action) or classify(reason)
    return {"ts": clean(ts), "action": action, "on": clean(on), "group": group, "reason": clean(reason)}


def parse_net(line: str) -> dict | None:
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    ts, domain = obj.get("ts"), obj.get("domain")
    if not isinstance(ts, str) or not isinstance(domain, str):
        return None
    domain = clean(domain).strip().lower()
    if domain.endswith("."):
        domain = domain[:-1]
    if not domain or len(domain) > 253:
        return None
    kind = obj.get("kind")
    return {"ts": clean(ts)[:40], "domain": domain, "kind": clean(kind)[:20] if isinstance(kind, str) else ""}


class NetAggregate:
    """Domain counts since start (last 5 MB at start, then everything)."""

    def __init__(self, path: str):
        self.path = path
        self.cursor = ""
        self.domains: dict[str, list] = {}  # domain -> [count, first, last]
        self.lock = threading.Lock()

    def refresh(self) -> None:
        with self.lock:
            lines, self.cursor, _ = read_since(self.path, self.cursor)
            for ln in lines:
                e = parse_net(ln)
                if e is None:
                    continue
                row = self.domains.get(e["domain"])
                if row is None:
                    if len(self.domains) >= MAX_DOMAINS:
                        continue
                    self.domains[e["domain"]] = [1, e["ts"], e["ts"]]
                else:
                    row[0] += 1
                    row[1] = min(row[1], e["ts"])
                    row[2] = max(row[2], e["ts"])

    def rows(self, names=None) -> list[dict]:
        with self.lock:
            keys = self.domains.keys() if names is None else [n for n in names if n in self.domains]
            return [{"domain": d, "count": self.domains[d][0], "first": self.domains[d][1],
                     "last": self.domains[d][2]} for d in keys]


# =========================================================================== #
# Settings
# =========================================================================== #
class Settings:
    def __init__(self, host: str, port: int, allow_remote_edit: bool = False):
        self.host = host
        self.port = port
        self.allow_remote_edit = allow_remote_edit
        self.config = config_path()
        self.log = os.environ.get("COMPASS_LOG") or str(Path.home() / ".claude" / "compass-warns.log")
        self.netlog = os.environ.get("COMPASS_NETLOG") or None
        self.token_file = os.environ.get("COMPASS_WEB_TOKEN_FILE") or None
        self.token: str | None = None
        self.token_problem = "no COMPASS_WEB_TOKEN_FILE set" if not self.token_file else ""
        self.errors: list[str] = []
        self._load_token()
        self._validate()

    def _load_token(self) -> None:
        if not self.token_file:
            return
        try:
            st = os.stat(self.token_file)
            if st.st_mode & 0o077:
                self.token_problem = (f"token file mode is {oct(st.st_mode & 0o777)}; "
                                      "it must be readable by its owner only (chmod 600)")
                return
            with open(self.token_file, encoding="utf-8") as f:
                first = f.readline().strip()
        except (OSError, UnicodeDecodeError) as e:
            self.token_problem = f"token file unreadable ({e.__class__.__name__})"
            return
        if len(first) < MIN_TOKEN:
            self.token_problem = f"token file's first line is shorter than {MIN_TOKEN} characters"
            return
        self.token = first

    def _validate(self) -> None:
        if not 1 <= self.port <= 65535:
            self.errors.append(f"port {self.port} out of range")
        if not is_loopback(self.host) and self.token_file and not self.allow_remote_edit:
            self.errors.append(f"--host {self.host} is not loopback and a token file is set; "
                               "add --allow-remote-edit if you really mean it")
        if self.token_file and self.token is None:
            self.errors.append(f"token file set but unusable: {self.token_problem}")

    def edit_problem(self) -> str:
        """'' when writes can work right now, else why not."""
        if self.token is None:
            return self.token_problem
        if not self.config.is_file():
            return f"config not found at {self.config}"
        if not os.access(self.config.parent, os.W_OK) or not os.access(self.config, os.R_OK):
            return f"config directory {self.config.parent} is not writable by this server"
        lp = Path(self.log)
        if not (os.access(lp, os.W_OK) if lp.exists() else os.access(lp.parent, os.W_OK)):
            return f"guard log {self.log} is not writable, so changes could not be recorded"
        return ""

    def allowed_hosts(self) -> set[str]:
        hosts = {f"localhost:{self.port}", f"127.0.0.1:{self.port}", f"[::1]:{self.port}"}
        try:
            ip = ipaddress.ip_address(self.host)
            hosts.add(f"[{ip}]:{self.port}" if ip.version == 6 else f"{ip}:{self.port}")
        except ValueError:
            pass
        return hosts


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def network_globs(cfg_text: str) -> list[str]:
    try:
        net = tomllib.loads(cfg_text).get("network", {})
    except tomllib.TOMLDecodeError:
        return []
    exp = net.get("expected", []) if isinstance(net, dict) else []
    return [str(g).lower() for g in exp if isinstance(g, str)] if isinstance(exp, list) else []


# =========================================================================== #
# Server state shared by all request threads
# =========================================================================== #
class App:
    def __init__(self, settings: Settings):
        self.s = settings
        self.write_lock = threading.Lock()
        self.net = NetAggregate(settings.netlog) if settings.netlog else None

    # -- audit ---------------------------------------------------------------
    def audit_line(self, kind: str, text: str) -> str:
        ts = datetime.now().isoformat(timespec="seconds")
        return f"[{ts}] {kind:<6} on=web  {clean(text)}\n"

    def audit(self, kind: str, text: str) -> None:
        try:
            with open(self.s.log, "a", encoding="utf-8") as f:
                f.write(self.audit_line(kind, text))
        except OSError:
            pass

    # -- auth ----------------------------------------------------------------
    # No lockout on bad tokens: the token is >= 32 random characters, so a
    # lockout adds nothing against guessing, while the agent (same localhost)
    # could use it to keep the human from editing. Failures are logged only.
    def record_failure(self, why: str) -> None:
        self.audit("AUTH", f"write refused: {why}")

    def token_ok(self, header: str | None) -> bool:
        if not header or not header.startswith("Bearer ") or self.s.token is None:
            return False
        given = header[len("Bearer "):].strip().encode("utf-8", "replace")
        return hmac.compare_digest(given, self.s.token.encode("utf-8"))

    # -- reads ---------------------------------------------------------------
    def state(self) -> dict:
        text, cfg_error, toml_ok = "", "", True
        try:
            text = self.s.config.read_text(encoding="utf-8")
        except FileNotFoundError:
            cfg_error = "config file not found"
        except (OSError, UnicodeDecodeError) as e:
            cfg_error = f"config unreadable ({e.__class__.__name__})"
        if text:
            try:
                tomllib.loads(text)
            except tomllib.TOMLDecodeError as e:
                toml_ok = False
                cfg_error = f"config is not valid TOML, so the hook treats every rule as off: {e}"
        problem = self.s.edit_problem()
        lines, _, _ = read_since(self.s.log, "")
        counts = {"block": 0, "warn": 0, "config": 0, "auth": 0, "by_group": {}}
        for ln in lines:
            e = parse_line(ln)
            if e:
                counts[e["action"].lower()] += 1
                counts["by_group"][e["group"]] = counts["by_group"].get(e["group"], 0) + 1
        return {
            "version": VERSION,
            "paths": {"config": str(self.s.config), "log": self.s.log, "netlog": self.s.netlog},
            "editable": problem == "",
            "edit_problem": problem,
            "config_error": cfg_error,
            "toml_ok": toml_ok,
            "groups": [{"name": g["name"], "enabled": g["enabled"], "action": g["action"]}
                       for g in parse_groups(text)],
            "rules": [{"index": i, **{k: r[k] for k in ("name", "enabled", "on", "pattern", "action", "reason")}}
                      for i, r in enumerate(parse_custom_rules(text))],
            "counts": counts,
            "network": self.s.netlog is not None,
        }

    def events(self, cursor: str) -> dict:
        lines, nxt, restarted = read_since(self.s.log, cursor)
        entries = [e for e in map(parse_line, lines) if e]
        return {"entries": entries[-MAX_EVENTS:], "cursor": nxt, "restarted": restarted}

    def net_page(self, cursor: str, sig: str) -> dict:
        assert self.net is not None
        lines, nxt, restarted = read_since(self.s.netlog, cursor)
        self.net.refresh()  # after the client's read: the aggregate covers every line returned
        try:
            globs = network_globs(self.s.config.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            globs = []
        new_sig = hashlib.sha256("\n".join(globs).encode()).hexdigest()[:12]

        def expected(d: str) -> bool:
            return any(fnmatch.fnmatchcase(d, g) for g in globs)

        entries = [e for e in map(parse_net, lines) if e]
        full = not cursor or sig != new_sig
        rows = self.net.rows(None if full else {e["domain"] for e in entries})
        for r in rows:
            r["expected"] = expected(r["domain"])
        tail = entries[-MAX_NET_TAIL:]
        for e in tail:
            e["expected"] = expected(e["domain"])
        return {"entries": tail, "domains": rows, "full": full, "cursor": nxt,
                "restarted": restarted, "sig": new_sig, "expected": globs}

    # -- writes --------------------------------------------------------------
    def _commit(self, before: str, after: str, changes: list[str], check) -> None:
        """Validate, record, save. Raises EditError (nothing saved) on any doubt."""
        try:
            doc = tomllib.loads(after)
        except tomllib.TOMLDecodeError as e:
            raise EditError(f"the edit would make the config invalid TOML ({e}); not saved")
        check(doc)
        try:
            audit = open(self.s.log, "a", encoding="utf-8")
        except OSError:
            raise EditError("guard log not writable, so the change could not be recorded; not saved")
        with audit:
            # record first: a change must never be saved without its log line.
            # If the save then fails, a second line says so.
            try:
                audit.write("".join(self.audit_line("CONFIG", c) for c in changes))
                audit.flush()
                os.fsync(audit.fileno())
            except OSError:
                raise EditError("could not record the change in the guard log; not saved")
            try:
                save_config(self.s.config, after)
            except OSError as e:
                audit.write(self.audit_line("CONFIG", f"previous change NOT saved: {e.__class__.__name__}"))
                raise

    def set_group(self, body: dict) -> dict:
        group = body.get("group")
        enabled, action = body.get("enabled"), body.get("action")
        if not isinstance(group, str) or not group:
            raise EditError("group must be a non-empty string")
        if enabled is not None and not isinstance(enabled, bool):
            raise EditError("enabled must be true or false")
        if action is not None and action not in ("block", "warn"):
            raise EditError("action must be 'block' or 'warn'")
        if enabled is None and action is None:
            raise EditError("nothing to change")
        with self.write_lock:
            text = self.s.config.read_text(encoding="utf-8")
            g = next((x for x in parse_groups(text) if x["name"] == group), None)
            if g is None:
                raise EditError(f"no group [{group}]")
            after, changes = text, []
            if enabled is not None and enabled != g["enabled"]:
                after = set_value(after, group, "enabled", "true" if enabled else "false")
                changes.append(f"{group}.enabled: {str(g['enabled']).lower()} -> {str(enabled).lower()}")
            if action is not None and action != g["action"]:
                after = set_value(after, group, "action", action)
                changes.append(f"{group}.action: {g['action']} -> {action}")
            if not changes:
                return {"ok": True, "changed": []}

            def check(doc):
                sec = doc.get(group, {})
                if enabled is not None and sec.get("enabled") is not enabled:
                    raise EditError("edit did not round-trip; not saved")
                if action is not None and sec.get("action") != action:
                    raise EditError("edit did not round-trip; not saved")

            self._commit(text, after, changes, check)
            return {"ok": True, "changed": changes}

    def set_rule(self, body: dict) -> dict:
        index = body.get("index")
        if index is not None and (not isinstance(index, int) or isinstance(index, bool)):
            raise EditError("index must be an integer or null")
        r = body.get("rule")
        if not isinstance(r, dict):
            raise EditError("rule must be an object")
        rule = empty_rule()
        for k in ("name", "on", "pattern", "action", "reason"):
            v = r.get(k, "")
            if not isinstance(v, str):
                raise EditError(f"{k} must be a string")
            if _CTRL.search(v):
                raise EditError(f"{k} must not contain control characters or line breaks")
            try:
                v.encode("utf-8")
            except UnicodeEncodeError:
                raise EditError(f"{k} is not valid text")
            rule[k] = v
        enabled = r.get("enabled", True)
        if not isinstance(enabled, bool):
            raise EditError("enabled must be true or false")
        rule["enabled"] = enabled
        rule["name"] = rule["name"].strip()
        if not rule["name"] or len(rule["name"]) > 100:
            raise EditError("name must be 1-100 characters")
        if rule["on"] not in ("pretool", "stop"):
            raise EditError("surface must be 'pretool' or 'stop'")
        if rule["action"] not in ("block", "warn"):
            raise EditError("action must be 'block' or 'warn'")
        if not rule["pattern"] or len(rule["pattern"]) > 2000:
            raise EditError("pattern must be 1-2000 characters")
        if len(rule["reason"]) > 500:
            raise EditError("reason must be at most 500 characters")
        err = pattern_error(rule["pattern"])
        if err:
            raise EditError(f"pattern {err}")
        fields = ("name", "enabled", "on", "pattern", "action", "reason")
        with self.write_lock:
            text = self.s.config.read_text(encoding="utf-8")
            rules = parse_custom_rules(text)
            if index is not None:
                if not 0 <= index < len(rules):
                    raise EditError(f"no custom rule #{index + 1}")
                if body.get("current_name") != rules[index]["name"]:
                    raise EditError("the config changed since the page loaded; reload and try again")
                old = rules[index]
                changes = [f"custom_rules[{old['name']!r}].{k}: {_fmt(old[k])} -> {_fmt(rule[k])}"
                           for k in fields if old[k] != rule[k]]
                if not changes:
                    return {"ok": True, "changed": []}
                pos = index
            else:
                changes = [f"custom_rules += {rule['name']!r} ({rule['on']}, {rule['action']}, "
                           f"{'enabled' if enabled else 'disabled'}) /{rule['pattern']}/"]
                pos = len(rules)
            after = upsert_rule(text, index, rule)

            def check(doc):
                got = doc.get("custom_rules", [])
                mine = parse_custom_rules(after)
                if pos >= len(got) or pos >= len(mine):
                    raise EditError("edit did not round-trip; not saved")
                t = got[pos]
                for k in fields:
                    want = rule[k]
                    have = t.get(k, True if k == "enabled" else "")
                    if have != want or mine[pos][k] != want:
                        raise EditError("edit did not round-trip; not saved")

            self._commit(text, after, changes, check)
            return {"ok": True, "changed": changes}


def _fmt(v) -> str:
    return str(v).lower() if isinstance(v, bool) else repr(v)


# =========================================================================== #
# HTTP
# =========================================================================== #
class Handler(BaseHTTPRequestHandler):
    server_version = f"compass-web/{VERSION}"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    app: App  # set on the subclass made by make_server

    # -- plumbing ------------------------------------------------------------
    def log_message(self, fmt, *args):  # never log query strings or headers
        path = urlsplit(self.path or "").path if hasattr(self, "path") else "-"
        sys.stderr.write(f"{self.log_date_time_string()} {self.command or '-'} {clean(path)[:200]}\n")

    def log_request(self, code="-", size="-"):
        self.log_message("")

    def send_error(self, code, message=None, explain=None):
        self._json(code, {"error": message or "error"})

    def _headers(self, code: int, ctype: str, length: int) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()

    def _json(self, code: int, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._headers(code, "application/json; charset=utf-8", len(data))
        if self.command != "HEAD":
            self.wfile.write(data)

    def _host_ok(self) -> bool:
        return (self.headers.get("Host") or "").lower() in self.app.s.allowed_hosts()

    # -- GET -----------------------------------------------------------------
    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"error": "unexpected Host header"})
        url = urlsplit(self.path)
        q = parse_qs(url.query)
        cursor = (q.get("after") or [""])[0][:200]
        if url.path == "/":
            return self._asset(INDEX_HTML, "text/html; charset=utf-8")
        if url.path == "/app.js":
            return self._asset(APP_JS, "text/javascript; charset=utf-8")
        if url.path == "/app.css":
            return self._asset(APP_CSS, "text/css; charset=utf-8")
        if url.path == "/api/state":
            return self._json(200, self.app.state())
        if url.path == "/api/events":
            return self._json(200, self.app.events(cursor))
        if url.path == "/api/net":
            if self.app.net is None:
                return self._json(404, {"error": "network panel not enabled (COMPASS_NETLOG unset)"})
            return self._json(200, self.app.net_page(cursor, (q.get("sig") or [""])[0][:40]))
        return self._json(404, {"error": "not found"})

    def _asset(self, body: str, ctype: str) -> None:
        data = body.encode("utf-8")
        self._headers(200, ctype, len(data))
        self.wfile.write(data)

    # -- POST ----------------------------------------------------------------
    def do_POST(self):
        self.close_connection = True  # a refused request may leave an unread body
        if not self._host_ok():
            return self._json(403, {"error": "unexpected Host header"})
        path = urlsplit(self.path).path
        if path not in ("/api/group", "/api/rule"):
            return self._json(404, {"error": "not found"})
        app = self.app
        if app.s.token is None:
            app.audit("AUTH", "write refused: page is view-only")
            return self._json(403, {"error": f"view-only: {app.s.token_problem}"})
        if not app.token_ok(self.headers.get("Authorization")):
            app.record_failure("bad token" if self.headers.get("Authorization") else "no token")
            return self._json(401, {"error": "missing or wrong token"})
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self._json(415, {"error": "writes accept application/json only"})
        length = self.headers.get("Content-Length")
        if self.headers.get("Transfer-Encoding") or length is None or not length.isdigit():
            return self._json(411, {"error": "Content-Length required"})
        if int(length) > MAX_BODY:
            return self._json(413, {"error": f"body larger than {MAX_BODY} bytes"})
        try:
            body = json.loads(self.rfile.read(int(length)).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"error": "body is not a JSON object"})
        problem = app.s.edit_problem()
        if problem:
            app.audit("AUTH", f"write refused: {problem}")
            return self._json(403, {"error": problem})
        try:
            result = app.set_group(body) if path == "/api/group" else app.set_rule(body)
        except EditError as e:
            app.audit("CONFIG", f"write refused: {e}")
            return self._json(400, {"error": str(e)})
        except (OSError, UnicodeDecodeError) as e:
            app.audit("CONFIG", f"write refused: {e.__class__.__name__}")
            return self._json(500, {"error": f"not saved: {e.__class__.__name__}"})
        return self._json(200, result)

    def do_PUT(self):
        self._json(405, {"error": "method not allowed"})

    do_DELETE = do_PATCH = do_OPTIONS = do_PUT


def make_server(settings: Settings) -> ThreadingHTTPServer:
    family = socket.AF_INET6 if ":" in settings.host else socket.AF_INET

    class Server(ThreadingHTTPServer):
        address_family = family
        daemon_threads = True

    handler = type("BoundHandler", (Handler,), {"app": App(settings)})
    srv = Server((settings.host, settings.port), handler)
    if settings.port == 0:  # tests: learn the real port for the Host check
        settings.port = srv.server_address[1]
    return srv


def check_report(s: Settings) -> tuple[str, int]:
    out = [f"compass-web {VERSION}", f"listen:      http://{s.host}:{s.port}/"]
    out.append(f"config:      {s.config} ({'found' if s.config.is_file() else 'NOT FOUND'})")
    out.append(f"guard log:   {s.log} ({'found' if Path(s.log).is_file() else 'not created yet'})")
    if s.netlog:
        out.append(f"network log: {s.netlog} ({'found' if Path(s.netlog).is_file() else 'not created yet'})")
    else:
        out.append("network log: unset (panel hidden)")
    out.append(f"token file:  {s.token_file or 'unset'}")
    problem = s.edit_problem()
    out.append("editable: yes" if not problem else f"editable: no ({problem})")
    for e in s.errors:
        out.append(f"ERROR: {e}")
    return "\n".join(out), 1 if s.errors else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Local web page for claude-compass.")
    ap.add_argument("--host", default=os.environ.get("COMPASS_WEB_HOST") or DEFAULT_HOST)
    ap.add_argument("--port", default=os.environ.get("COMPASS_WEB_PORT") or str(DEFAULT_PORT))
    ap.add_argument("--allow-remote-edit", action="store_true",
                    help="allow editing when --host is not a loopback address")
    ap.add_argument("--check", action="store_true", help="validate settings and exit")
    ap.add_argument("--version", action="version", version=f"compass-web {VERSION}")
    a = ap.parse_args(argv)
    try:
        port = int(a.port)
    except ValueError:
        print(f"ERROR: port {a.port!r} is not a number")
        return 1
    s = Settings(a.host, port, a.allow_remote_edit)
    if a.check:
        report, code = check_report(s)
        print(report)
        return code
    if os.geteuid() == 0:
        print("ERROR: refusing to run as root; run as an unprivileged user", file=sys.stderr)
        return 1
    if s.errors:
        print("\n".join(f"ERROR: {e}" for e in s.errors), file=sys.stderr)
        return 1
    srv = make_server(s)
    problem = s.edit_problem()
    print(f"compass-web {VERSION} on http://{'localhost' if is_loopback(s.host) else s.host}:{s.port}/"
          f" ({'editable' if not problem else 'view-only: ' + problem})", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


# =========================================================================== #
# The page. No inline script, no inline styles, no HTML built from data.
# =========================================================================== #
INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>compass</title>
<link rel="stylesheet" href="/app.css">
<script src="/app.js" defer></script>
</head>
<body>
<header>
  <h1>compass <span id="version" class="dim"></span></h1>
  <nav>
    <button type="button" class="tab" data-tab="live">Live</button>
    <button type="button" class="tab" data-tab="rules">Rules</button>
    <button type="button" class="tab" data-tab="net" id="tab-net" hidden>Network</button>
  </nav>
</header>
<main>
  <p id="banner" class="banner" hidden></p>

  <section id="live" class="panel">
    <div class="bar">
      <div id="counts" class="counts"></div>
      <div class="filters">
        <button type="button" class="filter" data-filter="all">all</button>
        <button type="button" class="filter" data-filter="BLOCK">blocks</button>
        <button type="button" class="filter" data-filter="WARN">warns</button>
      </div>
    </div>
    <p class="dim small">Guard log: <code id="log-path"></code></p>
    <table class="data">
      <thead><tr><th>time</th><th>what</th><th>where</th><th>group</th><th>reason</th></tr></thead>
      <tbody id="events"></tbody>
    </table>
    <p id="events-empty" class="empty">Nothing fired yet. That is the healthy state.</p>
  </section>

  <section id="rules" class="panel" hidden>
    <p class="dim small">Config: <code id="config-path"></code></p>
    <p id="lock" class="lock"></p>
    <p id="config-error" class="error" hidden></p>
    <p id="rules-msg" class="msg" hidden></p>
    <h2>Rule groups</h2>
    <table class="data">
      <thead><tr><th>group</th><th>on</th><th>action</th></tr></thead>
      <tbody id="groups"></tbody>
    </table>
    <h2>Custom rules</h2>
    <table class="data">
      <thead><tr><th>name</th><th>on</th><th>action</th><th>surface</th><th>pattern</th><th>reason</th><th></th></tr></thead>
      <tbody id="custom"></tbody>
    </table>
    <p id="custom-empty" class="empty" hidden>No custom rules.</p>
    <p><button type="button" id="add-rule" class="edit-control">Add a custom rule</button></p>
    <div id="rule-form" class="form" hidden>
      <h3 id="form-title"></h3>
      <label>name <input id="f-name" maxlength="100" autocomplete="off"></label>
      <label>surface
        <select id="f-on"><option value="pretool">pretool (Bash command)</option><option value="stop">stop (final reply)</option></select>
      </label>
      <label>pattern <input id="f-pattern" class="mono" maxlength="2000" autocomplete="off" spellcheck="false"></label>
      <label>action
        <select id="f-action"><option value="warn">warn</option><option value="block">block</option></select>
      </label>
      <label class="check"><input type="checkbox" id="f-enabled"> enabled</label>
      <label>reason <input id="f-reason" maxlength="500" autocomplete="off"></label>
      <p id="form-msg" class="error" hidden></p>
      <p><button type="button" id="f-save">Save</button> <button type="button" id="f-cancel">Cancel</button></p>
    </div>
  </section>

  <section id="net" class="panel" hidden>
    <p class="dim small">Network log: <code id="netlog-path"></code>. Read-only.</p>
    <div class="bar">
      <input id="net-filter" type="search" placeholder="filter domains" autocomplete="off" spellcheck="false">
      <label class="check"><input type="checkbox" id="hide-expected"> hide expected</label>
      <span id="net-summary" class="dim small"></span>
    </div>
    <table class="data">
      <thead><tr>
        <th><button type="button" class="sort" data-sort="domain">domain</button></th>
        <th><button type="button" class="sort" data-sort="count">lookups</button></th>
        <th><button type="button" class="sort" data-sort="first">first seen</button></th>
        <th><button type="button" class="sort" data-sort="last">last seen</button></th>
        <th>status</th>
      </tr></thead>
      <tbody id="domains"></tbody>
    </table>
    <h2>Live tail</h2>
    <ol id="net-tail" class="tail"></ol>
  </section>
</main>
</body>
</html>
"""

APP_CSS = """
:root{--bg:#f7f6f2;--fg:#1d2330;--dim:#6b7280;--line:#dcd9d0;--card:#fff;--red:#b42318;--red-bg:#fde8e6;
--yellow:#8a5a00;--yellow-bg:#fdf1d6;--blue:#1d4ed8;--blue-bg:#e3ebfd;--green:#1f7a3d;--green-bg:#e3f4e8;
--accent:#1d2330;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#14171d;--fg:#e6e6e3;--dim:#9aa0aa;--line:#2b303a;--card:#1b1f27;
--red:#ff8a80;--red-bg:#3a1d1b;--yellow:#f3c55b;--yellow-bg:#352a12;--blue:#8fb0ff;--blue-bg:#1b2540;
--green:#7fd39a;--green-bg:#17301f;--accent:#e6e6e3;color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
header{display:flex;align-items:center;gap:24px;padding:12px 16px;border-bottom:1px solid var(--line);flex-wrap:wrap}
h1{font-size:18px;margin:0}
h2{font-size:15px;margin:24px 0 8px}
h3{font-size:14px;margin:0 0 8px}
main{padding:16px;max-width:1200px}
nav{display:flex;gap:4px}
button,select,input{font:inherit;color:inherit}
button{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:4px 10px;cursor:pointer}
button:disabled,select:disabled,input:disabled{opacity:.5;cursor:not-allowed}
.tab.active,.filter.active{background:var(--accent);color:var(--bg);border-color:var(--accent)}
code,.mono,td.mono{font-family:ui-monospace,"Cascadia Mono",Consolas,monospace;font-size:13px}
.dim{color:var(--dim)} .small{font-size:12px}
.bar{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:8px}
.counts{display:flex;gap:6px;flex-wrap:wrap}
.chip{border:1px solid var(--line);border-radius:999px;padding:1px 8px;font-size:12px;background:var(--card)}
.filters{display:flex;gap:4px}
table.data{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line)}
table.data th,table.data td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:top}
table.data th{font-weight:600;font-size:12px;color:var(--dim)}
td.reason,td.pattern{word-break:break-word;overflow-wrap:anywhere}
td.nowrap{white-space:nowrap}
.badge{display:inline-block;font-size:11px;font-weight:700;border-radius:4px;padding:1px 6px}
.b-BLOCK{color:var(--red);background:var(--red-bg)} .b-WARN{color:var(--yellow);background:var(--yellow-bg)}
.b-CONFIG{color:var(--blue);background:var(--blue-bg)} .b-AUTH{color:var(--red);background:var(--red-bg)}
.b-expected{color:var(--green);background:var(--green-bg)} .b-other{color:var(--yellow);background:var(--yellow-bg)}
.empty{color:var(--dim);padding:8px 0}
.lock{padding:6px 10px;border-radius:6px;background:var(--card);border:1px solid var(--line)}
.lock.locked{border-color:var(--yellow);color:var(--yellow)}
.error{color:var(--red)} .msg{color:var(--green)}
.banner{padding:8px 12px;border-radius:6px;background:var(--red-bg);color:var(--red)}
.form{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;margin-top:8px;max-width:640px}
.form label{display:block;margin:6px 0}
.form input:not([type=checkbox]),.form select{display:block;width:100%;padding:4px 6px;border:1px solid var(--line);border-radius:4px;background:var(--bg)}
.check{display:inline-flex !important;align-items:center;gap:6px}
#net-filter{padding:4px 8px;border:1px solid var(--line);border-radius:6px;background:var(--card);min-width:220px}
.sort{border:0;background:none;padding:0;font-weight:600;font-size:12px;color:var(--dim)}
.sort.on{color:var(--fg)}
.tail{list-style:none;padding:0;margin:0;font-family:ui-monospace,Consolas,monospace;font-size:12px}
.tail li{padding:2px 0;border-bottom:1px solid var(--line);overflow-wrap:anywhere}
@media (max-width:640px){table.data th:nth-child(3),table.data td:nth-child(3){display:none}}
"""

APP_JS = r"""
'use strict';
// Every string from the server is data, written with textContent only.
(function () {
  const $ = (id) => document.getElementById(id);
  const TOKEN_KEY = 'compass-web-token';

  // move #token=... into sessionStorage and out of the address bar
  if (location.hash.startsWith('#token=')) {
    try { sessionStorage.setItem(TOKEN_KEY, decodeURIComponent(location.hash.slice(7))); } catch (e) {}
    history.replaceState(null, '', location.pathname + location.search);
  }
  function token() { try { return sessionStorage.getItem(TOKEN_KEY) || ''; } catch (e) { return ''; } }
  function dropToken() { try { sessionStorage.removeItem(TOKEN_KEY); } catch (e) {} }

  function el(tag, opts, kids) {
    const n = document.createElement(tag);
    if (opts) {
      if (opts.cls) n.className = opts.cls;
      if (opts.text !== undefined) n.textContent = String(opts.text);
      if (opts.title) n.title = String(opts.title);
    }
    (kids || []).forEach((k) => n.appendChild(k));
    return n;
  }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }
  function show(n, on) { n.hidden = !on; }

  async function getJSON(url) {
    const r = await fetch(url, { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }
  async function post(url, body) {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token() },
      body: JSON.stringify(body),
    });
    let data = {};
    try { data = await r.json(); } catch (e) {}
    if (r.status === 401) { dropToken(); }
    if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
    return data;
  }

  // ── tabs ──────────────────────────────────────────────────────────────
  function setTab(name) {
    document.querySelectorAll('.tab').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
    ['live', 'rules', 'net'].forEach((id) => show($(id), id === name));
    try { localStorage.setItem('compass-web-tab', name); } catch (e) {}
  }
  document.querySelectorAll('.tab').forEach((b) => b.addEventListener('click', () => setTab(b.dataset.tab)));

  // ── state / rules ────────────────────────────────────────────────────
  let state = null;
  let canEdit = false;
  let lastRulesKey = '';

  function setMsg(text, isError) {
    const m = $('rules-msg');
    m.textContent = text;
    m.className = isError ? 'error' : 'msg';
    show(m, !!text);
  }

  async function loadState() {
    try {
      state = await getJSON('/api/state');
      show($('banner'), false);
    } catch (e) {
      $('banner').textContent = 'compass-web is not answering (' + e.message + '). Retrying.';
      show($('banner'), true);
      return;
    }
    $('version').textContent = 'v' + state.version;
    $('log-path').textContent = state.paths.log;
    $('config-path').textContent = state.paths.config;
    $('netlog-path').textContent = state.paths.netlog || '';
    show($('tab-net'), state.network);
    canEdit = state.editable && token() !== '';
    const lock = $('lock');
    if (!state.editable) {
      lock.textContent = 'Editing is locked: ' + state.edit_problem + '.';
      lock.className = 'lock locked';
    } else if (!token()) {
      lock.textContent = 'Editing is locked in this tab: open the page with the edit link (#token=…) to edit.';
      lock.className = 'lock locked';
    } else {
      lock.textContent = 'Editing is on. Every change is written to the guard log.';
      lock.className = 'lock';
    }
    $('config-error').textContent = state.config_error;
    show($('config-error'), !!state.config_error);
    const key = JSON.stringify([state.groups, state.rules, canEdit]);
    if (key !== lastRulesKey) { lastRulesKey = key; renderRules(); }
    document.querySelectorAll('.edit-control').forEach((c) => { c.disabled = !canEdit; });
  }

  function actionSelect(value, onChange) {
    const s = el('select', { cls: 'edit-control' });
    ['block', 'warn'].forEach((v) => { const o = el('option', { text: v }); o.value = v; s.appendChild(o); });
    s.value = value || 'warn';
    s.disabled = !canEdit;
    s.addEventListener('change', () => onChange(s.value));
    return s;
  }
  function toggle(checked, onChange) {
    const c = el('input', { cls: 'edit-control' });
    c.type = 'checkbox';
    c.checked = !!checked;
    c.disabled = !canEdit;
    c.addEventListener('change', () => onChange(c.checked));
    return c;
  }

  async function write(url, body, okText) {
    try {
      const r = await post(url, body);
      setMsg(r.changed && r.changed.length ? okText + ' ' + r.changed.join('; ') : 'No change.', false);
      return true;
    } catch (e) {
      setMsg('Not saved: ' + e.message, true);
      return false;
    } finally {
      lastRulesKey = '';
      await loadState();
    }
  }

  function renderRules() {
    const tb = $('groups');
    clear(tb);
    state.groups.forEach((g) => {
      const tr = el('tr');
      tr.appendChild(el('td', { cls: 'mono', text: g.name }));
      tr.appendChild(el('td', null, [toggle(g.enabled, (v) => write('/api/group', { group: g.name, enabled: v }, 'Saved:'))]));
      const act = el('td');
      if (g.action) act.appendChild(actionSelect(g.action, (v) => write('/api/group', { group: g.name, action: v }, 'Saved:')));
      else act.appendChild(el('span', { cls: 'dim', text: '–' }));
      tr.appendChild(act);
      tb.appendChild(tr);
    });
    const cb = $('custom');
    clear(cb);
    state.rules.forEach((r) => {
      const tr = el('tr');
      const full = (patch) => Object.assign({ name: r.name, on: r.on, pattern: r.pattern, action: r.action, enabled: r.enabled, reason: r.reason }, patch);
      tr.appendChild(el('td', { cls: 'mono', text: r.name }));
      tr.appendChild(el('td', null, [toggle(r.enabled, (v) => write('/api/rule', { index: r.index, current_name: r.name, rule: full({ enabled: v }) }, 'Saved:'))]));
      tr.appendChild(el('td', null, [actionSelect(r.action, (v) => write('/api/rule', { index: r.index, current_name: r.name, rule: full({ action: v }) }, 'Saved:'))]));
      tr.appendChild(el('td', { text: r.on }));
      tr.appendChild(el('td', { cls: 'mono pattern', text: r.pattern }));
      tr.appendChild(el('td', { cls: 'reason', text: r.reason }));
      const b = el('button', { cls: 'edit-control', text: 'Edit' });
      b.type = 'button';
      b.disabled = !canEdit;
      b.addEventListener('click', () => openForm(r));
      tr.appendChild(el('td', null, [b]));
      cb.appendChild(tr);
    });
    show($('custom-empty'), state.rules.length === 0);
  }

  let formRule = null;
  function openForm(r) {
    formRule = r;
    $('form-title').textContent = r ? 'Edit rule ' + r.name : 'New custom rule';
    $('f-name').value = r ? r.name : '';
    $('f-on').value = r ? r.on : 'pretool';
    $('f-pattern').value = r ? r.pattern : '';
    $('f-action').value = r ? r.action : 'warn';
    $('f-enabled').checked = r ? r.enabled : true;
    $('f-reason').value = r ? r.reason : '';
    show($('form-msg'), false);
    show($('rule-form'), true);
    $('f-name').focus();
  }
  $('add-rule').addEventListener('click', () => openForm(null));
  $('f-cancel').addEventListener('click', () => show($('rule-form'), false));
  $('f-save').addEventListener('click', async () => {
    const rule = {
      name: $('f-name').value.trim(), on: $('f-on').value, pattern: $('f-pattern').value,
      action: $('f-action').value, enabled: $('f-enabled').checked, reason: $('f-reason').value,
    };
    const body = formRule ? { index: formRule.index, current_name: formRule.name, rule } : { index: null, rule };
    try {
      const r = await post('/api/rule', body);
      show($('rule-form'), false);
      setMsg(r.changed && r.changed.length ? 'Saved: ' + r.changed.join('; ') : 'No change.', false);
    } catch (e) {
      $('form-msg').textContent = 'Not saved: ' + e.message;
      show($('form-msg'), true);
    }
    lastRulesKey = '';
    loadState();
  });

  // ── live events ──────────────────────────────────────────────────────
  const events = [];
  let evCursor = '';
  let filter = 'all';
  const MAX_ROWS = 500;

  document.querySelectorAll('.filter').forEach((b) => b.addEventListener('click', () => {
    filter = b.dataset.filter;
    document.querySelectorAll('.filter').forEach((x) => x.classList.toggle('active', x === b));
    renderEvents();
  }));

  function shortTs(ts) { const t = ts.indexOf('T'); return t >= 0 ? ts.slice(0, t) + ' ' + ts.slice(t + 1) : ts; }

  function renderEvents() {
    const counts = { BLOCK: 0, WARN: 0, CONFIG: 0, AUTH: 0 };
    const groups = {};
    events.forEach((e) => {
      counts[e.action] = (counts[e.action] || 0) + 1;
      if (e.action === 'BLOCK' || e.action === 'WARN') groups[e.group] = (groups[e.group] || 0) + 1;
    });
    const c = $('counts');
    clear(c);
    ['BLOCK', 'WARN', 'CONFIG', 'AUTH'].forEach((a) => {
      if (a === 'BLOCK' || a === 'WARN' || counts[a]) c.appendChild(el('span', { cls: 'badge b-' + a, text: a + ' ' + counts[a] }));
    });
    Object.keys(groups).sort().forEach((g) => c.appendChild(el('span', { cls: 'chip', text: g + ' ' + groups[g] })));

    const shown = filter === 'all' ? events : events.filter((e) => e.action === filter);
    const tb = $('events');
    clear(tb);
    const start = Math.max(0, shown.length - MAX_ROWS);
    for (let i = shown.length - 1; i >= start; i--) {
      const e = shown[i];
      const tr = el('tr');
      tr.appendChild(el('td', { cls: 'nowrap mono', text: shortTs(e.ts) }));
      tr.appendChild(el('td', null, [el('span', { cls: 'badge b-' + e.action, text: e.action })]));
      tr.appendChild(el('td', { text: e.on }));
      tr.appendChild(el('td', { cls: 'mono', text: e.group }));
      tr.appendChild(el('td', { cls: 'reason', text: e.reason }));
      tb.appendChild(tr);
    }
    const empty = $('events-empty');
    empty.textContent = events.length === 0 ? 'Nothing fired yet. That is the healthy state.' : 'No entries match this filter.';
    show(empty, shown.length === 0);
  }

  async function pollEvents() {
    try {
      const r = await getJSON('/api/events?after=' + encodeURIComponent(evCursor));
      evCursor = r.cursor;
      if (r.entries.length) {
        events.push(...r.entries);
        if (events.length > 5000) events.splice(0, events.length - 5000);
        renderEvents();
        if (r.entries.some((e) => e.action === 'CONFIG')) loadState();
      }
      show($('banner'), false);
    } catch (e) {
      $('banner').textContent = 'compass-web is not answering (' + e.message + '). Retrying.';
      show($('banner'), true);
    }
  }

  // ── network ──────────────────────────────────────────────────────────
  const domains = new Map();
  const tail = [];
  let netCursor = '';
  let netSig = '';
  let sortKey = 'last';
  let sortDesc = true;

  document.querySelectorAll('.sort').forEach((b) => b.addEventListener('click', () => {
    const k = b.dataset.sort;
    if (sortKey === k) sortDesc = !sortDesc; else { sortKey = k; sortDesc = k !== 'domain'; }
    renderNet();
  }));
  $('net-filter').addEventListener('input', renderNet);
  $('hide-expected').addEventListener('change', renderNet);

  function renderNet() {
    document.querySelectorAll('.sort').forEach((b) => b.classList.toggle('on', b.dataset.sort === sortKey));
    const q = $('net-filter').value.trim().toLowerCase();
    const hide = $('hide-expected').checked;
    let rows = Array.from(domains.values()).filter((d) => (!hide || !d.expected) && (!q || d.domain.includes(q)));
    rows.sort((a, b) => {
      const x = a[sortKey], y = b[sortKey];
      const c = x < y ? -1 : x > y ? 1 : 0;
      return sortDesc ? -c : c;
    });
    const total = domains.size;
    let other = 0;
    domains.forEach((d) => { if (!d.expected) other++; });
    $('net-summary').textContent = total + ' domains, ' + other + ' other' + (rows.length > 2000 ? ' (showing 2000)' : '');
    const tb = $('domains');
    clear(tb);
    rows.slice(0, 2000).forEach((d) => {
      const tr = el('tr');
      tr.appendChild(el('td', { cls: 'mono reason', text: d.domain }));
      tr.appendChild(el('td', { text: d.count }));
      tr.appendChild(el('td', { cls: 'nowrap mono', text: shortTs(d.first) }));
      tr.appendChild(el('td', { cls: 'nowrap mono', text: shortTs(d.last) }));
      tr.appendChild(el('td', null, [el('span', { cls: 'badge ' + (d.expected ? 'b-expected' : 'b-other'), text: d.expected ? 'expected' : 'other' })]));
      tb.appendChild(tr);
    });
    const tl = $('net-tail');
    clear(tl);
    for (let i = tail.length - 1; i >= 0; i--) {
      const e = tail[i];
      if (hide && e.expected) continue;
      tl.appendChild(el('li', { text: shortTs(e.ts) + '  ' + (e.kind || '') + '  ' + e.domain + (e.expected ? '' : '  (other)') }));
    }
  }

  async function pollNet() {
    if (!state || !state.network) return;
    try {
      const r = await getJSON('/api/net?after=' + encodeURIComponent(netCursor) + '&sig=' + encodeURIComponent(netSig));
      netCursor = r.cursor;
      netSig = r.sig;
      if (r.full) domains.clear();
      r.domains.forEach((d) => domains.set(d.domain, d));
      tail.push(...r.entries);
      if (tail.length > 200) tail.splice(0, tail.length - 200);
      if (r.full || r.domains.length || r.entries.length) renderNet();
    } catch (e) { /* the events poll shows the banner */ }
  }

  // ── start ────────────────────────────────────────────────────────────
  let startTab = 'live';
  try { startTab = localStorage.getItem('compass-web-tab') || 'live'; } catch (e) {}
  document.querySelector('.filter[data-filter="all"]').classList.add('active');
  loadState().then(() => {
    setTab(startTab === 'net' && !(state && state.network) ? 'live' : startTab);
    pollEvents();
    pollNet();
  });
  setInterval(() => { pollEvents(); pollNet(); }, 2000);
  setInterval(loadState, 10000);
})();
"""

if __name__ == "__main__":
    sys.exit(main())
