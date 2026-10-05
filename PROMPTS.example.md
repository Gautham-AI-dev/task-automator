# Example prompts (rename to PROMPTS.md, edit paths, run)

Write outputs to absolute paths. Never invent IDs, numbers, or findings.

<!-- === T01 === -->
## T01. Services health check

You are build. List the expected local services (edit: ports,
container names), record running/stopped + port bindings. Write
`<OUT>/health-<YYYY-MM-DD>.md` with a status table. Read-only probes;
change nothing.
Done: health table exists, anomalies filed with severity.

<!-- === T02 === -->
## T02. Literature digest

You are build. Search your literature sources for the last 14 days on
YOUR topic (edit me). Tabulate title/journal/date/finding/identifier.
Write `<OUT>/digest-<YYYY-MM-DD>.md` with top-5 ranking + synthesis.
Done: >=5 entries with real identifiers, no invented DOIs.

<!-- === T03 === -->
## T03. Dependency review

You are build. Audit the dependency manifests in YOUR repo (edit:
paths). List outdated/vulnerable packages with versions, propose
upgrades with rollback notes. Change nothing.
Write `<OUT>/review-<YYYY-MM-DD>.md`.
Done: per-package table + proposals, zero env changes.
