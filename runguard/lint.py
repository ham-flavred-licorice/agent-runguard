"""Static checks on a task file: the mistakes that make unattended runs unreliable."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Task
from .schedule import ScheduleError, parse_cron

PROMPT_WARN_LINES = 150
PROMPT_WARN_CHARS = 8000
MAX_STEPS = 12
DYNAMIC_PATH = re.compile(r"\{(run_dir|run_id|date)\}")


@dataclass
class Issue:
    level: str   # error | warning | info
    code: str
    message: str


def lint_task(task: Task) -> list[Issue]:
    out: list[Issue] = []

    def add(level: str, code: str, message: str) -> None:
        out.append(Issue(level, code, message))

    lines, chars = task.prompt.count("\n") + 1, len(task.prompt)
    if lines > PROMPT_WARN_LINES or chars > PROMPT_WARN_CHARS:
        add("warning", "long-prompt",
            f"the prompt is {lines} lines / {chars} characters. Long instruction files are followed less reliably. "
            "Keep the prompt to the goal and the rules; move reference material into files the task reads when needed, "
            "and move the steps into [[steps]] so they are checked.")
    if not task.steps:
        add("error", "no-steps", "no [[steps]] defined, so nothing can be verified after a run")
    if len(task.steps) > MAX_STEPS:
        add("warning", "many-steps", f"{len(task.steps)} steps in one task; split it so a failure is cheap to rerun")
    for step in task.steps:
        if step.evidence is None:
            add("warning", "unverifiable-step",
                f"step [{step.id}] has no evidence, so runguard has to take the agent's word that it happened")
        elif step.evidence.kind in ("file", "glob"):
            target = step.evidence.file or step.evidence.glob or ""
            if step.evidence.allow_stale:
                add("warning", "stale-evidence",
                    f"step [{step.id}] accepts a file from an earlier run (allow_stale), so a skipped step can pass")
            elif not DYNAMIC_PATH.search(target):
                add("info", "static-evidence-path",
                    f"step [{step.id}] writes to a fixed path; it only counts when rewritten during the run. "
                    "A path under {run_dir} keeps each run's output.")
        if len(step.do) > 400:
            add("warning", "long-step", f"step [{step.id}] is {len(step.do)} characters; one action per step works best")

    if task.permission_mode == "bypassPermissions":
        add("warning", "bypass-permissions",
            "permission_mode = bypassPermissions lets the agent run anything unattended. Use dontAsk with an allow list.")
    for rule in task.allowed_tools:
        if rule.strip() in ("Bash", "Bash(*)", "Bash(:*)"):
            add("warning", "blanket-bash", "allowed_tools grants every shell command. Allow specific commands, e.g. Bash(python3 scripts/fetch.py:*)")
    if task.permission_mode == "dontAsk" and not task.allowed_tools:
        add("info", "nothing-allowed",
            "permission_mode = dontAsk with an empty allowed_tools: any tool that needs approval will be denied")
    if not task.model:
        add("info", "no-model", "no model set; the CLI default is used. Set model (and effort) per task to control cost and quality")
    if task.timeout_is_default:
        add("info", "default-timeout", "no timeout_minutes set; the default of 20 minutes applies")
    if task.schedule:
        try:
            parse_cron(task.schedule)
        except ScheduleError as exc:
            add("error", "bad-schedule", str(exc))
    else:
        add("info", "no-schedule", "no schedule set; the task only runs when started by hand")
    if not task.alert_command:
        add("info", "no-alert", "no [alert] command; a failed run is recorded but nobody is told")
    return out
