"""Run one task: launch the CLI with a hard time limit, verify every step, resume once for gaps."""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .config import Task
from .contract import REPORT_SCHEMA, RunContext, build_prompt, build_resume_prompt, parse_report
from .verify import check_evidence

OK_VERDICTS = ("verified", "reported")
# Flags that newer CLI versions have and older ones may not. They are used only when `claude --help` lists them.
OPTIONAL_FLAGS = ("--permission-prompts", "--json-schema", "--effort", "--fallback-model", "--max-budget-usd",
                  "--session-id", "--strict-mcp-config")
_flag_cache: dict[str, set[str] | None] = {}


@dataclass
class StepResult:
    id: str
    verdict: str          # verified | reported | unverified | missing | blocked | failed
    reported: str | None  # what the agent said: done | blocked | failed | None
    note: str
    evidence: str         # what the independent check found


@dataclass
class RunRecord:
    task: str
    run_id: str
    status: str           # ok | incomplete | blocked | timeout | error | skipped
    reason: str
    started_at: str
    finished_at: str = ""
    duration_s: float = 0.0
    attempts: int = 0
    steps: list[StepResult] = field(default_factory=list)
    summary: str = ""
    model: str | None = None
    effort: str | None = None
    models_used: list[str] = field(default_factory=list)
    num_turns: int | None = None
    cost_usd: float | None = None
    permission_denials: list[dict] = field(default_factory=list)
    session_id: str | None = None
    exit_codes: list[int | None] = field(default_factory=list)
    alert: str = ""
    run_dir: str = ""

    @property
    def failed_steps(self) -> list[str]:
        return [s.id for s in self.steps if s.verdict not in OK_VERDICTS]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Exec:
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str


def supported_flags(claude_bin: str) -> set[str] | None:
    """Flags listed by `claude --help`, or None when that cannot be read (then every flag is assumed)."""
    if claude_bin not in _flag_cache:
        try:
            out = subprocess.run([claude_bin, "--help"], capture_output=True, text=True, timeout=30)
            flags = set(re.findall(r"(?<![\w-])--[a-z][a-z-]+", out.stdout + out.stderr))
        except (OSError, subprocess.TimeoutExpired):
            flags = set()
        _flag_cache[claude_bin] = flags or None
    return _flag_cache[claude_bin]


def build_argv(task: Task, ctx: RunContext, flags: set[str] | None, session_id: str, resume: bool) -> list[str]:
    def has(flag: str) -> bool:
        return flags is None or flag in flags

    argv = [task.claude_bin, "-p", "--output-format", "json", "--permission-mode", task.permission_mode]
    if has("--permission-prompts"):
        argv += ["--permission-prompts", "none"]  # nobody is there to answer: deny instead of waiting
    if task.model:
        argv += ["--model", task.model]
    if task.effort and has("--effort"):
        argv += ["--effort", task.effort]
    if task.fallback_model and has("--fallback-model"):
        argv += ["--fallback-model", task.fallback_model]
    if task.max_budget_usd is not None and has("--max-budget-usd"):
        argv += ["--max-budget-usd", f"{task.max_budget_usd:g}"]
    if task.allowed_tools:
        argv += ["--allowedTools", *task.allowed_tools]
    if task.disallowed_tools:
        argv += ["--disallowedTools", *task.disallowed_tools]
    if task.mcp_config:
        argv += ["--mcp-config", str(task.mcp_config)]
        if has("--strict-mcp-config"):
            argv += ["--strict-mcp-config"]
    add_dirs = list(task.add_dirs)
    if task.workdir not in ctx.run_dir.parents:
        add_dirs.append(str(ctx.run_dir))  # let the agent write its outputs into the run directory
    for d in add_dirs:
        argv += ["--add-dir", d]
    if task.steps and has("--json-schema"):
        argv += ["--json-schema", json.dumps(REPORT_SCHEMA, separators=(",", ":"))]
    if resume:
        argv += ["--resume", session_id]
    elif has("--session-id"):
        argv += ["--session-id", session_id]
    return argv + list(task.extra_args)


def _kill_group(proc: subprocess.Popen) -> None:
    """Stop the whole process group, including anything the CLI started."""
    for sig, grace in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=grace)
            break
        except subprocess.TimeoutExpired:
            continue
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # children that ignored SIGTERM
    except ProcessLookupError:
        pass


def execute(argv: list[str], prompt: str, cwd: Path, env: dict, timeout: float) -> Exec:
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=cwd, env=env, text=True, start_new_session=True)
    try:
        out, err = proc.communicate(prompt, timeout=timeout)
        return Exec(proc.returncode, False, out, err)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        return Exec(None, True, out or "", err or "")


def parse_result(stdout: str) -> dict | None:
    """The CLI prints one JSON result object. Tolerate stray lines before it and the older list form."""
    text = stdout.strip()
    if not text:
        return None
    data = None
    try:
        data = json.loads(text)
    except ValueError:
        for line in reversed(text.splitlines()):
            if line.strip().startswith("{"):
                try:
                    data = json.loads(line)
                    break
                except ValueError:
                    continue
    if isinstance(data, list):
        data = next((m for m in reversed(data) if isinstance(m, dict) and m.get("type") == "result"), None)
    return data if isinstance(data, dict) and data.get("type") == "result" else None


def suggest_rule(denial: dict) -> str:
    """The allow rule that would have let a denied call through."""
    tool = str(denial.get("tool_name", "?"))
    command = (denial.get("tool_input") or {}).get("command")
    if tool == "Bash" and isinstance(command, str) and command.strip():
        try:
            words = shlex.split(command)
        except ValueError:
            words = command.split()
        head = words[:2] if len(words) > 1 and not words[1].startswith("-") else words[:1]
        return f"Bash({' '.join(head)}:*)"
    return tool


def verify_steps(task: Task, ctx: RunContext, report: dict | None) -> list[StepResult]:
    reported = (report or {}).get("steps", {})
    out = []
    for step in task.steps:
        said = reported.get(step.id)
        status = said["status"] if said and said["status"] in ("done", "blocked", "failed") else None
        note = said["note"] if said else ""
        if step.evidence is None:
            verdict = {"done": "reported", "blocked": "blocked", "failed": "failed", None: "missing"}[status]
            detail = "no independent check configured"
        else:
            check = check_evidence(step.evidence, ctx)
            detail = check.detail
            if check.ok:
                verdict = "verified"
            else:
                verdict = {"done": "unverified", "blocked": "blocked", "failed": "failed", None: "missing"}[status]
        out.append(StepResult(step.id, verdict, status, note, detail))
    return out


def _why(step: StepResult) -> str:
    if step.verdict == "unverified":
        return f"reported done, but the check failed: {step.evidence}"
    if step.verdict == "missing":
        return f"not reported. Check: {step.evidence}"
    return f"reported {step.reported}: {step.note or step.evidence}"


def _alert(task: Task, record: RunRecord) -> str:
    env = {**os.environ, "RUNGUARD_TASK": record.task, "RUNGUARD_STATUS": record.status,
           "RUNGUARD_REASON": record.reason, "RUNGUARD_RUN_DIR": record.run_dir,
           "RUNGUARD_FAILED_STEPS": ",".join(record.failed_steps)}
    try:
        done = subprocess.run(task.alert_command, shell=True, cwd=task.workdir, env=env, capture_output=True,
                              text=True, timeout=60)
        return "sent" if done.returncode == 0 else f"alert command exited {done.returncode}"
    except subprocess.TimeoutExpired:
        return "alert command timed out"


def run_task(task: Task, *, run_id: str | None = None) -> RunRecord:
    """Execute one run of a task. Never raises for run failures: the outcome is in the returned record."""
    from .store import save  # local import: store imports RunRecord

    started = datetime.now().astimezone()
    clock = time.monotonic()
    run_id = run_id or os.environ.get("RUNGUARD_RUN_ID") or f"{started:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
    task_dir = task.runs_dir / task.name
    run_dir = task_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    ctx = RunContext(run_id, run_dir, task.workdir, started)
    record = RunRecord(task=task.name, run_id=run_id, status="error", reason="", started_at=started.isoformat(timespec="seconds"),
                       model=task.model, effort=task.effort, run_dir=str(run_dir))

    lock = open(task_dir / ".lock", "w")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            record.status, record.reason = "skipped", "the previous run of this task is still going"
            return _finish(task, record, clock, save)

        flags = supported_flags(task.claude_bin)
        session_id = str(uuid.uuid4())
        prompt = build_prompt(task, ctx)
        deadline = clock + task.timeout_seconds
        env = {**os.environ, "RUNGUARD_RUN_ID": run_id, "RUNGUARD_RUN_DIR": str(run_dir)}
        costs: list[float] = []
        resume = False

        while True:
            record.attempts += 1
            n = record.attempts
            remaining = deadline - time.monotonic()
            if remaining <= 1:
                record.status, record.reason = "timeout", f"time limit of {task.timeout_seconds}s used up before attempt {n}"
                break
            argv = build_argv(task, ctx, flags, session_id, resume)
            (run_dir / f"attempt-{n}.prompt.txt").write_text(prompt, encoding="utf-8")
            (run_dir / f"attempt-{n}.argv.json").write_text(json.dumps(argv, indent=1), encoding="utf-8")
            try:
                ex = execute(argv, prompt, task.workdir, env, remaining)
            except FileNotFoundError:
                record.status = "error"
                record.reason = (f"executable not found: {task.claude_bin!r}. Set claude_bin in the task file or "
                                 "RUNGUARD_CLAUDE_BIN. Schedulers start with a minimal PATH.")
                break
            (run_dir / f"attempt-{n}.stdout.txt").write_text(ex.stdout, encoding="utf-8")
            (run_dir / f"attempt-{n}.stderr.txt").write_text(ex.stderr, encoding="utf-8")
            record.exit_codes.append(ex.exit_code)

            result = parse_result(ex.stdout)
            report = parse_report(result)
            if result:
                session_id = result.get("session_id") or session_id
                record.session_id = session_id
                if isinstance(result.get("total_cost_usd"), (int, float)):
                    costs.append(float(result["total_cost_usd"]))
                if isinstance(result.get("num_turns"), int):
                    record.num_turns = (record.num_turns or 0) + result["num_turns"]
                for model in (result.get("modelUsage") or {}):
                    if model not in record.models_used:
                        record.models_used.append(model)
                for denial in result.get("permission_denials") or []:
                    record.permission_denials.append({
                        "tool": denial.get("tool_name"), "input": denial.get("tool_input"),
                        "suggested_allow_rule": suggest_rule(denial),
                    })
                if report:
                    record.summary = report["summary"]
            record.steps = verify_steps(task, ctx, report)

            if ex.timed_out:
                record.status = "timeout"
                record.reason = f"stopped after the {task.timeout_seconds}s time limit; the process group was killed"
                break
            if result is None:
                tail = (ex.stderr.strip() or ex.stdout.strip()).splitlines()[-1:] or ["no output"]
                record.status, record.reason = "error", f"no result from the CLI (exit code {ex.exit_code}): {tail[0][:300]}"
                break
            if result.get("is_error") or ex.exit_code not in (0, None):
                why = result.get("terminal_reason") or result.get("subtype") or f"exit code {ex.exit_code}"
                record.status, record.reason = "error", f"{why}: {str(result.get('result', ''))[:300]}"
                break

            bad = [s for s in record.steps if s.verdict not in OK_VERDICTS]
            if not bad:
                record.status, record.reason = "ok", "every step verified" if task.steps else "finished"
                break
            retryable = [s for s in bad if s.verdict != "blocked"]
            if not retryable or n > task.retries:
                blocked = any(s.verdict == "blocked" for s in bad) or bool(record.permission_denials)
                record.status = "blocked" if blocked else "incomplete"
                record.reason = "; ".join(f"[{s.id}] {_why(s)}" for s in bad)
                break
            prompt = build_resume_prompt(task, [(s.id, _why(s)) for s in retryable],
                                         [s.id for s in record.steps if s.verdict in OK_VERDICTS])
            resume = True

        record.cost_usd = round(sum(costs), 6) if costs else None
        return _finish(task, record, clock, save)
    finally:
        lock.close()


def _finish(task: Task, record: RunRecord, clock: float, save) -> RunRecord:
    record.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
    record.duration_s = round(time.monotonic() - clock, 2)
    if task.alert_command and record.status not in ("ok", "skipped"):
        record.alert = _alert(task, record)
    save(task, record)
    return record
