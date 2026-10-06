# Budget and cost ledger

## Ledger schema (`ledger.jsonl`, one JSON object per line)

Each row is one dispatch attempt:

```json
{"task": "T01", "attempt": 2, "outcome": "done",
 "finished_at": "2026-10-06T12:00:00",
 "tokens_in": 81118, "tokens_out": 20, "cost": 0.002,
 "complete": true, "reason": "ok"}
```

- `outcome`: `done` | `attempt-failed`.
- `complete: false` rows carry `reason` `no-step-finish(...)` or
  `partial-step-finish(N seen, last=...)`: sums are lower bounds.
- Spend per task = sum of its rows; final outcome = highest-`attempt` row.

## `--budget` (core runner)

Halts before the next dispatch — and suppresses pending retries — once
`ledger_total >= cap`. Late (permissive) on incomplete rows; see
[guarantees](guarantees.md).

## `BudgetStore` (DBOS layer)

Scopes are strings (`run:default`, `task:T01`, `agent:build`). Flow per
task: `acquire(scope, estimate)` → run → `settle(scope, estimate,
actual)`.

- Uncapped scopes are **tracked** (`cap_usd NULL`), never invisible: a cap
  added later enforces against real history.
- Settle semantic (both backends): fresh row records exactly `actual`;
  existing row computes `spent - estimate + actual`. The Postgres branch
  is a single `ON CONFLICT … EXCLUDED` statement; the SQLite branch
  checks existence under `BEGIN IMMEDIATE`. (PG branch unexecuted —
  standard SQL, verify on first Postgres run.)
- SQLite requires `TASK_AUTO_SINGLE_WORKER=1` and resolves paths against
  the module directory (`HERE`), never CWD.
