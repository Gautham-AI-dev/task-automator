# Comparison with alternatives

Honest positioning: use the smallest tool that meets the requirement.

## cron + scripts

Fine for single commands. Falls over at: resume-after-crash, artifact
verification, cost accounting, stall detection. `task-automator` is the
next step up with no new infrastructure.

## Temporal

Full durable-execution platform: server + datastore + visibility store,
biweekly upgrades, per-workflow history limits (51,200 events) requiring
Continue-As-New discipline. Choose it for organization-scale,
multi-team orchestration. Overkill for a single-node batch runner —
which is why `task-automator` defaults to stdlib-only and offers DBOS
instead.

## DBOS Transact alone

`task-automator`'s optional layer *is* DBOS Transact, pre-wired for the
agent-batch use case (budget acquire/settle steps, async bounded
dispatch, approval gates). Use raw DBOS if you're building a different
application; use this repo if your application *is* batch agent runs.

## LangGraph checkpointing / Temporal LangGraph plugin (public preview)

Graph-native durability for agent internals (node-level checkpoints,
replay). Complementary: LangGraph persists the *agent's* reasoning
steps; `task-automator` persists the *batch's* task outcomes, costs, and
gates. They compose — a LangGraph agent can be the thing dispatched per
task.

## When to choose what

- Overnight backlog on one machine → core `runner.py`.
- Multi-day runs, human approvals, Postgres available → `runner_dbos.py`.
- Org-scale, many teams, compliance surface → Temporal (managed or
  self-hosted with staffing).
