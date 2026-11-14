"""Task files: one TOML (or JSON) file per scheduled task."""
from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PERMISSION_MODES = ("dontAsk", "acceptEdits", "auto", "manual", "plan", "bypassPermissions")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
STEP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


class ConfigError(Exception):
    """Raised with every problem found in a task file, not just the first."""

    def __init__(self, path: Path, problems: list[str]):
        self.path, self.problems = path, problems
        super().__init__(f"{path}: " + "; ".join(problems))


@dataclass
class Evidence:
    """What must exist after a step for it to count as done."""

    file: str | None = None        # path that must exist, be fresh and non-empty
    glob: str | None = None        # pattern that must match at least min_count fresh files
    min_count: int = 1
    contains: str | None = None    # text the file must contain
    json: bool = False             # the file must parse as JSON
    min_bytes: int = 1
    command: str | None = None     # shell command that must exit 0
    allow_stale: bool = False      # accept a file that already existed before this run

    @property
    def kind(self) -> str:
        return "file" if self.file else "glob" if self.glob else "command" if self.command else "none"

    def describe(self) -> str:
        if self.file:
            extra = [f"contains {self.contains!r}"] if self.contains else []
            extra += ["valid JSON"] if self.json else []
            return f"file {self.file}" + (f" ({', '.join(extra)})" if extra else "")
        if self.glob:
            return f"at least {self.min_count} file(s) matching {self.glob}"
        return f"command exits 0: {self.command}"


@dataclass
class Step:
    id: str
    do: str
    evidence: Evidence | None = None


@dataclass
class Task:
    name: str
    path: Path
    workdir: Path
    runs_dir: Path
    prompt: str
    steps: list[Step]
    description: str = ""
    prompt_file: Path | None = None
    schedule: str | None = None
    model: str | None = None
    effort: str | None = None
    fallback_model: str | None = None
    timeout_seconds: int = 20 * 60
    timeout_is_default: bool = True
    max_budget_usd: float | None = None
    retries: int = 1
    permission_mode: str = "dontAsk"
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    mcp_config: Path | None = None
    add_dirs: list[str] = field(default_factory=list)
    extra_args: list[str] = field(default_factory=list)
    alert_command: str | None = None
    claude_bin: str = "claude"


TASK_KEYS = {
    "name", "description", "schedule", "workdir", "runs_dir", "prompt", "prompt_file", "model", "effort",
    "fallback_model", "timeout_minutes", "max_budget_usd", "retries", "permission_mode", "allowed_tools",
    "disallowed_tools", "mcp_config", "add_dirs", "extra_args", "steps", "alert", "claude_bin",
}
EVIDENCE_KEYS = {"file", "glob", "min_count", "contains", "json", "min_bytes", "command", "allow_stale"}


def _read(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return json.loads(text)
    return tomllib.loads(text)


def load_task(path: str | os.PathLike) -> Task:
    path = Path(path).expanduser().resolve()
    problems: list[str] = []
    try:
        raw = _read(path)
    except FileNotFoundError:
        raise ConfigError(path, ["file not found"]) from None
    except (tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(path, [f"cannot parse: {exc}"]) from None

    for key in sorted(set(raw) - TASK_KEYS):
        problems.append(f"unknown key {key!r}")

    name = raw.get("name") or path.stem
    if not NAME_RE.match(str(name)):
        problems.append(f"name {name!r} must be lowercase letters, digits, '.', '_' or '-'")

    base = path.parent
    workdir = (base / os.path.expanduser(str(raw.get("workdir", ".")))).resolve()
    if not workdir.is_dir():
        problems.append(f"workdir {workdir} does not exist")
    runs_dir = Path(os.environ.get("RUNGUARD_RUNS_DIR") or (base / os.path.expanduser(str(raw.get("runs_dir", "runs"))))).resolve()

    prompt, prompt_file = raw.get("prompt"), None
    if raw.get("prompt_file"):
        if prompt:
            problems.append("give either prompt or prompt_file, not both")
        prompt_file = (base / str(raw["prompt_file"])).resolve()
        try:
            prompt = prompt_file.read_text(encoding="utf-8")
        except OSError as exc:
            problems.append(f"prompt_file: {exc}")
    if not (prompt or "").strip():
        problems.append("prompt is empty (set prompt or prompt_file)")

    steps: list[Step] = []
    seen: set[str] = set()
    for i, item in enumerate(raw.get("steps") or [], start=1):
        sid = str(item.get("id", ""))
        if not STEP_ID_RE.match(sid):
            problems.append(f"step {i}: id {sid!r} must be lowercase letters, digits, '_' or '-'")
        if sid in seen:
            problems.append(f"step {i}: duplicate id {sid!r}")
        seen.add(sid)
        if not str(item.get("do", "")).strip():
            problems.append(f"step {sid or i}: 'do' is empty")
        for key in sorted(set(item) - {"id", "do", "evidence"}):
            problems.append(f"step {sid or i}: unknown key {key!r}")
        evidence = None
        if item.get("evidence"):
            ev = item["evidence"]
            for key in sorted(set(ev) - EVIDENCE_KEYS):
                problems.append(f"step {sid or i}: unknown evidence key {key!r}")
            evidence = Evidence(**{k: v for k, v in ev.items() if k in EVIDENCE_KEYS})
            if sum(bool(x) for x in (evidence.file, evidence.glob, evidence.command)) != 1:
                problems.append(f"step {sid or i}: evidence needs exactly one of file, glob, command")
        steps.append(Step(id=sid, do=str(item.get("do", "")).strip(), evidence=evidence))

    mode = raw.get("permission_mode", "dontAsk")
    if mode not in PERMISSION_MODES:
        problems.append(f"permission_mode must be one of {', '.join(PERMISSION_MODES)}")
    effort = raw.get("effort")
    if effort is not None and effort not in EFFORTS:
        problems.append(f"effort must be one of {', '.join(EFFORTS)}")
    timeout_minutes = raw.get("timeout_minutes", 20)
    if not isinstance(timeout_minutes, (int, float)) or timeout_minutes <= 0:
        problems.append("timeout_minutes must be a positive number")
        timeout_minutes = 20
    retries = raw.get("retries", 1)
    if not isinstance(retries, int) or not 0 <= retries <= 5:
        problems.append("retries must be an integer from 0 to 5")
        retries = 1
    for key in ("allowed_tools", "disallowed_tools", "add_dirs", "extra_args"):
        if not all(isinstance(x, str) for x in raw.get(key, [])):
            problems.append(f"{key} must be a list of strings")
    mcp_config = (base / str(raw["mcp_config"])).resolve() if raw.get("mcp_config") else None
    if mcp_config and not mcp_config.is_file():
        problems.append(f"mcp_config {mcp_config} does not exist")
    alert = raw.get("alert") or {}
    for key in sorted(set(alert) - {"command"}):
        problems.append(f"alert: unknown key {key!r}")

    if problems:
        raise ConfigError(path, problems)
    return Task(
        name=str(name), path=path, workdir=workdir, runs_dir=runs_dir, prompt=prompt.strip(), steps=steps,
        description=str(raw.get("description", "")), prompt_file=prompt_file, schedule=raw.get("schedule"),
        model=raw.get("model"), effort=effort, fallback_model=raw.get("fallback_model"),
        timeout_seconds=int(timeout_minutes * 60), timeout_is_default="timeout_minutes" not in raw,
        max_budget_usd=raw.get("max_budget_usd"), retries=retries, permission_mode=mode,
        allowed_tools=list(raw.get("allowed_tools", [])), disallowed_tools=list(raw.get("disallowed_tools", [])),
        mcp_config=mcp_config, add_dirs=list(raw.get("add_dirs", [])), extra_args=list(raw.get("extra_args", [])),
        alert_command=alert.get("command"),
        claude_bin=os.environ.get("RUNGUARD_CLAUDE_BIN") or raw.get("claude_bin", "claude"),
    )
