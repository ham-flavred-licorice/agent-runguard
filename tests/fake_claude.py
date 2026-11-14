#!/usr/bin/env python3
"""A stand-in for the `claude` CLI, used by the tests.

It accepts the same flags, reads the prompt on stdin and prints one JSON result object in the
shape the real CLI prints (the shape was captured from Claude Code 2.1.325; see
tests/fixtures/auth_error.json for an unmodified capture). What it "does" is scripted:

  FAKE_LOG       file that receives one JSON line per invocation (argv + prompt)
  FAKE_SCRIPT    JSON list, one entry per invocation in order (the last entry repeats):
                   do         step ids whose evidence files get written
                   report     {step_id: "done" | "blocked" | "failed"}; default: "done" for every step
                   omit       step ids left out of the report
                   as_text    put the report in the result text (fenced) instead of structured_output
                   sleep      seconds to hang before answering
                   child      also start a long-lived child process and write its pid to this file
                   raw        print this string instead of a result object
                   exit       exit code (default 0)
                   result     keys merged into the result object (is_error, permission_denials, ...)
  FAKE_EVIDENCE  JSON {step_id: [[path, content], ...]}; {run_dir} in a path is replaced
  FAKE_HELP      text printed for --help (defaults to a list of every flag runguard may use)
"""
import json
import os
import re
import subprocess
import sys
import time

DEFAULT_HELP = """Usage: claude [options] [prompt]
  -p, --print  --output-format <format>  --permission-mode <mode>  --permission-prompts <target>
  --model <model>  --effort <level>  --fallback-model <model>  --max-budget-usd <amount>
  --allowedTools <tools...>  --disallowedTools <tools...>  --mcp-config <configs...>  --strict-mcp-config
  --add-dir <directories...>  --json-schema <schema>  --session-id <uuid>  -r, --resume [value]
"""


def main() -> int:
    argv = sys.argv[1:]
    if "--version" in argv:
        print("0.0.0 (fake claude)")
        return 0
    if "--help" in argv:
        print(os.environ.get("FAKE_HELP", DEFAULT_HELP))
        return 0

    prompt = sys.stdin.read()
    log = os.environ["FAKE_LOG"]
    calls = 0
    if os.path.exists(log):
        with open(log, encoding="utf-8") as fh:
            calls = sum(1 for _ in fh)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": argv, "prompt": prompt}) + "\n")

    script = json.loads(os.environ.get("FAKE_SCRIPT", "[{}]"))
    act = script[min(calls, len(script) - 1)]

    def flag(name):
        return argv[argv.index(name) + 1] if name in argv else None

    session = flag("--resume") or flag("--session-id") or "00000000-0000-4000-8000-000000000000"
    run_dir = (re.search(r"^Run directory: (.+)$", prompt, re.M) or [None, os.environ.get("RUNGUARD_RUN_DIR", ".")])[1]
    step_ids = re.findall(r"^\d+\. \[([a-z0-9_-]+)\]", prompt, re.M)
    if not step_ids:  # a resume prompt lists the ids in its last sentence only
        listed = re.search(r"step ids?: ([a-z0-9_, -]+)\.", prompt)
        step_ids = [x.strip() for x in listed.group(1).split(",")] if listed else []

    if act.get("child"):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        with open(act["child"], "w", encoding="utf-8") as fh:
            fh.write(str(child.pid))
    if act.get("sleep"):
        time.sleep(act["sleep"])

    evidence = json.loads(os.environ.get("FAKE_EVIDENCE", "{}"))
    for sid in act.get("do", []):
        for path, content in evidence.get(sid, []):
            path = path.replace("{run_dir}", run_dir)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)

    if "raw" in act:
        sys.stdout.write(act["raw"])
        return act.get("exit", 0)

    statuses = {sid: "done" for sid in step_ids}
    statuses.update(act.get("report", {}))
    for sid in act.get("omit", []):
        statuses.pop(sid, None)
    report = {"steps": [{"id": k, "status": v, "note": f"{k}: {v}"} for k, v in statuses.items()], "summary": "fake run"}

    model = flag("--model") or "default-model"
    result = {
        "type": "result", "subtype": "success", "is_error": False, "duration_ms": 1234, "duration_api_ms": 1000,
        "num_turns": 3, "session_id": session, "total_cost_usd": 0.0125, "stop_reason": "end_turn",
        "modelUsage": {model: {"inputTokens": 100, "outputTokens": 50}}, "permission_denials": [],
        "terminal_reason": "completed",
    }
    if act.get("as_text"):
        result["result"] = "All done.\n```json\n" + json.dumps(report) + "\n```"
    else:
        result["result"] = json.dumps(report)
        result["structured_output"] = report
    result.update(act.get("result", {}))
    print(json.dumps(result))
    return act.get("exit", 0)


if __name__ == "__main__":
    sys.exit(main())
