# task-automator

**Batch runner for headless AI coding agents — crash-safe resume, artifact-verified completion, cost ledger, stall watchdog, optional durable workflows.**

`task-automator` executes a catalog of tasks through headless
[`opencode run`](https://opencode.ai) dispatches — one agent turn per
task — and proves each task finished by checking **files on disk, not
model claims**. Any interruption (intended stop, crash, timeout, stall)
resumes exactly where it stopped: done tasks re-verify and skip, failed
tasks classify and retry, waiting tasks re-check their gates.

Built for: **AI agent automation, batch agent workflows, headless coding
agents, overnight/backlog execution, cost-controlled multi-task
dispatch, human-in-the-loop approval gates, durable agent execution**.

- Zero dependencies for the core runner (Python 3.10+ stdlib only).
- Optional durable-execution layer on [DBOS](https://www.dbos.dev) +
  Postgres (one workflow per task, budget caps, approval gates).
- Offline test suite, MIT licensed.

---

## What it does

| Capability | How |
|---|---|
| **Batch dispatch** | `tasks.json` catalog → one `opencode run --agent … --format json` per task, stdout+stderr captured to `logs/<id>.log` |
| **Crash-safe resume** | Every state mutation is write-tmp + `os.replace`; `resume` picks up after any break |
| **Done is re-verified** | `done` tasks re-run their artifact checks (`newfile` / `minlines` / `mtime`) against the filesystem before skipping; vanished artifacts trigger a re-run, legacy state entries re-run once fail-safe |
| **Time gates** | Tasks wait until telemetry preconditions hold (entry count + time span) before dispatching |
| **Cost ledger** | Per-attempt rows (`task`, `attempt`, `outcome`, tokens, cost) parsed from `--format json` `step_finish` events; incomplete streams flagged, never silently trusted (known upstream gap: opencode #26855) |
| **Stall watchdog** | Runs whose log stops growing for `stall_after_sec` are killed and marked `stalled` (retryable) |
| **Classified retry** | Timeouts/stalls and nonzero-exits with passing checks are `retryable` (`--max-retries N` re-dispatches in-process); exit-0-with-failing-checks and nonzero-with-failing-checks need human review |
| **Global budget cap** | `--budget USD` halts before the next dispatch — and suppresses in-loop retries — once ledger spend reaches the cap (halts late on incomplete rows: documented as permissive, not conservative) |
| **Durable execution (optional)** | `runner_dbos.py`: one DBOS workflow per task on Postgres — budget acquire/settle steps, async bounded dispatch, fail-closed approval waits |

## What it is not

- Not an agent framework (no planning, memory, or tool-use of its own) — it *drives* agents that already exist.
- Not a replacement for Temporal / DBOS Transact / LangGraph checkpointing — it is a lightweight batch harness; the optional DBOS layer covers multi-day runs and approval gates. See [docs/comparison.md](docs/comparison.md).
- Not a prompt library — `PROMPTS.md` holds your task prompts; the runner holds the execution guarantees.

---

## Quick start

```bash
# 1. Describe your tasks (copy the examples and edit paths/prompts)
cp tasks.example.json tasks.json
cp PROMPTS.example.md PROMPTS.md

# 2. Check gates and prompt wiring without side effects (writes no state)
python runner.py dry-run

# 3. Run everything, or one task
python runner.py run
python runner.py run --only T01

# 4. Check status any time; resume after any break
python runner.py status
python runner.py resume

# 5. Guarded run: $2 cost cap, 1 auto-retry of transient failures
python runner.py run --budget 2.0 --max-retries 1
```

Runtime files (`state.json`, `logs/`, `ledger.jsonl`,
`taskauto_budget.db`) are git-ignored by design — per-run state, not source.

## Manifest schema (`tasks.json`)

```json
{
  "default_model": "opencode/mimo-v2.6-flash-free",
  "task_timeout_sec": 1800,
  "stall_after_sec": 600,
  "tasks": [{
    "id": "T01", "title": "Services health check",
    "agent": "build", "section": "T01",
    "done": [{"type": "newfile", "dir": "./out", "glob": "health-*.md"}],
    "gate": {"telemetry": "/abs/path/telemetry.jsonl",
             "entries_min": 10, "span_days_min": 7}
  }]
}
```

- `section` maps to a `<!-- === T01 === -->` marker in `PROMPTS.md`.
- Done-check types: `newfile` (glob newer than run start), `minlines`
  (`path` + `min_lines`), `mtime` (`path` newer than run start).
- `gate` (optional): task stays `waiting` until the telemetry file holds
  enough entries over a long enough span.

## Commands

```
python runner.py status [--only T01,..] [--state FILE]
python runner.py dry-run [--only ...]                 # zero side effects
python runner.py run [--only ...] [--model ...] [--state FILE]
                        [--budget USD] [--max-retries N]
python runner.py resume [...]                         # identical to run
python runner.py reset [--only ...]
```

Optional durable layer (needs `pip install "dbos[postgres]>=2.0"` and
`DATABASE_URL` at Postgres; plain `runner.py` needs nothing):

```
python runner_dbos.py launch --only T01 [--estimate 1.0] [--needs-approval]
python runner_dbos.py status --wid taskauto-T01-<stamp>
python runner_dbos.py approve --wid taskauto-T01-<stamp> --approve yes|no
```

Single-worker SQLite budget stores require `TASK_AUTO_SINGLE_WORKER=1`
(multi-worker SQLite is refused — locks can't stop cross-process
overspend). See [docs/durable.md](docs/durable.md).

---

## Documentation

- [docs/architecture.md](docs/architecture.md) — components, state machine, file layout
- [docs/guarantees.md](docs/guarantees.md) — what resume/done-verification/ledger actually promise (and don't)
- [docs/budget.md](docs/budget.md) — ledger schema, caps, settle semantics, known gaps
- [docs/durable.md](docs/durable.md) — DBOS layer: setup, workflows, approvals, provenance
- [docs/comparison.md](docs/comparison.md) — vs Temporal, DBOS Transact alone, LangGraph checkpointing, cron+scripts
- [docs/faq.md](docs/faq.md) — opencode models, Windows/Linux/macOS, CI use, troubleshooting
- [SKILL.md](SKILL.md) — agent-skill contract (frontmatter + resume guarantee + neighbor map)

## Tests

```bash
python tests/test_runner.py       # core: markers, state, checks, gates, ledger, retry
python tests/test_runner_dbos.py  # budget store, approval parsing (no DBOS/Postgres needed)
```

## License

MIT — see [LICENSE](LICENSE).
