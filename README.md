# claude-compass

A small, **deterministic, local-only** principle-guard for [Claude Code](https://code.claude.com),
implemented as a single hook. It watches your session and — only for rules you
switch on — **blocks** dangerous actions or **warns** you when the model drifts
or starts sucking up.

No dependencies. No network. No LLM. Your session never leaves the machine.

```
your prompt ─▶ Claude Code ─▶ [compass hook] ─▶ tool runs
                                   │
                       rule hit?  ├── block  → tool denied (deny)
                                   ├── warn   → message to you, tool proceeds
                                   └── silent → nothing (every rule's default)
```

## Why this exists (and what it deliberately is *not*)

Runtime AI-agent guardrails are a crowded field — Meta's LlamaFirewall, Microsoft's
Agent Governance Toolkit, NVIDIA NeMo Guardrails, gateway products, and more. They
are **enterprise, security, and compliance** tools: OWASP agentic risks, the EU AI
Act, prompt-injection, audit trails. All heavyweight, most cloud-touching.

`claude-compass` is the corner none of them occupy: a **personal** guard that
enforces *your* working agreement on *your* coding agent, on your laptop, with
nothing phoning home. It's intentionally tiny and deterministic. If you need
semantic misalignment detection at org scale, use one of the big ones — this is
the opposite end of the spectrum on purpose.

## What it can guard

Four rule groups, **all OFF by default**. A fresh install changes nothing until
you edit [`compass.toml`](compass.toml) and flip `enabled = true`.

| Group | Event | Default action | Catches |
|---|---|---|---|
| `dangerous_tools` | PreToolUse | **block** | `rm -rf`, `dd`→disk, `mkfs`, `curl\|sh`, `chmod 777`, fork bombs, edits to `.env`/keys/secrets, your own regexes |
| `git_safety` | PreToolUse | **block** | `git push` to `main`/`master`, `--force` / `-f` pushes |
| `sycophancy` | Stop | **warn** | flattery phrases, superlative pile-ups, gushing closers — the "prompt begging" |
| `scope_drift` | Stop | **warn** | unrequested scope-expansion language ("while I was at it… I also refactored") |
| `self_report` | Stop | **warn** | markers the model emits *about itself* — `<<compass:drift\|scope\|unsure\|assume\|flattery\|risk>>` (see below) |

`action` is per group: `"block"` (hard deny / push-back) or `"warn"` (surfaced to
you, action proceeds). The shipped defaults are **tiered** — block the dangerous,
warn on the stylistic.

## Install

```bash
git clone https://github.com/janos-gyorgy/claude-compass
cd claude-compass
python3 install.py            # wires hooks into ~/.claude/settings.json
#   --project    install into ./.claude/settings.json instead
#   --uninstall  remove compass's hook entries (leaves everything else)
```

Your existing settings and hooks are preserved; a `.bak` is written first.
Prefer to wire it by hand? Copy the block in [`settings.snippet.json`](settings.snippet.json).

Then arm what you want:

```bash
$EDITOR compass.toml        # flip enabled = true on the groups you want
```

There's a ready-made armed config in [`examples/`](examples/compass.armed-example.toml).
Point at any config with `COMPASS_CONFIG=/path/to/your.toml`.

## The double-sided idea (`self_report`)

Pattern-matching has a ceiling: it catches *language*, not *meaning*. So the
`self_report` group adds a second, independent best-effort — **the model flags
itself**. You paste [`CLAUDE.snippet.md`](CLAUDE.snippet.md) into your
`CLAUDE.md`, which asks the session to emit a marker when it catches itself
drifting, guessing, assuming, sucking up, or about to do something risky:

```
Renamed the function. While I was here I also restructured the module
<<compass:scope>> and I haven't re-run the tests <<compass:unsure>>
```

The hook greps for `<<compass:CODE>>` on Stop and warns (or, for codes in
`block_markers`, hard-blocks). Still **100% local and deterministic on the hook
side** — the judgment comes from the model that's *already running*, so you get
near-"LLM-judge" coverage with no extra cost and nothing leaving the machine.

The two sides fail differently, which is the whole point: patterns catch a model
that won't admit a problem; self-report catches semantic problems no regex can
see. **Honest caveat:** self-report is only as reliable as the model's
willingness/ability to flag itself — weakest exactly when you'd most want it. Use
it as an *additive* best-effort, not a guarantee.

To use it: enable `[self_report]` in `compass.toml` **and** add the snippet to
your `CLAUDE.md`. One without the other does nothing.

## How it works

It's one hook script ([`claude_compass.py`](claude_compass.py)) registered on two
events. It reads the event JSON on stdin, dispatches on `hook_event_name`, checks
the enabled rules, and emits the Claude Code hook output contract:

- **block (PreToolUse)** → `{"hookSpecificOutput": {"permissionDecision": "deny", …}}`
- **warn** → `{"systemMessage": "compass ⚠ …"}` (shown to you, not to Claude)
- **block (Stop)** → `{"decision": "block", "reason": …}` (push back / ask for a redo)

### Two principles in the design

- **Fail-open.** Malformed input, a missing config, a parse error — *anything* —
  results in a clean exit 0 and no decision. A guard that can crash your session
  is worse than no guard. It never gets in your way by breaking.
- **Off by default.** Installing it is safe and reversible. You turn on exactly
  what you want, nothing surprises you.

## Honest limitations

- **`scope_drift` is a proxy, not real drift detection.** Deterministically you
  can catch *language* that signals "I did more than asked," but not true
  intent-vs-action divergence — that needs an LLM judge, which this build
  deliberately omits to stay local and dependency-free.
- **Pattern-based, so evadable and imperfect.** A determined model could phrase
  around the flattery patterns; an exotic destructive command could dodge the
  regexes. This raises the floor on the *common* cases; it is not a security
  boundary against an adversary.
- **`git_safety` reads the command, not repo state.** It reliably catches
  `git push … main` and `--force`; it can't know your current branch, so
  "block any commit while on main" isn't in this version.

## Two implementations, one contract

The hook is also ported to **Go** ([`go/`](go/)). Same stdin/stdout contract,
same rules, same fail-open behavior — pinned by a shared conformance suite
([`tests/conformance/`](tests/conformance/)) that runs both as real
subprocesses over the same JSON vectors.

Why bother? A hook spawns a fresh process on **every tool call**, so start-up
cost is the honest number, and the languages are not close
(Linux x86-64, 50 spawns/cell, all rule groups enabled):

| scenario | python | go |
|---|---|---|
| **silent-pass** (every tool call pays this) | 35.8 ms (p95 39.0) | **1.9 ms** (p95 3.1) |
| **deny** (rule fires) | 36.5 ms (p95 42.3) | **2.9 ms** (p95 4.0) |
| **stop-scan** (60-msg transcript) | 37.0 ms (p95 39.5) | **4.0 ms** (p95 6.4) |

The Go binary is ~19× faster than the Python original on the path that runs
constantly. (Node was measured too, out of curiosity: ~107 ms silent-pass —
V8 start-up dwarfs the actual work, which is exactly why TypeScript's home in
this repo is the long-lived TUI below, not the per-call hook.) Reproduce with
`python3 bench/bench.py`.

The Python file stays the reference implementation and the default install —
zero dependencies beats 34 ms for most sessions. If you want the Go binary
instead: `cd go && go build -o compass .` and point the hook command at it.

## compass-tui — watch the guard live

The guard logs every firing to `~/.claude/compass-warns.log`; [`tui/`](tui/) is
a small companion terminal monitor (TypeScript + [Ink](https://github.com/vadimdemedes/ink))
so you don't have to `tail -f` it: blocks in red, warns in yellow, counts per
rule group, live as they fire.

```bash
cd tui && npm install && npm run build && npm start
```

For a one-word launcher, drop a two-line shim on your PATH:

```bash
printf '#!/usr/bin/env bash\nexec node "%s/tui/dist/cli.js" "$@"\n' "$PWD" \
  > ~/.local/bin/compass-tui && chmod +x ~/.local/bin/compass-tui
```

Out of the hot path: the hook stays a zero-dependency single file; the TUI is
a separate long-lived process. `b`/`w`/`a` filter, arrows scroll, `q` quits.
Respects `$COMPASS_LOG`.

Press `c` for the **config pane**: toggle rule groups on/off (space) and flip
their action between block/warn (`b`/`w`) without hand-editing the armed
`compass.toml` (`$COMPASS_CONFIG`, else `~/.claude/compass.toml`). Edits are
deliberately line-surgical — only the value token on a group's `enabled` /
`action` line is ever rewritten, so comments, alignment, and every other key
are untouchable by construction. Saves are atomic (temp + rename) and keep a
`.bak` of the previous version. The hook re-reads the config per tool call, so
a toggle takes effect on the very next one.

## Test

```bash
python3 -m unittest discover -s tests -v      # Python unit + e2e suite
python3 tests/conformance/run.py              # contract suite, all 3 impls
cd go && go test ./...                        # Go native tests
```

29 Python tests (every matcher, transcript parsing, end-to-end subprocess runs)
plus 29 conformance vectors asserted identically against Python and Go.

## License

MIT — see [LICENSE](LICENSE).
