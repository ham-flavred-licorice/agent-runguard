#!/usr/bin/env python3
"""Five simulated runs that show what runguard catches.

This demo does not need a Claude account. It points runguard at tests/fake_claude.py, a
scripted stand-in that accepts the real CLI's flags and prints results in the real CLI's
format. Everything runguard does (time limit, checks, resume, records) is the real code.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASK = '''name = "digest"
prompt = "Prepare the daily digest."
model = "sonnet"
effort = "low"
timeout_minutes = 0.1
allowed_tools = ["Read", "Write"]

[[steps]]
id = "fetch"
do = "Save the items to {run_dir}/items.json"
evidence = { file = "{run_dir}/items.json", json = true }

[[steps]]
id = "draft"
do = "Write one draft per item into {run_dir}/drafts/"
evidence = { glob = "{run_dir}/drafts/*.md", min_count = 2 }

[[steps]]
id = "summary"
do = "Write {run_dir}/summary.txt and mention the word digest"
evidence = { file = "{run_dir}/summary.txt", contains = "digest" }
'''
EVIDENCE = {
    "fetch": [["{run_dir}/items.json", "[1, 2]"]],
    "draft": [["{run_dir}/drafts/a.md", "draft a"], ["{run_dir}/drafts/b.md", "draft b"]],
    "summary": [["{run_dir}/summary.txt", "the digest is ready"]],
}
ALL = ["fetch", "draft", "summary"]
DENIAL = {"tool_name": "mcp__mail__send_email", "tool_use_id": "toolu_1", "tool_input": {"to": "team@example.com"}}
SCENARIOS = [
    ("1. A clean run", [{"do": ALL}]),
    ("2. The agent reports the drafts as done but never wrote them. Caught, then fixed by one targeted resume",
     [{"do": ["fetch", "summary"]}, {"do": ["draft"]}]),
    ("3. The agent skips the same step again on the resume", [{"do": ["fetch", "summary"]}]),
    ("4. The run hangs. It is killed at the time limit (6 seconds here)", [{"sleep": 120}]),
    ("5. The agent needs a tool that is not on the allow list. Denied at once, with the rule to add",
     [{"do": ["fetch", "draft"], "report": {"summary": "blocked"}, "result": {"permission_denials": [DENIAL]}}]),
]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="runguard-demo-") as tmp:
        task = Path(tmp) / "task.toml"
        task.write_text(TASK, encoding="utf-8")
        base = {**os.environ, "PYTHONPATH": str(ROOT), "RUNGUARD_CLAUDE_BIN": str(ROOT / "tests" / "fake_claude.py"),
                "FAKE_EVIDENCE": json.dumps(EVIDENCE)}
        for n, (title, script) in enumerate(SCENARIOS):
            print(f"\n{title}")
            env = {**base, "FAKE_LOG": str(Path(tmp) / f"calls-{n}.log"), "FAKE_SCRIPT": json.dumps(script)}
            out = subprocess.run([sys.executable, "-m", "runguard", "run", str(task)], env=env, capture_output=True, text=True)
            print("".join(f"  {line}\n" for line in out.stdout.replace(tmp, "…").splitlines()) + f"  exit code {out.returncode}")
        print("\nRun history")
        out = subprocess.run([sys.executable, "-m", "runguard", "status", "--runs-dir", str(Path(tmp) / "runs")], env=base,
                             capture_output=True, text=True)
        print("".join(f"  {line}\n" for line in out.stdout.splitlines()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
