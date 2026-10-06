# Critical-path regression gate

`runner.py` + `tests/test_runner.py` + the ledger schema are OSD
critical path: the `automation/` deployment runs production catalogs on
them, and the verification panel (`release/osd-windows/ui/server.py`
`/costs`) renders `ledger.jsonl` directly. A runner regression breaks
reruns AND the dashboard.

## Required before merging ANY change to runner.py / ledger schema

1. `python tests/test_runner.py` → ALL PASS (17 tests, stdlib only).
2. `python tests/test_runner_dbos.py` → ALL PASS (13 tests, no
   DBOS/Postgres needed).
3. `test_consumer_costs_compat` (in suite 1) covers the `/costs`
   contract: old-schema rows, new per-attempt rows, incomplete rows,
   and corrupt lines must all survive `tokens_in/tokens_out/cost`
   summation with `.get()` defaults. If the ledger schema changes, this
   test MUST be extended first, and `server.py costs_html` re-verified
   against a mixed ledger (see note below).
4. Mirror to `skills/task-automator/` + `release/task-automator/`,
   re-run both suites IN EACH COPY, confirm no `docs/docs` or
   `assets/assets` nesting (`Get-ChildItem -Recurse`).

## Verified /costs behavior (2026-10-06, against live code)

- Old rows (no attempt/outcome), new rows, `complete: false` rows, and
  non-JSON lines all render; totals = straight sums (3 mixed rows →
  210 in / 21 out / $0.03 in the test fixture).
- Retried tasks now show one row per attempt — more truthful spend, and
  the totals math is unchanged (addition is addition).

## Explicitly OUT of the gate (kept honest)

- `runner_dbos_workflow.py` + PG branches: reviewed, test-pinned where
  possible (fake-cursor/registry tests), but UNEXECUTED against live
  DBOS+Postgres. Exempt from the gate until a first Postgres run.
- Awesome-list PRs, About/topics, social image: distribution, not
  correctness — no gate.
