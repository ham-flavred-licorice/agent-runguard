import json
import plistlib
import unittest
from pathlib import Path

from support import ROOT, STEPS, Case
from runguard.config import ConfigError, load_task
from runguard.lint import lint_task
from runguard.schedule import ScheduleError, cron_to_launchd, parse_cron, render_cron_line, render_plist


def codes(task):
    return {i.code: i.level for i in lint_task(task)}


class Config(Case):
    def test_defaults(self):
        task = self.task()
        self.assertEqual((task.timeout_seconds, task.retries, task.permission_mode), (1200, 1, "dontAsk"))
        self.assertEqual(task.runs_dir, self.tmp / "runs")
        self.assertEqual([s.id for s in task.steps], ["fetch", "draft", "summary"])

    def test_json_task_file_is_equivalent(self):
        spec = {"name": "digest", "prompt": "Write the daily digest.", "model": "sonnet",
                "steps": [{"id": "fetch", "do": "Save items", "evidence": {"file": "{run_dir}/items.json"}}]}
        path = self.tmp / "task.json"
        path.write_text(json.dumps(spec))
        task = load_task(path)
        self.assertEqual((task.name, task.model, task.steps[0].evidence.file), ("digest", "sonnet", "{run_dir}/items.json"))

    def test_prompt_file_is_read_relative_to_the_task_file(self):
        (self.tmp / "prompts").mkdir()
        (self.tmp / "prompts" / "p.md").write_text("From a file.\n")
        path = self.write_task(extra='prompt_file = "prompts/p.md"\n')
        path.write_text(path.read_text().replace('prompt = "Write the daily digest."\n', ""))
        self.assertEqual(load_task(path).prompt, "From a file.")

    def test_all_problems_are_reported_at_once(self):
        bad = '''name = "Bad Name"
prompt = ""
effort = "extreme"
permission_mode = "yolo"
timeout_minutes = -1
retries = 9
typo_key = 1
[[steps]]
id = "a"
do = "x"
evidence = { file = "a", glob = "b" }
[[steps]]
id = "a"
do = ""
'''
        path = self.tmp / "bad.toml"
        path.write_text(bad)
        with self.assertRaises(ConfigError) as caught:
            load_task(path)
        text = " | ".join(caught.exception.problems)
        for needle in ("unknown key 'typo_key'", "name 'Bad Name'", "prompt is empty", "effort must be", "permission_mode must be",
                       "timeout_minutes", "retries", "exactly one of file, glob, command", "duplicate id 'a'", "'do' is empty"):
            self.assertIn(needle, text)

    def test_missing_file_and_bad_syntax(self):
        with self.assertRaises(ConfigError):
            load_task(self.tmp / "nope.toml")
        (self.tmp / "broken.toml").write_text("name = ")
        with self.assertRaises(ConfigError):
            load_task(self.tmp / "broken.toml")


class Lint(Case):
    def test_well_formed_task_has_no_errors_or_warnings(self):
        task = self.task(extra='schedule = "0 9 * * 1-5"\ntimeout_minutes = 10\n')
        levels = set(codes(task).values())
        self.assertNotIn("error", levels)
        self.assertNotIn("warning", levels)

    def test_long_prompt_is_flagged(self):
        path = self.write_task()
        path.write_text(path.read_text().replace('prompt = "Write the daily digest."', 'prompt = """' + "Do the thing.\\n" * 200 + '"""'))
        self.assertEqual(codes(load_task(path)).get("long-prompt"), "warning")

    def test_risky_settings_are_flagged(self):
        steps = '\n[[steps]]\nid = "send"\ndo = "Send the email"\n\n[[steps]]\nid = "old"\ndo = "x"\nevidence = { file = "out.txt", allow_stale = true }\n'
        path = self.write_task(extra='permission_mode = "bypassPermissions"\nschedule = "61 * * * *"\n', steps=steps)
        path.write_text(path.read_text().replace('"Bash(python3 scripts/fetch.py:*)"', '"Bash"'))
        found = codes(load_task(path))
        self.assertEqual(found["unverifiable-step"], "warning")
        self.assertEqual(found["stale-evidence"], "warning")
        self.assertEqual(found["bypass-permissions"], "warning")
        self.assertEqual(found["blanket-bash"], "warning")
        self.assertEqual(found["bad-schedule"], "error")

    def test_task_without_steps_is_an_error(self):
        self.assertEqual(codes(self.task(steps="")).get("no-steps"), "error")


class Schedule(Case):
    def test_cron_fields(self):
        self.assertEqual(parse_cron("0 9 * * 1-5"), {"minute": [0], "hour": [9], "day": None, "month": None, "weekday": [1, 2, 3, 4, 5]})
        self.assertEqual(parse_cron("*/15 * * * *")["minute"], [0, 15, 30, 45])
        self.assertEqual(parse_cron("5/20 * * * *")["minute"], [5, 25, 45])
        self.assertEqual(parse_cron("0 0 * jan,jul sun")["month"], [1, 7])
        self.assertEqual(parse_cron("0 0 * * 7")["weekday"], [0])
        self.assertEqual(parse_cron("0 8-18/5 * * *")["hour"], [8, 13, 18])
        for bad in ("* * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "a * * * *", "5-1 * * * *", "*/0 * * * *"):
            with self.assertRaises(ScheduleError, msg=bad):
                parse_cron(bad)

    def test_weekday_schedule_becomes_one_launchd_entry_per_day(self):
        self.assertEqual(cron_to_launchd("0 9 * * 1-5"), [{"Minute": 0, "Hour": 9, "Weekday": d} for d in range(1, 6)])
        self.assertEqual(cron_to_launchd("30 7 * * *"), [{"Minute": 30, "Hour": 7}])
        self.assertEqual(cron_to_launchd("* * * * *"), [{}])

    def test_day_of_month_and_weekday_keep_cron_either_or_meaning(self):
        self.assertEqual(cron_to_launchd("0 0 1 * 1"), [{"Minute": 0, "Hour": 0, "Day": 1}, {"Minute": 0, "Hour": 0, "Weekday": 1}])

    def test_schedule_too_fine_for_launchd_is_refused(self):
        self.assertEqual(len(cron_to_launchd("*/2 * * * 1-5")), 150)
        with self.assertRaises(ScheduleError):
            cron_to_launchd("* 0-23 1-31 * *")  # 24 hours x 31 days = 744 entries

    def test_plist_is_valid_and_carries_path_and_logs(self):
        task = self.task(extra='schedule = "0 9 * * 1-5"\n')
        plist = plistlib.loads(render_plist(task, python="/usr/bin/python3", path_env="/opt/bin:/usr/bin", package_root=ROOT))
        self.assertEqual(plist["Label"], "com.runguard.digest")
        self.assertEqual(plist["ProgramArguments"], ["/usr/bin/python3", "-m", "runguard", "run", str(task.path)])
        self.assertEqual(plist["EnvironmentVariables"], {"PATH": "/opt/bin:/usr/bin", "PYTHONPATH": str(ROOT)})
        self.assertEqual(len(plist["StartCalendarInterval"]), 5)
        self.assertTrue(plist["StandardErrorPath"].endswith("runs/digest/launchd.err.log"))
        self.assertEqual(plist["WorkingDirectory"], str(self.tmp))

    def test_cron_line(self):
        task = self.task(extra='schedule = "*/30 8-18 * * *"\n')
        line = render_cron_line(task, python="/usr/bin/python3", path_env="/usr/bin", package_root=ROOT)
        self.assertTrue(line.startswith("*/30 8-18 * * * cd "))
        self.assertIn(f"/usr/bin/python3 -m runguard run {task.path}", line)
        self.assertTrue(line.endswith("cron.log 2>&1"))

    def test_task_without_schedule_cannot_be_installed(self):
        with self.assertRaises(ScheduleError):
            render_plist(self.task(), path_env="", package_root=ROOT)


if __name__ == "__main__":
    unittest.main()
