# Durable execution (optional DBOS layer)

For multi-day runs and human approval gates, `runner_dbos.py` promotes
each task to a durable DBOS workflow on Postgres. The core `runner.py`
stays zero-dependency and is the default; this layer is opt-in.

## Setup

```bash
pip install "dbos[postgres]>=2.0"
export DATABASE_URL=postgresql://user:pass@host:5432/taskauto
export TASK_AUTO_SINGLE_WORKER=1   # only if using SQLite instead
python runner_dbos.py launch --only T01 --estimate 1.0 --needs-approval
python runner_dbos.py status --wid taskauto-T01-<stamp>
python runner_dbos.py approve --wid taskauto-T01-<stamp> --approve yes|no
```

Without `dbos` installed the module still imports (helpers + offline
tests run); `launch/status/approve` raise an install error.

## Approval gates

`--needs-approval`: after a `done` result, the workflow waits on its own
inbox (`DBOS.recv` with `approval_timeout`, default 7 days), fail-closed
on timeout or unclear messages (`approved: true` required; anything else
denies). Approvers discover pending items via workflow status queries.
Re-launching a `needs-operator-retry` workflow resumes from the last
completed step.

## Provenance

Reviewed but UNEXECUTED against live DBOS+Postgres (unavailable where
built). `BudgetStore` + approval parsing are covered by
`tests/test_runner_dbos.py`. Run the workflow against Postgres before
relying on durable execution in production.
