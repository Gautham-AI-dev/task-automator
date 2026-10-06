# Guarantees — what is promised, and what is not

## Resume

- **Promised:** any break (Ctrl-C, crash, power loss, timeout, stall)
  leaves `state.json` consistent (tmp + `os.replace` per mutation).
  `resume` re-verifies done tasks, retries failed/stalled, re-checks
  waiting gates. Nothing executes twice silently: re-runs are logged as
  `RE-RUN` with reasons.
- **Not promised:** exactly-once agent execution. A killed agent may have
  partially written artifacts; done-checks (not exit codes) decide.

## Done verification

- **Promised:** `done` requires exit 0 AND every artifact check true
  against mtimes newer than that attempt's `run_start`.
- **Not promised:** semantic correctness of artifacts. A `minlines: 50`
  file with 50 lines of garbage passes. Checks prove existence and
  freshness, not quality — quality is the prompt's job.

## Ledger

- **Promised:** one row per attempt with tokens/cost parsed from
  `step_finish` events; rows from incomplete streams flagged
  (`complete: false`, `partial-step-finish` / `no-step-finish` reasons).
- **Not promised:** authoritative totals when rows are incomplete. Known
  upstream gap (opencode #26855): the CLI can exit on idle before emitting
  the final `step_finish`, so sums are lower bounds. Reconcile via the
  opencode session DB/export before billing anyone.

## Budget cap

- **Promised:** `--budget` halts before the next dispatch and suppresses
  in-loop retries once observed spend reaches the cap.
- **Not promised:** a hard ceiling on true spend. On incomplete ledger
  rows the halt fires late (permissive). Treat the cap as a brake, not a
  vault.

## Retry

- **Promised:** transient classes (timeout/stall, nonzero-exit with
  passing checks) re-dispatch up to `--max-retries`, with the attempt
  counter surviving resumes.
- **Not promised:** success. Exit-0-with-failing-checks (the agent claimed
  success but produced nothing verifiable) and nonzero-with-failing-checks
  never auto-retry — they need a human.
