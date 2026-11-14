import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FAKE = str(ROOT / "tests" / "fake_claude.py")

from runguard import runner  # noqa: E402
from runguard.config import load_task  # noqa: E402

HEADER = '''name = "{name}"
prompt = "Write the daily digest."
model = "sonnet"
effort = "low"
allowed_tools = ["Read", "Write", "Bash(python3 scripts/fetch.py:*)"]
disallowed_tools = ["WebFetch"]
'''
STEPS = '''
[[steps]]
id = "fetch"
do = "Save the items to {run_dir}/items.json"
evidence = { file = "{run_dir}/items.json", json = true }

[[steps]]
id = "draft"
do = "Write one draft per item into {run_dir}/drafts/"
evidence = { glob = "{run_dir}/drafts/*.md", min_count = 2 }

[[steps]]
id = "summary"
do = "Write {run_dir}/summary.txt and mention the word digest"
evidence = { file = "{run_dir}/summary.txt", contains = "digest" }
'''
EVIDENCE = {
    "fetch": [["{run_dir}/items.json", "[1, 2]"]],
    "draft": [["{run_dir}/drafts/a.md", "draft a"], ["{run_dir}/drafts/b.md", "draft b"]],
    "summary": [["{run_dir}/summary.txt", "the digest is ready"]],
}
ALL = ["fetch", "draft", "summary"]
FAKE_VARS = ("FAKE_SCRIPT", "FAKE_EVIDENCE", "FAKE_HELP", "RUNGUARD_RUNS_DIR", "RUNGUARD_RUN_ID")


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="runguard-")).resolve()
        self.log = self.tmp / "fake.log"
        self._env = dict(os.environ)
        for key in FAKE_VARS:
            os.environ.pop(key, None)
        os.environ.update(FAKE_LOG=str(self.log), RUNGUARD_CLAUDE_BIN=FAKE, FAKE_EVIDENCE=json.dumps(EVIDENCE))
        runner._flag_cache.clear()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_task(self, extra="", steps=STEPS, name="digest", filename="task.toml"):
        path = self.tmp / filename
        path.write_text(HEADER.format(name=name) + extra + steps, encoding="utf-8")
        return path

    def task(self, **kw):
        return load_task(self.write_task(**kw))

    def script(self, *acts):
        os.environ["FAKE_SCRIPT"] = json.dumps(list(acts))

    def calls(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie is dead for our purposes
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True
