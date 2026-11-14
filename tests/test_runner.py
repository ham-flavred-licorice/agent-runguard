import fcntl
import json
import os
import time
import unittest
from pathlib import Path

from support import ALL, ROOT, Case, alive
from runguard.config import load_task
from runguard.runner import parse_result, run_task, suggest_rule


def verdicts(record):
    return {s.id: s.verdict for s in record.steps}


class HappyPath(Case):
    def test_every_step_verified(self):
        self.script({"do": ALL})
        record = run_task(self.task())
        self.assertEqual(record.status, "ok")
        self.assertEqual(verdicts(record), {"fetch": "verified", "draft": "verified", "summary": "verified"})
        self.assertEqual(record.attempts, 1)
        self.assertEqual(record.models_used, ["sonnet"])
        self.assertEqual(record.cost_usd, 0.0125)
        saved = json.loads((Path(record.run_dir) / "run.json").read_text())
        self.assertEqual(saved["status"], "ok")
        history = (Path(record.run_dir).parent / "history.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(history[0])["status"], "ok")

    def test_command_line_carries_the_per_task_settings(self):
        self.script({"do": ALL})
        record = run_task(self.task(extra='fallback_model = "haiku"\nmax_budget_usd = 1.5\n'))
        argv = self.calls()[0]["argv"]

        def after(flag, n=1):
            i = argv.index(flag)
            return argv[i + 1] if n == 1 else argv[i + 1:i + 1 + n]

        self.assertEqual(argv[:3], ["-p", "--output-format", "json"])
        self.assertEqual(after("--model"), "sonnet")
        self.assertEqual(after("--effort"), "low")
        self.assertEqual(after("--fallback-model"), "haiku")
        self.assertEqual(after("--max-budget-usd"), "1.5")
        self.assertEqual(after("--permission-mode"), "dontAsk")
        self.assertEqual(after("--permission-prompts"), "none")
        self.assertEqual(after("--allowedTools", 3), ["Read", "Write", "Bash(python3 scripts/fetch.py:*)"])
        self.assertEqual(after("--disallowedTools"), "WebFetch")
        self.assertEqual(after("--session-id"), record.session_id)
        self.assertIn("steps", json.loads(after("--json-schema"))["properties"])
        self.assertNotIn("--resume", argv)

    def test_prompt_contains_the_contract_with_real_paths(self):
        self.script({"do": ALL})
        record = run_task(self.task())
        prompt = self.calls()[0]["prompt"]
        self.assertIn("Write the daily digest.", prompt)
        self.assertIn(f"Run directory: {record.run_dir}", prompt)
        self.assertIn(f"1. [fetch] Save the items to {record.run_dir}/items.json", prompt)
        self.assertIn("one entry for each of these step ids: fetch, draft, summary", prompt)
        self.assertNotIn("{run_dir}", prompt)

    def test_report_inside_the_text_answer_is_understood(self):
        self.script({"do": ALL, "as_text": True})
        self.assertEqual(run_task(self.task()).status, "ok")


class SkippedSteps(Case):
    def test_step_reported_done_without_evidence_is_caught_then_fixed_on_resume(self):
        self.script({"do": ["fetch", "summary"]}, {"do": ["draft"]})  # first call skips "draft" but claims it
        record = run_task(self.task())
        self.assertEqual(record.status, "ok")
        self.assertEqual(record.attempts, 2)
        first, second = self.calls()
        self.assertIn("--resume", second["argv"])
        self.assertEqual(second["argv"][second["argv"].index("--resume") + 1], record.session_id)
        self.assertNotIn("--session-id", second["argv"])
        self.assertIn("[draft] reported done, but the check failed", second["prompt"])
        self.assertIn("Do not redo the steps that already passed: fetch, summary.", second["prompt"])
        self.assertNotIn("[fetch] reported", second["prompt"])

    def test_step_that_stays_skipped_ends_as_incomplete(self):
        self.script({"do": ["fetch", "summary"]})
        record = run_task(self.task())
        self.assertEqual(record.status, "incomplete")
        self.assertEqual(record.attempts, 2)
        self.assertEqual(verdicts(record)["draft"], "unverified")
        self.assertEqual(record.failed_steps, ["draft"])
        self.assertIn("[draft] reported done, but the check failed", record.reason)

    def test_step_left_out_of_the_report_is_missing(self):
        self.script({"do": ["fetch"], "omit": ["summary", "draft"]})
        record = run_task(self.task(extra="retries = 0\n"))
        self.assertEqual(record.status, "incomplete")
        self.assertEqual(record.attempts, 1)
        self.assertEqual(verdicts(record), {"fetch": "verified", "draft": "missing", "summary": "missing"})

    def test_too_few_files_and_wrong_content_do_not_count(self):
        os.environ["FAKE_EVIDENCE"] = json.dumps({
            "fetch": [["{run_dir}/items.json", "not json"]],
            "draft": [["{run_dir}/drafts/a.md", "only one"]],
            "summary": [["{run_dir}/summary.txt", "nothing relevant"]],
        })
        self.script({"do": ALL})
        record = run_task(self.task(extra="retries = 0\n"))
        self.assertEqual(record.status, "incomplete")
        self.assertEqual(set(verdicts(record).values()), {"unverified"})
        detail = {s.id: s.evidence for s in record.steps}
        self.assertIn("not valid JSON", detail["fetch"])
        self.assertIn("need 2", detail["draft"])
        self.assertIn("does not contain 'digest'", detail["summary"])

    def test_file_left_over_from_an_earlier_run_does_not_count(self):
        stale = self.tmp / "out" / "report.md"
        stale.parent.mkdir()
        stale.write_text("yesterday")
        old = time.time() - 3600
        os.utime(stale, (old, old))
        steps = '\n[[steps]]\nid = "report"\ndo = "Write out/report.md"\nevidence = { file = "out/report.md" }\n'
        self.script({"do": []})
        record = run_task(self.task(extra="retries = 0\n", steps=steps))
        self.assertEqual(record.status, "incomplete")
        self.assertIn("was not written during this run", record.steps[0].evidence)

    def test_command_evidence(self):
        steps = ('\n[[steps]]\nid = "sent"\ndo = "Send the message"\n'
                 'evidence = { command = "test -f {run_dir}/sent.flag" }\n')
        os.environ["FAKE_EVIDENCE"] = json.dumps({"sent": [["{run_dir}/sent.flag", "1"]]})
        self.script({"do": []}, {"do": ["sent"]})
        record = run_task(self.task(steps=steps))
        self.assertEqual((record.status, record.attempts), ("ok", 2))

    def test_step_without_evidence_is_marked_as_taken_on_trust(self):
        steps = '\n[[steps]]\nid = "think"\ndo = "Decide which items matter"\n'
        self.script({"do": []})
        record = run_task(self.task(steps=steps))
        self.assertEqual(record.status, "ok")
        self.assertEqual(verdicts(record), {"think": "reported"})


class StuckRuns(Case):
    def test_hung_run_is_killed_with_everything_it_started(self):
        pidfile = self.tmp / "child.pid"
        self.script({"sleep": 120, "child": str(pidfile)})
        started = time.monotonic()
        record = run_task(self.task(extra="timeout_minutes = 0.05\n"))  # 3 seconds
        elapsed = time.monotonic() - started
        self.assertEqual(record.status, "timeout")
        self.assertLess(elapsed, 20)
        self.assertEqual(record.attempts, 1)
        self.assertEqual(record.exit_codes, [None])
        child = int(pidfile.read_text())
        for _ in range(50):
            if not alive(child):
                break
            time.sleep(0.1)
        self.assertFalse(alive(child), "a process started by the CLI survived the timeout")

    def test_time_limit_covers_all_attempts_together(self):
        self.script({"do": ["fetch"], "sleep": 2})
        record = run_task(self.task(extra="timeout_minutes = 0.05\nretries = 5\n"))
        self.assertEqual(record.status, "timeout")
        self.assertLessEqual(record.attempts, 3)

    def test_second_run_of_the_same_task_is_skipped_while_the_first_holds_the_lock(self):
        task = self.task()
        lock_dir = task.runs_dir / task.name
        lock_dir.mkdir(parents=True)
        with open(lock_dir / ".lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            record = run_task(task)
        self.assertEqual(record.status, "skipped")
        self.assertFalse(self.log.exists(), "the CLI must not be started")


class Permissions(Case):
    DENIAL = {"tool_name": "Bash", "tool_use_id": "toolu_1", "tool_input": {"command": "osascript -e 'tell application \"Messages\"'"}}

    def test_denied_tool_ends_as_blocked_with_the_rule_to_add_and_no_pointless_retry(self):
        self.script({"do": ["fetch", "draft"], "report": {"summary": "blocked"}, "result": {"permission_denials": [self.DENIAL]}})
        record = run_task(self.task())
        self.assertEqual(record.status, "blocked")
        self.assertEqual(record.attempts, 1)
        self.assertEqual(verdicts(record)["summary"], "blocked")
        self.assertEqual(record.permission_denials[0]["suggested_allow_rule"], "Bash(osascript:*)")

    def test_rule_suggestions(self):
        self.assertEqual(suggest_rule({"tool_name": "Bash", "tool_input": {"command": "git push origin main"}}), "Bash(git push:*)")
        self.assertEqual(suggest_rule({"tool_name": "Bash", "tool_input": {"command": "ls -la"}}), "Bash(ls:*)")
        self.assertEqual(suggest_rule({"tool_name": "mcp__gmail__send_email", "tool_input": {"to": "x"}}), "mcp__gmail__send_email")

    def test_flags_missing_from_an_older_cli_are_left_out(self):
        os.environ["FAKE_HELP"] = "Usage: claude  -p, --print  --output-format  --permission-mode  --model  --allowedTools  -r, --resume"
        self.script({"do": ALL})
        record = run_task(self.task())
        argv = self.calls()[0]["argv"]
        self.assertEqual(record.status, "ok")
        for flag in ("--permission-prompts", "--json-schema", "--effort", "--session-id"):
            self.assertNotIn(flag, argv)
        self.assertIn("--model", argv)


class Failures(Case):
    def test_real_captured_auth_error_is_an_error_even_though_subtype_says_success(self):
        raw = (ROOT / "tests" / "fixtures" / "auth_error.json").read_text()
        self.script({"raw": raw, "exit": 1})
        record = run_task(self.task())
        self.assertEqual(record.status, "error")
        self.assertIn("api_error", record.reason)
        self.assertIn("Authentication error", record.reason)
        self.assertEqual(record.attempts, 1)

    def test_garbage_output_and_crashes_are_errors(self):
        for act, needle in (({"raw": "Segmentation fault\n", "exit": 139}, "exit code 139"),
                            ({"raw": "", "exit": 1}, "no result from the CLI")):
            self.log.unlink(missing_ok=True)
            self.script(act)
            record = run_task(self.task())
            self.assertEqual(record.status, "error")
            self.assertIn(needle, record.reason)

    def test_missing_executable_is_reported_plainly(self):
        os.environ["RUNGUARD_CLAUDE_BIN"] = str(self.tmp / "no-such-claude")
        record = run_task(self.task())
        self.assertEqual(record.status, "error")
        self.assertIn("executable not found", record.reason)

    def test_alert_command_runs_on_failure_only(self):
        marker = self.tmp / "alert.txt"
        path = self.write_task(extra="retries = 0\n")
        path.write_text(path.read_text() + f'\n[alert]\ncommand = "echo $RUNGUARD_TASK:$RUNGUARD_STATUS:$RUNGUARD_FAILED_STEPS > {marker}"\n')
        self.script({"do": ALL})
        self.assertEqual(run_task(load_task(path)).alert, "")
        self.assertFalse(marker.exists())
        self.log.unlink()
        self.script({"do": ["fetch"]})
        record = run_task(load_task(path))
        self.assertEqual(record.alert, "sent")
        self.assertEqual(marker.read_text().strip(), "digest:incomplete:draft,summary")

    def test_result_parser_tolerates_noise_and_the_list_form(self):
        obj = {"type": "result", "is_error": False}
        self.assertEqual(parse_result("warning: something\n" + json.dumps(obj)), obj)
        self.assertEqual(parse_result(json.dumps([{"type": "system"}, obj])), obj)
        self.assertIsNone(parse_result(json.dumps({"type": "assistant"})))
        self.assertIsNone(parse_result("   "))


if __name__ == "__main__":
    unittest.main()
