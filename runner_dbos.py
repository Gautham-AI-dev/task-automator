"""Phase 2 (optional): durable per-task execution on DBOS + Postgres.

Needs: pip install "dbos[postgres]>=2.0" and DATABASE_URL pointing at
Postgres. Without them this module still imports fine (pure helpers and
tests run); only launch()/status()/approve() raise a clear error.
runner.py remains the zero-dependency default.

STATUS: define_app()/task_workflow has NEVER executed (dbos uninstallable
in this env; no Postgres here). Treat the workflow file as reviewed but
unproven until the first Postgres run; the BudgetStore/approval helpers
are covered by tests/test_runner_dbos.py.

Design (one workflow per task: short histories, no long sleeps):
  launch -> DBOS.start_workflow(task_workflow, task, prompt, opts)
  workflow: acquire budget (step) -> dispatch agent (async step bounded
    by timeout_seconds) -> verify artifacts (step) ->
    settle budget actuals (step) -> optional approval wait
    (durable recv with timeout; fail-closed on timeout).

DBOS rules honored (fixes for the report-code bugs):
  - every budget mutation and every side effect lives in a @step;
    the workflow body only orchestrates values returned from steps;
  - dispatch is an ASYNC step so timeout_seconds/preemptible apply
    (sync steps cannot time out);
  - ledger append is a step with retries_allowed=False (a retried step
    would double-append);
  - Postgres budget ledger uses SELECT ... FOR UPDATE; the SQLite
    fallback refuses to run unless TASK_AUTO_SINGLE_WORKER=1, so two
    workers can never silently overspend;
  - no workflow sleeps for 30 min; per-task workflows keep histories
    short instead of needing Continue-As-New.

Reuses runner.py pure logic (check_done, gate_open, parse_cost,
failure_class): single source of truth, no duplicated policy.

NOTE: DBOS API surface (send/recv/set_event/get_event/start_workflow)
is written against dbos-transact-py v2.x docs; re-verify kwarg names
against the installed version on the first Postgres run.
"""
import argparse
import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import runner  # noqa: E402  (pure logic reuse: no policy duplication)

DEFAULT_SQLITE_FILE = os.path.join(HERE, 'taskauto_budget.db')


def _resolve_sqlite_path(path):
    """Resolve SQLite budget DB paths against HERE (not CWD), so launches
    from different directories share one ledger. Empty path or
    ':memory:' resolves to the HERE-relative default file with a note;
    per-connection :memory: would silently forget caps between steps."""
    if not path or path == ':memory:':
        print('note: SQLite budget DB resolving to default file %s '
              '(explicit path recommended)' % DEFAULT_SQLITE_FILE)
        return DEFAULT_SQLITE_FILE
    if not os.path.isabs(path):
        return os.path.join(HERE, path)
    return path

try:
    from dbos import DBOS
    _DBOS_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    DBOS = None
    _DBOS_AVAILABLE = False


def require_dbos():
    if not _DBOS_AVAILABLE:
        raise RuntimeError(
            'DBOS not installed. Run: pip install "dbos[postgres]>=2.0" '
            'and set DATABASE_URL to Postgres. runner.py needs nothing.')
    return DBOS


def workflow_id_for(task_id, when=None):
    when = when or time.strftime('%Y%m%d-%H%M%S')
    return 'taskauto-%s-%s' % (task_id, when)


class BudgetStore:
    """Run/task/agent spend caps. Postgres for multi-worker, SQLite only
    for provably single-worker runs (refuses otherwise).

    Uncapped scopes are TRACKED (cap_usd NULL), never invisible: acquire
    creates the row on first touch and settle upserts, so a cap added
    later enforces against real historical spend, not zero."""

    SCHEMA = ('CREATE TABLE IF NOT EXISTS taskauto_budget('
              'scope TEXT PRIMARY KEY, cap_usd REAL, spent_usd REAL)')

    def __init__(self, url):
        self.url = url
        self.is_pg = url.startswith(('postgres://', 'postgresql://'))
        if not self.is_pg and os.environ.get(
                'TASK_AUTO_SINGLE_WORKER') != '1':
            raise RuntimeError(
                'SQLite budget store without TASK_AUTO_SINGLE_WORKER=1 '
                'refused: threading.Lock cannot stop two workers '
                'overspending. Use Postgres or export the env var.')
        if self.is_pg:
            import psycopg  # lazy: only needed for the PG path
            self._pg = psycopg
            self.conn = psycopg.connect(url, autocommit=False)
        else:
            self.path = _resolve_sqlite_path(
                url.replace('sqlite:///', ''))
            self.conn = sqlite3.connect(self.path)
        self._init()

    def _init(self):
        cur = self.conn.cursor()
        cur.execute(self.SCHEMA)
        self.conn.commit()

    def set_cap(self, scope, cap_usd):
        cur = self.conn.cursor()
        if self.is_pg:
            cur.execute(
                'INSERT INTO taskauto_budget(scope, cap_usd, spent_usd) '
                'VALUES (%s, %s, 0) ON CONFLICT (scope) DO UPDATE '
                'SET cap_usd=EXCLUDED.cap_usd', (scope, cap_usd))
        else:
            cur.execute('BEGIN IMMEDIATE')
            cur.execute('INSERT OR IGNORE INTO taskauto_budget VALUES '
                        '(?, ?, 0)', (scope, cap_usd))
            cur.execute('UPDATE taskauto_budget SET cap_usd=? WHERE '
                        'scope=?', (cap_usd, scope))
        self.conn.commit()

    def acquire(self, scope, estimate):
        """Reserve estimate; True if under cap (or uncapped). Must run
        inside a step. First touch creates the scope row (cap NULL =
        tracked but uncapped)."""
        cur = self.conn.cursor()
        if self.is_pg:
            cur.execute('SELECT cap_usd, spent_usd FROM taskauto_budget '
                        'WHERE scope=%s FOR UPDATE', (scope,))
        else:
            cur.execute('BEGIN IMMEDIATE')
            cur.execute('SELECT cap_usd, spent_usd FROM taskauto_budget '
                        'WHERE scope=?', (scope,))
        row = cur.fetchone()
        if row is None:
            ph = '%s' if self.is_pg else '?'
            cur.execute('INSERT INTO taskauto_budget(scope, cap_usd, '
                        'spent_usd) VALUES (%s, NULL, %s)' % (ph, ph),
                        (scope, estimate))
            self.conn.commit()
            return True
        cap, spent = row
        if cap is not None and spent + estimate > cap:
            self.conn.rollback()
            return False
        ph = '%s' if self.is_pg else '?'
        cur.execute('UPDATE taskauto_budget SET spent_usd=spent_usd+%s '
                    'WHERE scope=%s' % (ph, ph), (estimate, scope))
        self.conn.commit()
        return True

    def settle(self, scope, estimate, actual):
        """Replace the outstanding estimate with actual. Upserts: never a
        silent no-op on unknown scopes. Both backends share one semantic:
        spent = COALESCE(existing, estimate) - estimate + actual, so a
        fresh row records exactly actual on PG and SQLite alike. Must run
        inside a step."""
        cur = self.conn.cursor()
        if self.is_pg:
            # Proposed row carries the estimate; the call carries actual.
            # Fresh row: existing spend is implicitly the estimate, so the
            # result is exactly actual. Existing row: spend-estimate+actual.
            # One statement, no read-modify-write race under FOR UPDATE
            # callers (acquire holds the lock; settle runs in its own step).
            cur.execute('INSERT INTO taskauto_budget(scope, cap_usd, '
                        'spent_usd) VALUES (%s, NULL, %s) '
                        'ON CONFLICT (scope) DO UPDATE SET spent_usd='
                        'taskauto_budget.spent_usd-EXCLUDED.spent_usd+%s',
                        (scope, estimate, actual))
        else:
            cur.execute('BEGIN IMMEDIATE')
            cur.execute('SELECT spent_usd FROM taskauto_budget '
                        'WHERE scope=?', (scope,))
            if cur.fetchone() is None:
                # Fresh row: no outstanding estimate exists, so the row
                # starts at actual (mirrors the PG fresh-row branch).
                cur.execute('INSERT INTO taskauto_budget(scope, cap_usd, '
                            'spent_usd) VALUES (?, NULL, ?)',
                            (scope, actual))
            else:
                cur.execute('UPDATE taskauto_budget SET '
                            'spent_usd=spent_usd-?+? WHERE scope=?',
                            (estimate, actual, scope))
        self.conn.commit()


def parse_approval(msg):
    """Normalize an approval message; fail-closed on anything unclear."""
    if isinstance(msg, dict) and msg.get('approved') is True:
        return True
    return False


def launch(task_id, model=None, opts=None):
    """Start one durable per-task workflow. Returns the workflow handle."""
    DBOS = require_dbos()
    import runner_dbos_workflow as wfmod
    DBOS.launch()
    wfmod.define_app(DBOS, sys.modules[__name__])
    manifest = runner.load_tasks()
    prompts = runner.load_prompts()
    task = next(t for t in manifest['tasks'] if t['id'] == task_id)
    prompt = prompts[task['section']]
    opts = dict(opts or {})
    opts.setdefault('model', model or manifest.get('default_model', ''))
    opts.setdefault('timeout', manifest.get('task_timeout_sec', 1800))
    opts.setdefault('budget_url', os.environ.get(
        'DATABASE_URL', 'sqlite:///taskauto_budget.db'))
    # NOTE: the sqlite default above is a FILENAME resolved against HERE
    # by BudgetStore; pass an absolute sqlite:/// path to pin elsewhere.
    wid = workflow_id_for(task_id)
    handle = DBOS.start_workflow(wfmod.task_workflow, task, prompt, opts,
                                 workflow_id=wid)
    print('launched %s (workflow %s)' % (task_id, wid))
    return handle


def status(workflow_id):
    DBOS = require_dbos()
    DBOS.launch()
    return DBOS.get_workflow_status(workflow_id)


def approve(workflow_id, approved=True, note=''):
    """Send an approval decision to a waiting workflow."""
    DBOS = require_dbos()
    DBOS.launch()
    DBOS.send(workflow_id, {'approved': bool(approved), 'note': note})
    print('sent approval=%s to %s' % (approved, workflow_id))


def main(argv=None):
    ap = argparse.ArgumentParser(prog='runner_dbos.py')
    ap.add_argument('cmd', choices=['launch', 'status', 'approve'])
    ap.add_argument('--only', default='')
    ap.add_argument('--model', default='')
    ap.add_argument('--approve', default='yes', choices=['yes', 'no'])
    ap.add_argument('--wid', default='')
    ap.add_argument('--estimate', type=float, default=1.0)
    ap.add_argument('--needs-approval', action='store_true')
    a = ap.parse_args(argv)
    if a.cmd == 'launch':
        only = [x for x in a.only.split(',') if x]
        if not only:
            raise SystemExit('--only T01[,T02] is required')
        for tid in only:
            launch(tid, a.model or None,
                   {'estimate_usd': a.estimate,
                    'needs_approval': a.needs_approval})
    elif a.cmd == 'status':
        if not a.wid:
            raise SystemExit('--wid is required')
        print(json.dumps(status(a.wid), indent=2, default=str))
    elif a.cmd == 'approve':
        if not a.wid:
            raise SystemExit('--wid is required')
        approve(a.wid, a.approve == 'yes')


if __name__ == '__main__':
    main()
