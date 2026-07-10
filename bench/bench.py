#!/usr/bin/env python3
"""Benchmark the three compass implementations as Claude Code actually runs
them: one full process spawn per hook event. Cold-start dominates — a hook
runs on *every* tool call, so start-up cost is the real number.

Scenarios:
  silent-pass  PreToolUse, clean command, rules enabled, no hit — the tax
               paid on every single tool call in a session.
  deny         PreToolUse, rm -rf, rule fires (adds JSON emit + log write).
  stop-scan    Stop event over a 60-message transcript, clean final turn.

Usage: python3 bench/bench.py [-n RUNS]
"""
import argparse
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

IMPLS = {
    "python": [sys.executable, str(ROOT / "claude_compass.py")],
    "go": [str(ROOT / "go" / "compass")],
    "node (ts)": ["node", str(ROOT / "ts" / "dist" / "compass.mjs")],
}


def make_scenarios(tmp: Path) -> dict:
    cfg = tmp / "compass.toml"
    cfg.write_text(
        '[dangerous_tools]\nenabled = true\n[git_safety]\nenabled = true\n'
        '[sycophancy]\nenabled = true\n[scope_drift]\nenabled = true\n'
        '[self_report]\nenabled = true\n'
    )

    lines = []
    for i in range(30):
        lines.append(json.dumps({"type": "user", "message": {"role": "user", "content": f"task {i}"}}))
        lines.append(json.dumps({"type": "assistant", "message": {"role": "assistant",
                     "content": [{"type": "text", "text": f"Fixed item {i}; the cause was a stale cache entry. " * 20}]}}))
    transcript = tmp / "transcript.jsonl"
    transcript.write_text("\n".join(lines) + "\n")

    return {
        "silent-pass": (cfg, {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                              "tool_input": {"command": "npm test"}}),
        "deny": (cfg, {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                       "tool_input": {"command": "rm -rf /tmp/x"}}),
        "stop-scan": (cfg, {"hook_event_name": "Stop", "transcript_path": str(transcript)}),
    }


def time_one(cmd, cfg, event_json) -> float:
    t0 = time.perf_counter()
    subprocess.run(cmd, input=event_json, capture_output=True, text=True,
                   env={"COMPASS_CONFIG": str(cfg), "COMPASS_LOG": "/dev/null",
                        "PATH": "/usr/bin:/bin"})
    return (time.perf_counter() - t0) * 1000.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=50, help="timed runs per cell (default 50)")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as td:
        scenarios = make_scenarios(Path(td))
        results = {}
        for iname, cmd in IMPLS.items():
            if not Path(cmd[-1]).exists():
                print(f"skip {iname}: {cmd[-1]} not built", file=sys.stderr)
                continue
            for sname, (cfg, event) in scenarios.items():
                payload = json.dumps(event)
                for _ in range(3):  # warmup (page cache, JIT of nothing)
                    time_one(cmd, cfg, payload)
                samples = [time_one(cmd, cfg, payload) for _ in range(args.n)]
                results[(iname, sname)] = samples
                m = statistics.mean(samples)
                print(f"{iname:10s} {sname:12s} mean {m:7.2f} ms", file=sys.stderr)

    print(f"\n| scenario | " + " | ".join(IMPLS) + " |")
    print("|---|" + "---|" * len(IMPLS))
    for sname in scenarios:
        row = [f"**{sname}**"]
        for iname in IMPLS:
            s = results.get((iname, sname))
            if s is None:
                row.append("—")
            else:
                p95 = statistics.quantiles(s, n=20)[18]
                row.append(f"{statistics.mean(s):.1f} ms (p95 {p95:.1f})")
        print("| " + " | ".join(row) + " |")
    print(f"\n_{args.n} runs/cell, mean of full process spawns, "
          "COMPASS_LOG=/dev/null, all rule groups enabled._")
    return 0


if __name__ == "__main__":
    sys.exit(main())
