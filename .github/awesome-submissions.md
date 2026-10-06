# Awesome-list submissions (drafts — submit as PRs from your account)

Curators reject promotion-only entries. Each draft below states only what
the repo demonstrably contains (code + tests + docs), in the target
list's house style. Submit one at a time; adapt the line to the list's
current section layout before opening the PR.

## 1. awesome-ai-agents style (e.g. awesomelistsio/awesome-ai-agents)

Section: Frameworks (or Orchestration, if the list has one).

```markdown
* [task-automator](https://github.com/Gautham-AI-dev/task-automator) – Crash-safe batch runner for headless AI coding agents: artifact-verified completion, per-attempt cost ledger, stall watchdog, global budget caps, and an optional DBOS + Postgres durable layer with human-approval gates. Stdlib-only core, offline tests, MIT.
```

Why it clears the bar: working code (not a directory entry), test suite
runnable offline, docs with explicit non-goals and known gaps —
curators' stated criteria.

## 2. awesome-workflow-automation style (e.g. dariubs/awesome-workflow-automation)

Section: Tools / Engines (batch + durable execution).

```markdown
* [task-automator](https://github.com/Gautham-AI-dev/task-automator) – Batch-execution harness for AI agent workflows: crash-safe resume, time gates, classified retries, per-attempt cost ledger with budget halt, plus optional durable per-task workflows (DBOS + Postgres) with fail-closed approval. Python stdlib core, MIT.
```

## 3. opencode ecosystem (topic `opencode-skills` / opencode skills collections)

Entry for skill-collection READMEs (e.g. open-hax/opencode-skills style):

```markdown
* [task-automator](https://github.com/Gautham-AI-dev/task-automator) – Execution harness skill for headless `opencode run` batches: crash-safe `state.json` resume, artifact done-verification (`newfile`/`minlines`/`mtime`), telemetry time gates, JSONL cost ledger, stall watchdog, `--budget`/`--max-retries` guards. Ships `SKILL.md` agent contract + offline tests.
```

### Decision 2026-10-06: NO PR to open-hax/opencode-skills (recorded, not deferred)

- That repo accepts skill *directories* (`.opencode/skills/<name>/SKILL.md`),
  not link entries, and its only category is DevSecOps infrastructure
  discovery. A batch-execution harness does not fit its taxonomy.
- Forcing a misfit PR risks a rejection that harms the repo's standing
  with curators. Opencode discoverability is covered by: the
  `opencode-skills` topic on this repo, the `SKILL.md` agent contract
  shipped in-repo, and the two awesome-list PRs above.
- Revisit if: they add an execution/automation category, or a general
  opencode-skills registry emerges (e.g. awesomeskills.dev listing —
  submit there instead; it indexes skill repos by URL).

## Submission checklist (per list)

- [ ] Read the list's CONTRIBUTING.md (format, alphabetical order, section fit).
- [ ] Keep the entry to one line + one link, matching neighbors' style.
- [ ] PR title: `Add task-automator` (or the list's convention).
- [ ] PR body: one sentence on what it is + note the test suite and docs
      (curators check maintenance signals: recent commits, issues enabled,
      license file present — all true for this repo).
- [ ] If rejected, ask what would change their mind; common asks are usage
      examples (point at `tasks.example.json` + `docs/faq.md`) and a demo
      (offer the dry-run transcript).
