# agent-runguard

> **Published for review.** You are welcome to read this code. Running it, copying it or building on it needs the author's permission. To have it run on your project, hire the author.

Scheduled Claude Code tasks that either finish every step, or tell you exactly which step did not happen and why.

`runguard` is a small runner you put between your scheduler (launchd, cron) and the `claude` command line. It needs Python 3.11+ and nothing else.

Not affiliated with Anthropic.

## The problems it is built for

| What goes wrong with unattended agent tasks | What runguard does about it |
|---|---|
| A run skips a step even though the step is clearly listed, and still says "done" | Steps are data, each with **evidence** that a separate program checks after the run. No evidence, not done, whatever the agent reported. Failed steps get one targeted resume. |
| Instruction files have grown long, and reliability dropped | The prompt holds the goal and the rules. Steps live in the task file, where they are checked. `runguard lint` flags long prompts and steps nobody can verify. |
| A run sits at "running" for hours | A hard time limit for the whole run. At the limit the entire process group is killed and the run is recorded as `timeout`. A second run of the same task never overlaps the first. |
| Unattended runs stall or fail on a permission prompt that a live session would simply click through | Each task has an explicit allow list. Anything else is **denied immediately** instead of waiting, and the record names the denied tool and the exact rule to add. |
| No way to set a different model or effort per task | `model`, `effort`, `fallback_model` and `max_budget_usd` are per task. |

## See it work in 15 seconds

No Claude account needed. The demo uses a scripted stand-in for the CLI; the runner code is the real thing.

```bash
python3 examples/demo.py
```

```
2. The agent reports the drafts as done but never wrote them. Caught, then fixed by one targeted resume
  digest  OK  0.1s  attempts=2  every step verified
    [ok ] fetch: verified ({run_dir}/items.json (6 bytes))
    [ok ] draft: verified (2 file(s) match {run_dir}/drafts/*.md)
    [ok ] summary: verified ({run_dir}/summary.txt (19 bytes))

3. The agent skips the same step again on the resume
  digest  INCOMPLETE  0.08s  attempts=2  [draft] reported done, but the check failed: 0 fresh file(s) match {run_dir}/drafts/*.md, need 2
    [ok ] fetch: verified ({run_dir}/items.json (6 bytes))
    [!! ] draft: unverified (0 fresh file(s) match {run_dir}/drafts/*.md, need 2)
    [ok ] summary: verified ({run_dir}/summary.txt (19 bytes))
  exit code 1

4. The run hangs. It is killed at the time limit (6 seconds here)
  digest  TIMEOUT  6.01s  attempts=1  stopped after the 6s time limit; the process group was killed
  exit code 124

5. The agent needs a tool that is not on the allow list. Denied at once, with the rule to add
  digest  BLOCKED  0.06s  attempts=1  [summary] reported blocked: summary: blocked
    denied: mcp__mail__send_email  ->  to allow it, add 'mcp__mail__send_email' to allowed_tools
  exit code 1
```

## A task file

One TOML file per task. This is [`examples/tasks/daily-digest.toml`](examples/tasks/daily-digest.toml):

```toml
name = "daily-digest"
schedule = "30 7 * * 1-5"                 # 07:30, Monday to Friday
prompt_file = "prompts/daily-digest.md"   # short on purpose

model = "sonnet"                          # per task
effort = "low"                            # per task
timeout_minutes = 10                      # hard limit for the whole run
max_budget_usd = 0.50
retries = 1                               # one resume, aimed only at steps that failed their check

permission_mode = "dontAsk"               # anything not listed below is denied, never waited on
allowed_tools = ["Read", "Write", "Bash(python3 scripts/export_tickets.py:*)"]

[[steps]]
id = "export"
do = "Run `python3 scripts/export_tickets.py {run_dir}/tickets.json` to export yesterday's tickets."
evidence = { file = "{run_dir}/tickets.json", json = true }

[[steps]]
id = "digest"
do = "Read {run_dir}/tickets.json and write a digest of at most 12 lines to {run_dir}/digest.md. Start it with the heading '# Daily digest'."
evidence = { file = "{run_dir}/digest.md", contains = "# Daily digest" }

[alert]
command = "echo \"$RUNGUARD_TASK $RUNGUARD_STATUS: $RUNGUARD_REASON\" >> runs/alerts.log"
```

### Evidence

| Kind | Passes when |
|---|---|
| `file = "path"` | The file exists, is not empty, and **was written during this run**. Optional: `contains = "text"`, `json = true`, `min_bytes = n`. |
| `glob = "pattern"`, `min_count = n` | At least `n` fresh, non-empty files match. |
| `command = "…"` | The command exits 0. Use it for anything that is not a file: a row in a database, a message in an outbox, an API that confirms a post. |

A file left over from yesterday does not count. That is the usual way a skipped step goes unnoticed.

Placeholders in `do` and in evidence: `{run_dir}` (a new folder per run), `{run_id}`, `{date}`, `{workdir}`.

A step with no evidence is allowed, but it is shown as `ok*` (taken on the agent's word) and `lint` warns about it.

## Commands

```bash
bin/runguard doctor                               # is the claude CLI found, and which flags does it support?
bin/runguard lint examples/tasks/*.toml           # reliability problems in task files
bin/runguard run examples/tasks/daily-digest.toml # one run, now
bin/runguard status --runs-dir examples/tasks/runs
bin/runguard report --runs-dir examples/tasks/runs --out status.md
bin/runguard schedule examples/tasks/*.toml --write ~/Library/LaunchAgents   # macOS
bin/runguard schedule examples/tasks/*.toml --format cron                    # Linux
```

| Status | Exit code | Meaning |
|---|---|---|
| `ok` | 0 | Every step passed its check |
| `incomplete` | 1 | A step was skipped or failed its check, also after the resume |
| `blocked` | 1 | A step needs a tool that is not allowed, or the agent reported it impossible |
| `error` | 2 | The CLI failed (API error, crash, bad output, executable not found) |
| `timeout` | 124 | Time limit reached; everything was killed |
| `skipped` | 0 | The previous run of this task was still going |

Every run leaves a folder: the exact prompt and command line of each attempt, raw output, and `run.json` with the verdict for each step. `history.jsonl` per task feeds `status` and `report`.

## Scheduling

`runguard schedule` turns the task's cron expression into a launchd job (macOS) or a crontab line.

- On macOS use launchd. Unlike cron, it runs a job that was missed while the Mac was asleep once the Mac wakes.
- Schedulers start jobs with a minimal `PATH`. That is the most common reason a task works in a live session and fails on a schedule. The generated job carries the `PATH` you had when you generated it.
- `--write` only writes the `.plist` files and prints the `launchctl` command. It does not load anything by itself.

## How permissions are handled

- Default mode is `dontAsk` plus an explicit `allowed_tools` list, and prompts are answered by nobody (`--permission-prompts none`), so a tool that is not allowed is denied at once.
- A denial ends the run as `blocked` and prints the rule that would allow it. You decide whether to add it.
- `bypassPermissions` works if you set it, and `lint` warns about it. The point of this tool is that you should not need it.
- For sending messages, prefer a connector or script you can allow by name (`mcp__…__send`, `Bash(python3 scripts/send.py:*)`) over driving a browser or a chat app, and give the step a `command` check that confirms the message exists.

## What has been verified, and what has not

Verified:

- 46 tests pass (standard library `unittest`, no dependencies). They drive the real runner against a scripted stand-in for the CLI and cover: skipped steps, missing reports, stale files, wrong content, resume targeting, the time limit including child processes, overlap locking, denials, crashes, garbage output, older CLIs that lack newer flags, cron parsing, launchd output and the command line.
- The command line the runner builds, and the prompt on standard input, were accepted by the real Claude Code CLI (2.1.325).
- The runner was run against the real CLI in the build environment, where no login was available. It recorded the CLI's real failure correctly: `ERROR … api_error: Authentication error`. `runguard doctor` read the real `--help` and found every optional flag.
- One unmodified result object captured from the real CLI is a test fixture. It is an API authentication error that the CLI labels `"subtype": "success"` with `"is_error": true`, which is why the runner checks `is_error`.

Not verified in the build environment:

- A successful run against the real model end to end. There were no credentials there. Run `bin/runguard run examples/tasks/daily-digest.toml` on a machine where `claude` is logged in.
- Where exactly the real CLI puts the `--json-schema` answer. The runner reads `structured_output` and also falls back to JSON in the text answer, so it works either way. Step verdicts never depend on it: evidence decides.
- The launchd jobs are checked as valid property lists with the right schedule. They were not loaded on a Mac.

```bash
python3 -m unittest discover -s tests -v
```

## Layout

```
runguard/config.py     task files
runguard/contract.py   the run contract appended to the prompt, and reading the report back
runguard/verify.py     independent evidence checks
runguard/runner.py     time limit, process-group kill, resume, verdicts
runguard/store.py      run records, history, status page
runguard/schedule.py   cron expression to launchd job or crontab line
runguard/lint.py       reliability checks for task files
tests/fake_claude.py   scripted stand-in for the CLI
examples/              two runnable tasks and the demo
```

## License

All rights reserved. See [LICENSE](LICENSE). This code is published to be read. Running, copying or reusing it requires the author's permission.
