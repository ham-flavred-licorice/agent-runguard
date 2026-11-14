"""Command line: run, lint, status, report, schedule, doctor."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError, load_task
from .lint import lint_task
from .runner import OPTIONAL_FLAGS, OK_VERDICTS, run_task, supported_flags
from .schedule import ScheduleError, label_for, render_cron_line, render_plist
from .store import load_history, render_markdown, render_table, summarize

EXIT = {"ok": 0, "skipped": 0, "incomplete": 1, "blocked": 1, "error": 2, "timeout": 124}
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def _load(path: str):
    try:
        return load_task(path)
    except ConfigError as exc:
        print(f"{exc.path}:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  error: {problem}", file=sys.stderr)
        return None


def cmd_run(args) -> int:
    task = _load(args.task)
    if task is None:
        return 2
    record = run_task(task)
    mark = {"verified": "ok ", "reported": "ok*"}
    short = lambda text: text.replace(record.run_dir, "{run_dir}")  # noqa: E731
    print(f"{record.task}  {record.status.upper()}  {record.duration_s}s  attempts={record.attempts}  {short(record.reason)}")
    for s in record.steps:
        print(f"  [{mark.get(s.verdict, '!! ')}] {s.id}: {s.verdict} ({short(s.evidence)})")
    if any(s.verdict == "reported" for s in record.steps):
        print("  ok* = taken on the agent's word; add evidence to the step to verify it")
    for d in record.permission_denials:
        print(f"  denied: {d['tool']}  ->  to allow it, add {d['suggested_allow_rule']!r} to allowed_tools")
    print(f"  record: {record.run_dir}/run.json")
    return EXIT.get(record.status, 2)


def cmd_lint(args) -> int:
    worst = 0
    for path in args.tasks:
        task = _load(path)
        if task is None:
            worst = max(worst, 2)
            continue
        issues = lint_task(task)
        shown = [i for i in issues if args.verbose or i.level != "info"]
        print(f"{task.path.name}: " + ("clean" if not shown else f"{len(shown)} issue(s)"))
        for i in shown:
            print(f"  {i.level}: [{i.code}] {i.message}")
        if any(i.level == "error" for i in issues) or (args.strict and any(i.level == "warning" for i in issues)):
            worst = max(worst, 1)
    return worst


def cmd_status(args) -> int:
    print(render_table(summarize(load_history(Path(args.runs_dir), args.task), args.last)))
    return 0


def cmd_report(args) -> int:
    text = render_markdown(load_history(Path(args.runs_dir), args.task), args.last)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


def cmd_schedule(args) -> int:
    path_env = args.path or os.environ.get("PATH", "")
    code = 0
    for item in args.tasks:
        task = _load(item)
        if task is None:
            code = 2
            continue
        try:
            if args.format == "cron":
                print(render_cron_line(task, python=args.python, path_env=path_env, package_root=PACKAGE_ROOT))
                continue
            data = render_plist(task, python=args.python, path_env=path_env, package_root=PACKAGE_ROOT)
        except ScheduleError as exc:
            print(f"{task.path.name}: {exc}", file=sys.stderr)
            code = 2
            continue
        if not args.write:
            sys.stdout.write(data.decode())
            continue
        target = Path(args.write).expanduser() / f"{label_for(task)}.plist"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        (task.runs_dir / task.name).mkdir(parents=True, exist_ok=True)
        print(f"wrote {target}\n  load it with:   launchctl bootstrap gui/$(id -u) {target}"
              f"\n  remove it with: launchctl bootout gui/$(id -u)/{label_for(task)}")
    return code


def cmd_doctor(args) -> int:
    binary = os.environ.get("RUNGUARD_CLAUDE_BIN") or args.claude_bin
    found = shutil.which(binary)
    print(f"python:  {sys.version.split()[0]} ({sys.executable})")
    print(f"claude:  {found or 'NOT FOUND on PATH'}")
    if not found:
        print("  Install Claude Code, or point claude_bin / RUNGUARD_CLAUDE_BIN at the executable.")
        return 1
    version = subprocess.run([found, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    print(f"version: {version}")
    flags = supported_flags(found)
    if flags is None:
        print("flags:   could not read `claude --help`; every flag will be assumed to exist")
        return 0
    missing = [f for f in OPTIONAL_FLAGS if f not in flags]
    print("flags:   all optional flags available" if not missing else
          f"flags:   not available in this version and therefore left out: {', '.join(missing)}")
    print(f"PATH seen by this process (a scheduler may see less):\n  {os.environ.get('PATH', '')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="runguard", description="Run scheduled Claude Code tasks unattended and verify every step.")
    parser.add_argument("--version", action="version", version=f"runguard {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="run one task now")
    p.add_argument("task")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("lint", help="check task files for reliability problems")
    p.add_argument("tasks", nargs="+")
    p.add_argument("--strict", action="store_true", help="warnings fail too")
    p.add_argument("-v", "--verbose", action="store_true", help="show info-level notes")
    p.set_defaults(fn=cmd_lint)

    for name, fn, text in (("status", cmd_status, "success rates from the run history"),
                           ("report", cmd_report, "Markdown status page from the run history")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--runs-dir", default="runs")
        p.add_argument("--task")
        p.add_argument("--last", type=int, default=30, help="runs per task to include")
        if name == "report":
            p.add_argument("--out")
        p.set_defaults(fn=fn)

    p = sub.add_parser("schedule", help="print (or write) launchd jobs or crontab lines for task files")
    p.add_argument("tasks", nargs="+")
    p.add_argument("--format", choices=("launchd", "cron"), default="launchd")
    p.add_argument("--write", metavar="DIR", help="launchd only: write the .plist files here, e.g. ~/Library/LaunchAgents")
    p.add_argument("--python", help="python executable the job should use (default: this one)")
    p.add_argument("--path", help="PATH for the job (default: the current PATH)")
    p.set_defaults(fn=cmd_schedule)

    p = sub.add_parser("doctor", help="check the Claude Code install and which flags it supports")
    p.add_argument("--claude-bin", default="claude")
    p.set_defaults(fn=cmd_doctor)

    args = parser.parse_args(argv)
    return args.fn(args)
