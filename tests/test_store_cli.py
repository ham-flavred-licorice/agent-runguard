import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from support import ALL, ROOT, Case
from runguard.runner import run_task
from runguard.store import load_history, render_markdown, render_table, summarize


def cli(*args, cwd=None):
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    return subprocess.run([sys.executable, "-m", "runguard", *args], capture_output=True, text=True, env=env, cwd=cwd)


class History(Case):
    def test_success_rate_streak_and_recovery_counts(self):
        task = self.task(extra="retries = 1\n")
        plan = [
            [{"do": ALL}],                                   # ok, first try
            [{"do": ["fetch", "summary"]}, {"do": ["draft"]}],  # ok, fixed on resume
            [{"do": ["fetch"]}],                             # incomplete
            [{"do": ALL}],                                   # ok
            [{"do": ALL}],                                   # ok
        ]
        for i, acts in enumerate(plan):
            self.log.unlink(missing_ok=True)
            self.script(*acts)
            run_task(task, run_id=f"run-{i}")
        history = load_history(task.runs_dir)
        self.assertEqual([r["status"] for r in history], ["ok", "ok", "incomplete", "ok", "ok"])
        row = summarize(history)[0]
        self.assertEqual((row["runs"], row["ok"], row["success_rate"], row["ok_streak"]), (5, 4, 0.8, 2))
        self.assertEqual((row["first_try"], row["recovered_on_resume"], row["failures"]), (3, 1, {"incomplete": 1}))
        self.assertIn("80%", render_table(summarize(history)))
        page = render_markdown(history)
        self.assertIn("| digest | 5 | 4 | 80% | 2 | 3 | 1 |", page)
        self.assertIn("incomplete", page)

    def test_torn_history_line_does_not_break_reporting(self):
        task = self.task()
        self.script({"do": ALL})
        run_task(task)
        with open(task.runs_dir / task.name / "history.jsonl", "a") as fh:
            fh.write('{"task": "digest", "status": "o')
        self.assertEqual(len(load_history(task.runs_dir)), 1)

    def test_empty_history(self):
        self.assertEqual(render_table(summarize([])), "No runs recorded yet.")
        self.assertIn("No runs recorded yet.", render_markdown([]))


class CommandLine(Case):
    def test_run_exit_codes_and_output(self):
        path = self.write_task()
        self.script({"do": ALL})
        done = cli("run", str(path))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("digest  OK", done.stdout)
        self.assertIn("[ok ] draft: verified", done.stdout)

        self.log.unlink()
        self.script({"do": ["fetch"]})
        done = cli("run", str(path))
        self.assertEqual(done.returncode, 1)
        self.assertIn("digest  INCOMPLETE", done.stdout)
        self.assertIn("[!! ] draft: unverified", done.stdout)

        self.log.unlink()
        self.script({"sleep": 60})
        path.write_text(path.read_text().replace('model = "sonnet"', 'model = "sonnet"\ntimeout_minutes = 0.05'))
        self.assertEqual(cli("run", str(path)).returncode, 124)

    def test_denied_tool_prints_the_rule_to_add(self):
        path = self.write_task()
        denial = {"tool_name": "mcp__mail__send", "tool_input": {"to": "x"}}
        self.script({"do": ["fetch", "draft"], "report": {"summary": "blocked"}, "result": {"permission_denials": [denial]}})
        done = cli("run", str(path))
        self.assertEqual(done.returncode, 1)
        self.assertIn("to allow it, add 'mcp__mail__send' to allowed_tools", done.stdout)

    def test_bad_task_file_exits_2_and_lists_problems(self):
        (self.tmp / "bad.toml").write_text('name = "x"\n')
        done = cli("run", str(self.tmp / "bad.toml"))
        self.assertEqual(done.returncode, 2)
        self.assertIn("prompt is empty", done.stderr)

    def test_lint_status_report_schedule_doctor(self):
        path = self.write_task(extra='schedule = "0 9 * * 1-5"\ntimeout_minutes = 5\n')
        self.assertEqual(cli("lint", str(path)).returncode, 0)
        self.assertIn("[no-alert]", cli("lint", "-v", str(path)).stdout)

        self.script({"do": ALL})
        cli("run", str(path))
        status = cli("status", "--runs-dir", str(self.tmp / "runs"))
        self.assertIn("digest", status.stdout)
        self.assertIn("100%", status.stdout)
        out = self.tmp / "status.md"
        self.assertEqual(cli("report", "--runs-dir", str(self.tmp / "runs"), "--out", str(out)).returncode, 0)
        self.assertIn("# Scheduled task status", out.read_text())

        self.assertIn("<key>StartCalendarInterval</key>", cli("schedule", str(path)).stdout)
        self.assertIn("0 9 * * 1-5 cd ", cli("schedule", "--format", "cron", str(path)).stdout)
        written = cli("schedule", str(path), "--write", str(self.tmp / "agents"))
        self.assertTrue((self.tmp / "agents" / "com.runguard.digest.plist").is_file())
        self.assertIn("launchctl bootstrap", written.stdout)

        doctor = cli("doctor")
        self.assertEqual(doctor.returncode, 0, doctor.stdout)
        self.assertIn("all optional flags available", doctor.stdout)

    def test_examples_in_the_repo_load_and_lint_clean(self):
        examples = sorted((ROOT / "examples" / "tasks").glob("*.toml"))
        self.assertTrue(examples)
        for path in examples:
            done = cli("lint", "--strict", str(path))
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
