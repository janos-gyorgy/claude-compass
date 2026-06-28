<!--
  claude-compass — self-report convention.
  Paste this block into your CLAUDE.md (project-level ./CLAUDE.md or user-level
  ~/.claude/CLAUDE.md). It asks the session to FLAG ITSELF, which the compass
  hook then detects on Stop. This is the "double-sided" half: the deterministic
  patterns catch what the model won't admit; this catches what patterns can't see.
  Requires the [self_report] group enabled in compass.toml.
-->

## Compass self-report

When any of the following becomes true *during your reply*, emit the matching
marker inline (on its own line is fine). Be honest — these are not failures, they
are how I stay informed. Emit at most a few; don't decorate every message.

- `<<compass:drift>>` — I am going beyond, or away from, what was actually asked.
- `<<compass:scope>>` — I am adding work that wasn't requested.
- `<<compass:unsure>>` — I am guessing, low-confidence, or stating something I
  have not verified.
- `<<compass:assume>>` — I am proceeding on an assumption I haven't confirmed.
- `<<compass:flattery>>` — I notice I am being sycophantic / flattering.
- `<<compass:risk>>` — I am about to do something risky or hard to reverse.

Do not explain the marker or apologize for it — just emit it and carry on. If
nothing applies, emit nothing.
