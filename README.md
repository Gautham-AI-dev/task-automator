# task-Automator (public release staging)

Portable batch-execution skill for headless `opencode run` dispatches:
crash-safe resume, artifact done-verification, time gates, cost ledger,
stall watchdog. MIT licensed (see LICENSE).

## Layout

- `SKILL.md` — contract, neighbor map, resume guarantee.
- `runner.py` — the runner (stdlib only).
- `runner_dbos.py` + `runner_dbos_workflow.py` — OPTIONAL Phase 2:
  durable per-task workflows on DBOS + Postgres (needs
  `pip install "dbos[postgres]>=2.0"` + `DATABASE_URL`; plain `runner.py`
  needs nothing). See SKILL.md.
- `tests/test_runner.py` — offline tests (`python tests/test_runner.py`).
- `tests/test_runner_dbos.py` — offline tests for the DBOS module
  (`python tests/test_runner_dbos.py`; needs no DBOS/Postgres).
- `tasks.example.json` — 3-task example manifest (edit paths, rename to
  `tasks.json`).
- `PROMPTS.example.md` — matching example prompts (rename to `PROMPTS.md`).
- `README.md` — usage (in skill doc).

## Quick start

```bash
python runner.py dry-run
python runner.py run --only T01
python runner.py resume
python runner.py status
```

Runtime files (`state.json`, `logs/`, `ledger.jsonl`) are git-ignored
by design — they are per-run state, not source. Verified with the
muse-spark free route at $0 cost; any `opencode` model works via `--model`.
