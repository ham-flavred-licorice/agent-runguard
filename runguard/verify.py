"""Independent evidence checks. Nothing here trusts what the agent said about itself."""
from __future__ import annotations

import glob as globlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Evidence
from .contract import RunContext

FRESH_TOLERANCE_SECONDS = 2.0
COMMAND_TIMEOUT_SECONDS = 120


@dataclass
class Check:
    ok: bool
    detail: str


def _fresh(path: Path, ctx: RunContext) -> bool:
    return path.stat().st_mtime >= ctx.started_at.timestamp() - FRESH_TOLERANCE_SECONDS


def check_evidence(ev: Evidence, ctx: RunContext) -> Check:
    if ev.file:
        path = Path(ctx.expand(ev.file))
        path = path if path.is_absolute() else ctx.workdir / path
        if not path.is_file():
            return Check(False, f"{path} does not exist")
        if not ev.allow_stale and not _fresh(path, ctx):
            return Check(False, f"{path} exists but was not written during this run")
        size = path.stat().st_size
        if size < ev.min_bytes:
            return Check(False, f"{path} is {size} bytes (needs at least {ev.min_bytes})")
        if ev.contains or ev.json:
            text = path.read_text(encoding="utf-8", errors="replace")
            if ev.contains and ev.contains not in text:
                return Check(False, f"{path} does not contain {ev.contains!r}")
            if ev.json:
                try:
                    json.loads(text)
                except ValueError as exc:
                    return Check(False, f"{path} is not valid JSON ({exc})")
        return Check(True, f"{path} ({size} bytes)")

    if ev.glob:
        pattern = ctx.expand(ev.glob)
        pattern = pattern if Path(pattern).is_absolute() else str(ctx.workdir / pattern)
        matches = [Path(p) for p in globlib.glob(pattern, recursive=True) if Path(p).is_file()]
        usable = [p for p in matches if p.stat().st_size >= ev.min_bytes and (ev.allow_stale or _fresh(p, ctx))]
        if len(usable) < ev.min_count:
            stale = len(matches) - len(usable)
            hint = f" ({stale} older or empty file(s) ignored)" if stale else ""
            return Check(False, f"{len(usable)} fresh file(s) match {pattern}, need {ev.min_count}{hint}")
        return Check(True, f"{len(usable)} file(s) match {pattern}")

    if ev.command:
        command = ctx.expand(ev.command)
        try:
            done = subprocess.run(command, shell=True, cwd=ctx.workdir, capture_output=True, text=True,
                                  timeout=COMMAND_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return Check(False, f"check command timed out after {COMMAND_TIMEOUT_SECONDS}s: {command}")
        tail = (done.stdout.strip() or done.stderr.strip()).splitlines()[-1:] or [""]
        if done.returncode != 0:
            return Check(False, f"check command exited {done.returncode}: {command} ({tail[0][:160]})")
        return Check(True, f"check command passed: {command}")

    return Check(False, "no evidence configured")
