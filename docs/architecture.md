# Architecture

## Components

```
tasks.json ─┐
PROMPTS.md ─┴─▶ runner.py ─┬─▶ opencode run (subprocess, one per task)
                           ├─▶ state.json   (crash-safe checkpoint)
                           ├─▶ logs/<id>.log (raw JSONL event stream)
                           └─▶ ledger.jsonl  (per-attempt cost rows)

runner_dbos.py (optional) ─▶ DBOS + Postgres: one workflow per task
tests/ ─▶ offline suites, no opencode / DBOS / Postgres required
```

## `runner.py` (stdlib only)

Single-threaded, sequential dispatcher:

1. Load manifest (`tasks.json`), prompts (`PROMPTS.md` markers), state.
2. For each selected task: skip-if-done-verified → budget check → gate
   check → prompt lookup → dry-run print or dispatch loop.
3. `dispatch()` spawns `opencode run --agent <agent> -m <model> --title
   "<id> <title>" --format json <prompt>` via `subprocess.Popen`,
   watches log growth (`stall_after_sec`) and a hard deadline
   (`--timeout` / `task_timeout_sec`), kills on stall/timeout with
   distinct exit codes (125 stalled, 124 timeout, 127 missing binary).
4. After each attempt: parse cost, run done-checks, append the ledger row
   (with that attempt's outcome), classify (`failure_class`), retry or
   record the outcome, save state.

## State machine

```
pending ─▶ waiting (gate closed; re-checked every invocation)
        ─▶ running ─▶ done (exit 0 AND all done-checks)
                   ─▶ stalled (killed by watchdog; retryable)
                   ─▶ failed/retryable (classified; see guarantees)
done ─▶ skip (artifacts re-verified) | RE-RUN (artifacts missing)
```

`attempt` increments per dispatch and survives in `state.json`, so
`--max-retries` accounting is stable across resumes.

## `runner_dbos.py` (optional durable layer)

One DBOS workflow per task (never one-workflow-per-catalog: keeps
histories short, no long workflow sleeps):

```
launch → start_workflow(task_workflow)
  step_gate → step_acquire (budget reserve) → step_dispatch (async,
  bounded) → step_verify → step_ledger → step_settle → [approval recv]
```

- Every side effect is a `@step`; the workflow body orchestrates only.
- Ledger append is `retries_allowed=False` (a retried step would
  double-append).
- Budget caps live in Postgres (`SELECT … FOR UPDATE`); SQLite only
  with `TASK_AUTO_SINGLE_WORKER=1`.
- Approval waits use a single `DBOS.recv` timeout on the workflow's own
  inbox, fail-closed. No side-channel notify exists by design.
- Non-done transient outcomes return `needs-operator-retry`: re-launch
  resumes from the last completed step (single-shot by design, no
  in-workflow retry loop).

Provenance: the workflow definition is reviewed but UNEXECUTED against a
live DBOS+Postgres deployment as of this writing. Helpers
(`BudgetStore`, approval parsing) are test-covered.
