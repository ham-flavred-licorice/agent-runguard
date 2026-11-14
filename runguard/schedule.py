"""Turn a task's cron expression into a macOS launchd job or a crontab line."""
from __future__ import annotations

import itertools
import plistlib
import re
import shlex
import sys
from pathlib import Path

from .config import Task

FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day", 1, 31), ("month", 1, 12), ("weekday", 0, 7))
NAMES = {
    "month": {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1)},
    "weekday": {d: i for i, d in enumerate("sun mon tue wed thu fri sat".split())},
}
MAX_LAUNCHD_ENTRIES = 300


class ScheduleError(ValueError):
    pass


def _value(token: str, name: str, lo: int, hi: int) -> int:
    token = token.lower()
    if token in NAMES.get(name, {}):
        return NAMES[name][token]
    if not token.isdigit() or not lo <= int(token) <= hi:
        raise ScheduleError(f"{name}: {token!r} is not between {lo} and {hi}")
    return int(token)


def parse_cron(expr: str) -> dict[str, list[int] | None]:
    """Parse a five-field cron expression. A field that is '*' comes back as None."""
    parts = expr.split()
    if len(parts) != 5:
        raise ScheduleError(f"expected 5 fields (minute hour day month weekday), got {len(parts)}: {expr!r}")
    out: dict[str, list[int] | None] = {}
    for (name, lo, hi), part in zip(FIELDS, parts):
        if part == "*":
            out[name] = None
            continue
        values: set[int] = set()
        for piece in part.split(","):
            m = re.fullmatch(r"(\*|[\w]+(?:-[\w]+)?)(?:/(\d+))?", piece)
            if not m:
                raise ScheduleError(f"{name}: cannot read {piece!r}")
            span, step = m.group(1), int(m.group(2) or 1)
            if step < 1:
                raise ScheduleError(f"{name}: step must be at least 1")
            if span == "*":
                a, b = lo, hi if name != "weekday" else 6
            elif "-" in span:
                a, b = (_value(x, name, lo, hi) for x in span.split("-"))
                if a > b:
                    raise ScheduleError(f"{name}: range {span!r} runs backwards")
            else:
                a = b = _value(span, name, lo, hi)
                if m.group(2):
                    b = hi if name != "weekday" else 6
            values.update(range(a, b + 1, step))
        if name == "weekday":
            values = {0 if v == 7 else v for v in values}
        out[name] = sorted(values)
    return out


def cron_to_launchd(expr: str) -> list[dict[str, int]]:
    """StartCalendarInterval entries equivalent to a cron expression.

    Cron fires when EITHER the day-of-month or the weekday matches if both are restricted;
    a launchd entry requires every key to match. So that case becomes two groups of entries.
    """
    f = parse_cron(expr)
    keymap = {"minute": "Minute", "hour": "Hour", "day": "Day", "month": "Month", "weekday": "Weekday"}

    def expand(names: list[str]) -> list[dict[str, int]]:
        active = [n for n in names if f[n] is not None]
        combos = itertools.product(*(f[n] for n in active)) if active else [()]
        return [{keymap[n]: v for n, v in zip(active, combo)} for combo in combos]

    base = ["minute", "hour", "month"]
    if f["day"] is not None and f["weekday"] is not None:
        entries = expand(base + ["day"]) + expand(base + ["weekday"])
    else:
        entries = expand(base + ["day", "weekday"])
    if len(entries) > MAX_LAUNCHD_ENTRIES:
        raise ScheduleError(f"{expr!r} expands to {len(entries)} launchd entries (limit {MAX_LAUNCHD_ENTRIES}); simplify it")
    return entries


def label_for(task: Task) -> str:
    return "com.runguard." + re.sub(r"[^A-Za-z0-9.-]", "-", task.name)


def _command(task: Task, python: str) -> list[str]:
    return [python, "-m", "runguard", "run", str(task.path)]


def render_plist(task: Task, *, python: str | None = None, path_env: str, package_root: Path) -> bytes:
    """A LaunchAgent that runs the task on its schedule.

    launchd starts jobs with a minimal PATH, which is the usual reason a task "works in a
    live session but not on a schedule". The PATH captured at install time is written in.
    """
    if not task.schedule:
        raise ScheduleError(f"task {task.name} has no schedule")
    log_dir = task.runs_dir / task.name
    plist = {
        "Label": label_for(task),
        "ProgramArguments": _command(task, python or sys.executable),
        "WorkingDirectory": str(task.workdir),
        "EnvironmentVariables": {"PATH": path_env, "PYTHONPATH": str(package_root)},
        "StartCalendarInterval": cron_to_launchd(task.schedule),
        "StandardOutPath": str(log_dir / "launchd.out.log"),
        "StandardErrorPath": str(log_dir / "launchd.err.log"),
        "RunAtLoad": False,
    }
    return plistlib.dumps(plist, sort_keys=False)


def render_cron_line(task: Task, *, python: str | None = None, path_env: str, package_root: Path) -> str:
    if not task.schedule:
        raise ScheduleError(f"task {task.name} has no schedule")
    parse_cron(task.schedule)
    log = task.runs_dir / task.name / "cron.log"
    command = " ".join(shlex.quote(x) for x in _command(task, python or sys.executable))
    return (f"{task.schedule} cd {shlex.quote(str(task.workdir))} && PATH={shlex.quote(path_env)} "
            f"PYTHONPATH={shlex.quote(str(package_root))} {command} >> {shlex.quote(str(log))} 2>&1")
