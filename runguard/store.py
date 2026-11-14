"""Run records on disk, and the status summaries built from them."""
from __future__ import annotations

import json
import statistics
from collections import Counter
from pathlib import Path

from .config import Task
from .runner import RunRecord


def save(task: Task, record: RunRecord) -> None:
    run_dir = Path(record.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps(record.to_dict(), indent=2) + "\n", encoding="utf-8")
    line = {
        "task": record.task, "run_id": record.run_id, "status": record.status, "reason": record.reason,
        "started_at": record.started_at, "duration_s": record.duration_s, "attempts": record.attempts,
        "cost_usd": record.cost_usd, "failed_steps": record.failed_steps,
        "steps_total": len(record.steps), "model": record.model, "models_used": record.models_used,
    }
    with open(task.runs_dir / task.name / "history.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")


def load_history(runs_dir: Path, task: str | None = None) -> list[dict]:
    rows = []
    for path in sorted(Path(runs_dir).glob("*/history.jsonl")):
        if task and path.parent.name != task:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue  # a torn line must not break reporting
    return sorted(rows, key=lambda r: (r.get("started_at", ""), r.get("run_id", "")))


def summarize(history: list[dict], last: int = 30) -> list[dict]:
    """One row per task over its most recent `last` runs. Skipped (overlap) runs do not count against it."""
    by_task: dict[str, list[dict]] = {}
    for row in history:
        by_task.setdefault(row["task"], []).append(row)
    out = []
    for name in sorted(by_task):
        runs = by_task[name][-last:]
        counted = [r for r in runs if r["status"] != "skipped"]
        ok = [r for r in counted if r["status"] == "ok"]
        failures = Counter(r["status"] for r in counted if r["status"] != "ok")
        streak = 0
        for r in reversed(counted):
            if r["status"] != "ok":
                break
            streak += 1
        costs = [r["cost_usd"] for r in counted if isinstance(r.get("cost_usd"), (int, float))]
        out.append({
            "task": name, "runs": len(counted), "ok": len(ok),
            "success_rate": round(len(ok) / len(counted), 4) if counted else None,
            "ok_streak": streak,
            "last_status": runs[-1]["status"], "last_started": runs[-1]["started_at"],
            "last_reason": runs[-1].get("reason", ""),
            "median_duration_s": round(statistics.median(r["duration_s"] for r in counted), 1) if counted else None,
            "first_try": sum(1 for r in ok if r.get("attempts") == 1),
            "recovered_on_resume": sum(1 for r in ok if (r.get("attempts") or 1) > 1),
            "total_cost_usd": round(sum(costs), 4) if costs else None,
            "failures": dict(failures),
        })
    return out


def _pct(rate: float | None) -> str:
    return "–" if rate is None else f"{rate * 100:.0f}%"


def render_table(rows: list[dict]) -> str:
    if not rows:
        return "No runs recorded yet."
    head = ("TASK", "RUNS", "OK", "RATE", "STREAK", "LAST", "LAST RUN", "MEDIAN")
    body = [(r["task"], str(r["runs"]), str(r["ok"]), _pct(r["success_rate"]), str(r["ok_streak"]), r["last_status"],
             r["last_started"][:16].replace("T", " "), f'{r["median_duration_s"]}s') for r in rows]
    widths = [max(len(x[i]) for x in [head, *body]) for i in range(len(head))]
    return "\n".join("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(line)).rstrip() for line in [head, *body])


def render_markdown(history: list[dict], last: int = 30, recent: int = 15) -> str:
    rows = summarize(history, last)
    out = ["# Scheduled task status", ""]
    if not rows:
        return "\n".join(out + ["No runs recorded yet.", ""])
    counted = [r for r in history if r["status"] != "skipped"]
    ok = sum(1 for r in counted if r["status"] == "ok")
    out += [f"{len(counted)} runs recorded across {len(rows)} task(s); {ok} finished with every step verified "
            f"({_pct(ok / len(counted) if counted else None)}).", ""]
    out += [f"## By task (last {last} runs each)", "",
            "| Task | Runs | Verified | Success rate | Current streak | First try | Fixed on resume | Median time | Cost | Last run |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        cost = "–" if r["total_cost_usd"] is None else f'${r["total_cost_usd"]:.2f}'
        out.append(f'| {r["task"]} | {r["runs"]} | {r["ok"]} | {_pct(r["success_rate"])} | {r["ok_streak"]} | '
                   f'{r["first_try"]} | {r["recovered_on_resume"]} | {r["median_duration_s"]}s | {cost} | '
                   f'{r["last_status"]} at {r["last_started"][:16].replace("T", " ")} |')
    bad = [r for r in history if r["status"] not in ("ok", "skipped")][-recent:]
    out += ["", f"## Most recent problems (up to {recent})", ""]
    if not bad:
        out.append("None.")
    else:
        out += ["| When | Task | Status | What happened |", "|---|---|---|---|"]
        for r in reversed(bad):
            reason = str(r.get("reason", "")).replace("|", "\\|").replace("\n", " ")[:220]
            out.append(f'| {r["started_at"][:16].replace("T", " ")} | {r["task"]} | {r["status"]} | {reason} |')
    return "\n".join(out) + "\n"
