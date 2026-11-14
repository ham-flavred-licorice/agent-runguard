"""The run contract: what the agent is told, and how its final report is read back."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import Task

REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "status": {"type": "string", "enum": ["done", "blocked", "failed"]},
                    "note": {"type": "string"},
                },
                "required": ["id", "status"],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["steps", "summary"],
}


@dataclass
class RunContext:
    run_id: str
    run_dir: Path
    workdir: Path
    started_at: datetime

    def expand(self, text: str) -> str:
        """Fill the placeholders allowed in step text and evidence paths."""
        return (
            text.replace("{run_dir}", str(self.run_dir))
            .replace("{run_id}", self.run_id)
            .replace("{workdir}", str(self.workdir))
            .replace("{date}", self.started_at.strftime("%Y-%m-%d"))
        )


def build_prompt(task: Task, ctx: RunContext) -> str:
    lines = [task.prompt, "", "---", "RUN CONTRACT (checked automatically after you finish)", f"Run id: {ctx.run_id}",
             f"Run directory: {ctx.run_dir}", ""]
    if task.steps:
        lines += [
            "Complete every step below, in order. After you finish, each step is checked against its evidence "
            "by a separate program. A step without its evidence counts as not done, whatever you report.",
            "",
        ]
        for n, step in enumerate(task.steps, start=1):
            lines.append(f"{n}. [{step.id}] {ctx.expand(step.do)}")
            if step.evidence:
                lines.append(f"   Evidence checked: {ctx.expand(step.evidence.describe())}")
        ids = ", ".join(s.id for s in task.steps)
        lines += [
            "",
            "Rules:",
            "- Do not skip a step. If a step is impossible (a tool is denied, an input is missing), stop working on "
            'it and report it as "blocked" with the reason.',
            '- Do not report a step as "done" unless its evidence exists.',
            "- Your final message must be one JSON object and nothing else: "
            '{"steps": [{"id": "...", "status": "done" | "blocked" | "failed", "note": "..."}], "summary": "..."} '
            f"with one entry for each of these step ids: {ids}.",
        ]
    return "\n".join(lines) + "\n"


def build_resume_prompt(task: Task, problems: list[tuple[str, str]], verified: list[str]) -> str:
    lines = ["The run contract check did not pass. These steps are not done:"]
    lines += [f"- [{sid}] {why}" for sid, why in problems]
    lines += ["", "Complete only these steps now."]
    if verified:
        lines.append(f"Do not redo the steps that already passed: {', '.join(verified)}.")
    ids = ", ".join(s.id for s in task.steps)
    lines.append(f"Then reply with the same JSON object as before, with one entry for each step id: {ids}.")
    return "\n".join(lines) + "\n"


_FENCE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.S)


def parse_report(result: dict | None) -> dict | None:
    """Return {"steps": {id: {"status", "note"}}, "summary": str} from a CLI result, or None."""
    if not isinstance(result, dict):
        return None
    candidates = [result.get("structured_output")]
    text = result.get("result")
    if isinstance(text, str):
        fenced = _FENCE.search(text)
        first, last = text.find("{"), text.rfind("}")
        candidates += [text, fenced.group(1) if fenced else None, text[first:last + 1] if 0 <= first < last else None]
    for cand in candidates:
        if isinstance(cand, str):
            try:
                cand = json.loads(cand)
            except ValueError:
                continue
        if isinstance(cand, dict) and isinstance(cand.get("steps"), list):
            steps = {}
            for item in cand["steps"]:
                if isinstance(item, dict) and item.get("id") is not None:
                    steps[str(item["id"])] = {"status": str(item.get("status", "")), "note": str(item.get("note", ""))}
            return {"steps": steps, "summary": str(cand.get("summary", ""))}
    return None
