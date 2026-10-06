---
name: task-automator
description: Run catalog task lists through headless opencode agents with crash-safe checkpoints, artifact done-verification, time gates, cost ledger, and stall detection. Resume after any break. Use for batch execution of multi-task backlogs (research ops, audits, digests) where each task must prove completion with files, not claims.
---

# Task Automator

Executes a task catalog (see `tasks.json` schema below) through headless
`opencode run` dispatches. Each task checkpoints to `state.json`
(crash-safe tmp+replace). Any interruption — intended stop, crash,
timeout, stall — resumes exactly where it stopped: done tasks verify
and skip, failed tasks retry, waiting tasks re-check gates.

## What this is NOT (neighbor skills, referenced not duplicated)

- `data-orchestrator`: data pipelines (Excel/PDF/CSV). Different domain.
- `goal-plugin.server.js`: tracks long-running goals. This skill
  *executes* and reports; goal state stays with the plugin.
- `coworker-orchestrator.ts`: coordinates LIVE agent sessions. This
  skill runs HEADLESS batch dispatches (`opencode run`, no TUI).
- `teach-a-task`: compiles observed workflows into routines. This skill
  consumes task lists; teach-a-task output is valid input.
- `workflows` (session): inspects background runs. Complementary.

## Manifest schema (`tasks.json`)

```json
{
  "default_model": "opencode/muse-spark-1.3-contributor-free",
  "task_timeout_sec": 1800,
  "stall_after_sec": 600,
  "tasks": [{
    "id": "T01", "title": "...", "agent": "literature-analyst",
    "section": "T01",
    "done": [{"type": "newfile", "dir": "<abs>", "glob": "*.md"}],
    "gate": {"telemetry": "<abs path>", "entries_min": 10, "span_days_min": 7}
  }]
}
```

- `section`: header marker `<!-- === T01 === -->` in `PROMPTS.md`.
- Done-check types: `newfile` (glob newer than run start), `minlines`
  (`path` + `min_lines`), `mtime` (`path` newer than run start).
- `gate` (optional): task stays `waiting` until telemetry preconditions hold.
- Only primary agents (registry `agents/*.yaml`); `general` is a subagent
  and silently falls back — never use it.

## Commands (`runner.py`)

```
python runner.py status [--only T01,..] [--state FILE]
python runner.py dry-run [--only ...]     # zero side effects, writes no state
python runner.py run [--only ...] [--model ...] [--state FILE]
  [--budget USD] [--max-retries N]
python runner.py resume [...]             # identical to run; skips done
python runner.py reset [--only ...]
```

- Dispatch: `opencode run --agent <agent> -m <model> --title "<id> <title>" --format json <prompt>`; stdout+stderr to `logs/<id>.log`.
- A task is DONE only on exit 0 AND all done-checks true. Anything else
  is `failed` (retryable) or `stalled` (killed by watchdog, retryable).
- `waiting` tasks re-check gates every invocation; nothing executes early.
- Retry policy: timeouts/stalls and nonzero-exits whose artifact checks
  still pass are `retryable` (`--max-retries N` to auto-flag them);
  nonzero-exits with failing checks need human review, never auto-retry.
- `--budget USD` halts before the next dispatch once the ledger total
  reaches the cap.

## Cost ledger (`ledger.jsonl`, one line per finished task)

Parsed from `--format json` step_finish events: input/output tokens and
cost. Feeds cost-guard budgeting. Stuck detection: the watchdog kills runs
whose log stops growing for `stall_after_sec` and marks them `stalled`.

Known upstream gap (opencode #26855): `run --format json` can exit on
idle before emitting the final step_finish event. Ledger entries parsed
from such logs carry `complete: false` plus a reconcile warning — do not
treat their totals as authoritative; reconcile via the session DB/export.

## Resume contract (the core guarantee)

1. Every state mutation is crash-safe (write tmp + os.replace).
2. Done is re-verified, never trusted: done-checks run against the
   filesystem on every invocation before skipping.
3. Failed/stalled tasks keep their logs; resume retries them in place.
4. Dry-run never writes state, never dispatches, never fails a task.
