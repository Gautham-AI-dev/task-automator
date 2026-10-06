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
- A task is DONE only on exit 0 AND all done-checks true. Other outcomes
  are `failed` or `stalled` (killed by watchdog); each carries a
  `retryable` flag per the policy below.
- `waiting` tasks re-check gates every invocation; nothing executes early.
- Retry policy: timeouts/stalls and nonzero-exits whose artifact checks
  still pass are `retryable`; `--max-retries N` re-dispatches them
  in-process up to N times (attempt counter survives in state.json), stalled
  tasks included. Exit 0 with failing checks (claimed success, nothing
  verifiable) and nonzero-exits with failing checks need human review,
  never auto-retry.
- `--budget USD` halts before the next dispatch once the ledger total
  reaches the cap.

## Cost ledger (`ledger.jsonl`, one row per attempt)

Parsed from `--format json` step_finish events: input/output tokens and
cost. Every dispatch writes a row — including failed and retried attempts,
since every attempt costs money. Rows carry `task`, `attempt`, `outcome`
(`done` | `attempt-failed`) and `finished_at`: sum rows by task for total
spend, or take the highest-attempt row for the final outcome. Feeds
cost-guard budgeting. Stuck detection: the watchdog kills runs
whose log stops growing for `stall_after_sec` and marks them `stalled`.

Known upstream gap (opencode #26855): `run --format json` can exit on
idle before emitting the final step_finish event. Only a log whose LAST
event is step_finish counts as `complete: true`; earlier-but-not-final
step_finish sums are flagged `partial-step-finish` and treated as lower
bounds — the `--budget` cap therefore halts LATE (permissive, not
conservative) when rows are incomplete: true spend may already exceed the
cap. Treat any run with incomplete rows as needing session DB/export
reconciliation before trusting totals; do not treat their totals as
authoritative.

## Resume contract (the core guarantee)

1. Every state mutation is crash-safe (write tmp + os.replace).
2. Done is re-verified, never trusted: done-checks run against the
   filesystem on every invocation before skipping; a task whose
   artifacts vanished is RE-RUN, not skipped.
3. Failed/stalled tasks keep their logs; resume retries them in place.
4. Dry-run never writes state, never dispatches, never fails a task.

## Phase 2 (optional): durable per-task workflows (`runner_dbos.py`)

For multi-day runs / human approval gates, one DBOS workflow per task
(never one-workflow-per-catalog: keeps histories short, no 30-min
workflow sleeps). Requires `pip install "dbos[postgres]>=2.0"` and
`DATABASE_URL` at Postgres; without them the module imports fine but
`launch/status/approve` raise a clear error. Plain `runner.py` stays
zero-dependency and is the default.

```
python runner_dbos.py launch --only T01 [--model ...] [--estimate 1.0] [--needs-approval]
python runner_dbos.py status --wid taskauto-T01-<stamp>
python runner_dbos.py approve --wid taskauto-T01-<stamp> --approve yes|no
```

Rules baked in (report-code bugs fixed): every budget/ledger/side effect
is a `@step` (workflow body orchestrates only); dispatch is an async
step so timeouts apply (DBOS step timeouts are async-only); ledger append
is non-retried; budget caps live in Postgres with `SELECT ... FOR UPDATE`
(SQLite only with `TASK_AUTO_SINGLE_WORKER=1`, otherwise refused);
approval waits use a single `DBOS.recv` timeout on the workflow's own
inbox (`approve --wid` targets it; approvers discover pending items via
workflow status queries — no side-channel notify exists), fail-closed on
timeout/unclear messages.

Provenance: the workflow file is reviewed but UNEXECUTED (dbos +
Postgres unavailable where built) — run it against Postgres before
relying on durable execution; helpers are test-covered.
