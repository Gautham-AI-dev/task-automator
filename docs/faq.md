# FAQ

## Which agents can tasks use?

The `agent` field in `tasks.json` names an agent registered in your
opencode configuration (user `~/.config/opencode/` or project
`.opencode/`). The examples use `"agent": "build"` — replace it with an
agent that exists in YOUR registry, or dispatch fails with exit 127 /
agent-not-found. List yours with `opencode agent list` (or inspect your
opencode config) before editing `tasks.json`.

## Which AI models work?
Any model `opencode run -m` accepts (`--model` overrides the manifest
default). Free-tier routes work (ledger records zero cost); paid models
work identically — the ledger records real spend. No specific cost figure
is claimed here: verify against your own ledger rows.

## Which OS?

The core runner is portable Python (stdlib only): Windows, Linux, macOS.
`opencode` itself must be on PATH.

## Can I use it in CI?

Yes: `dry-run` for validation, `run --only …` per job, exit codes and
`state.json` for gating. Keep `--timeout` below the job limit and set
`stall_after_sec` generously for shared runners.

## A task says DONE but the artifact is wrong. Why?

Done-checks prove existence and freshness, never quality. Tighten the
prompt, add `minlines`/`mtime` checks, or add a downstream review task
whose gate is the upstream artifact.

## Ledger totals don't match my provider bill. Why?

Incomplete `step_finish` streams (opencode #26855) make rows lower
bounds. Reconcile via the opencode session DB/export. Never bill from
`complete: false` rows.

## How do I reset?

`python runner.py reset` clears all state; `--only T01,T02` clears just
those. Logs and ledger are append-only history — archive or delete them
explicitly.

## Where do I file issues?

GitHub Issues on this repo. Include: task id, `state.json` entry,
`logs/<id>.log` tail, and the ledger rows for the task.
