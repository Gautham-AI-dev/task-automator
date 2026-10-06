"""Offline tests for runner_dbos (no DBOS, no Postgres needed)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import runner_dbos as m


def test_import_without_dbos():
    # Environment-dependent: if dbos IS installed here, there is nothing
    # to assert about the missing-dependency path (require_dbos succeeds
    # by design). Skip instead of failing on environment grounds.
    if m._DBOS_AVAILABLE:
        print('SKIP import-without-dbos (dbos installed in this env)')
        return
    try:
        m.require_dbos()
    except RuntimeError as e:
        assert 'pip install "dbos[postgres]' in str(e), e
    else:
        raise AssertionError('require_dbos should raise without dbos')
    print('PASS import-without-dbos')


def test_workflow_id():
    assert m.workflow_id_for('T01', 'X').startswith('taskauto-T01-')
    print('PASS workflow-id')


def test_parse_approval_fail_closed():
    assert m.parse_approval({'approved': True}) is True
    for bad in ({'approved': False}, {'approved': 'yes'}, {}, None,
                'approved', {'note': 'lgtm'}):
        assert m.parse_approval(bad) is False, bad
    print('PASS approval-fail-closed')


def test_budget_sqlite_single_worker():
    os.environ['TASK_AUTO_SINGLE_WORKER'] = '1'
    try:
        d = tempfile.mkdtemp()
        s = m.BudgetStore('sqlite:///' + os.path.join(d, 'b.db'))
        # first touch on an uncapped scope: tracked, not invisible
        assert s.acquire('run:x', 5.0) is True
        cur = s.conn.cursor()
        cur.execute('SELECT cap_usd, spent_usd FROM taskauto_budget '
                    'WHERE scope=?', ('run:x',))
        cap, spent = cur.fetchone()
        assert cap is None and abs(spent - 5.0) < 1e-9, (cap, spent)
        s.set_cap('run:x', 12.0)  # cap added later sees real history:
        assert s.acquire('run:x', 8.0) is False  # 5 + 8 > 12
        assert s.acquire('run:x', 1.0) is True
        # settle 1.0 estimate -> 0.25 actual frees the reservation
        s.settle('run:x', 1.0, 0.25)
        assert s.acquire('run:x', 6.75) is True  # 5.25 + 6.75 = 12.0
    finally:
        del os.environ['TASK_AUTO_SINGLE_WORKER']
    print('PASS budget-sqlite')


def test_budget_settle_upsert():
    # settle must create the row when absent (never a silent no-op) with
    # ONE semantic on both backends: fresh row records exactly actual
    # (PG fresh-row branch parity); acquire->settle nets to actual.
    os.environ['TASK_AUTO_SINGLE_WORKER'] = '1'
    try:
        d = tempfile.mkdtemp()
        s = m.BudgetStore('sqlite:///' + os.path.join(d, 'b.db'))
        s.settle('run:fresh', 1.0, 0.4)  # never acquired: fresh row
        cur = s.conn.cursor()
        cur.execute('SELECT spent_usd FROM taskauto_budget WHERE scope=?',
                    ('run:fresh',))
        assert abs(cur.fetchone()[0] - 0.4) < 1e-9
        s.acquire('run:n', 1.0)
        s.settle('run:n', 1.0, 0.25)
        cur.execute('SELECT spent_usd FROM taskauto_budget WHERE scope=?',
                    ('run:n',))
        assert abs(cur.fetchone()[0] - 0.25) < 1e-9
    finally:
        del os.environ['TASK_AUTO_SINGLE_WORKER']
    print('PASS budget-settle-upsert')


def test_budget_sqlite_path_resolution():
    os.environ['TASK_AUTO_SINGLE_WORKER'] = '1'
    try:
        # relative filename resolves against HERE, not CWD
        s = m.BudgetStore('sqlite:///rel-budget.db')
        assert os.path.dirname(os.path.abspath(s.path)) == m.HERE, s.path
        s.conn.close()
        os.remove(s.path)
        # empty path falls back to the HERE-relative default file
        s2 = m.BudgetStore('sqlite:///')
        assert s2.path == m.DEFAULT_SQLITE_FILE, s2.path
        s2.conn.close()
        os.remove(s2.path)
    finally:
        del os.environ['TASK_AUTO_SINGLE_WORKER']
    print('PASS budget-paths')


def test_budget_store_closes():
    # context-manager use must close the connection (no per-step leak)
    os.environ['TASK_AUTO_SINGLE_WORKER'] = '1'
    try:
        d = tempfile.mkdtemp()
        with m.BudgetStore('sqlite:///' + os.path.join(d, 'b.db')) as s:
            assert s.acquire('run:x', 1.0) is True
        try:
            s.conn.execute('SELECT 1')
        except Exception:
            pass  # closed: ProgrammingError expected (impl detail)
        else:
            raise AssertionError('connection should be closed after with')
    finally:
        del os.environ['TASK_AUTO_SINGLE_WORKER']
    print('PASS budget-close')


def test_budget_sqlite_refused_multiworker():
    os.environ.pop('TASK_AUTO_SINGLE_WORKER', None)
    try:
        m.BudgetStore('sqlite:///:memory:')
    except RuntimeError as e:
        assert 'refused' in str(e), e
    else:
        raise AssertionError('multi-worker SQLite must be refused')
    print('PASS budget-refuse')


def test_failure_class_zero_exit():
    import runner
    assert runner.failure_class(0, False, [False]) == (
        False, 'zero-exit-checks-fail')
    print('PASS failure-class-zero')


def test_pg_settle_statement_shape():    # No live Postgres here: pin the SQL contract with a fake cursor.
    # Fresh row must store ACTUAL (params[1]); conflict branch must
    # compute spent - estimate + actual (params[2], params[3]).
    seen = {}

    class FakeCur:
        def execute(self, sql, params):
            seen['sql'] = sql
            seen['params'] = params

    store = m.BudgetStore.__new__(m.BudgetStore)
    store.is_pg = True
    store.conn = type('C', (), {'cursor': lambda self: FakeCur(),
                                'commit': lambda self: None})()
    store.settle('run:x', 1.0, 0.4)
    sql, p = seen['sql'], seen['params']
    assert 'ON CONFLICT (scope) DO UPDATE' in sql, sql
    assert p[0] == 'run:x' and p[1] == 0.4, p  # fresh row stores actual
    assert p[2] == 1.0 and p[3] == 0.4, p  # update: spent-est+actual
    assert 'EXCLUDED.spent_usd' not in sql, sql  # would be actual, wrong
    print('PASS pg-settle-shape')


def test_pg_acquire_lost_race():
    # Lost first-touch race: RETURNING comes back empty (winner inserted),
    # re-SELECT finds the winner's row, reservation proceeds normally.
    # No UniqueViolation may escape: ON CONFLICT DO NOTHING is mandatory.
    script = iter([
        None,   # initial SELECT ... FOR UPDATE: no row yet
        None,   # RETURNING: empty -> we lost the race
        (None, 5.0),  # re-SELECT: winner's row (uncapped, spent 5)
    ])
    seen = []

    class FakeCur:
        def execute(self, sql, params):
            seen.append((sql, params))

        def fetchone(self):
            return next(script)

    store = m.BudgetStore.__new__(m.BudgetStore)
    store.is_pg = True
    store.conn = type('C', (), {'cursor': lambda self: FakeCur(),
                                'commit': lambda self: seen.append(
                                    ('COMMIT', ())),
                                'rollback': lambda self: seen.append(
                                    ('ROLLBACK', ()))})()
    assert store.acquire('run:default', 1.0) is True
    assert any('ON CONFLICT (scope) DO NOTHING' in s for s, _ in seen), seen
    assert any('RETURNING scope' in s for s, _ in seen), seen
    assert any(s == 'COMMIT' for s, _ in seen), seen
    print('PASS pg-acquire-race')


def test_pg_acquire_won_race():
    # Won first-touch race: RETURNING yields the scope; single commit,
    # no follow-up UPDATE (estimate already stored by our INSERT).
    script = iter([None, ('run:x',)])

    class FakeCur:
        def __init__(self):
            self.statements = []

        def execute(self, sql, params):
            self.statements.append((sql, params))

        def fetchone(self):
            return next(script)

    cur = FakeCur()
    store = m.BudgetStore.__new__(m.BudgetStore)
    store.is_pg = True
    store.conn = type('C', (), {'cursor': lambda self: cur,
                                'commit': lambda self: cur.statements.append(
                                    ('COMMIT', ())),
                                'rollback': lambda self: None})()
    assert store.acquire('run:x', 2.0) is True
    assert not any(s.startswith('UPDATE') for s, _ in cur.statements), \
        cur.statements
    print('PASS pg-acquire-won')


def test_launch_uses_registry_not_module_attr():
    # Regression: launch() once read wfmod.task_workflow (AttributeError:
    # no module-level attribute; the workflow lives in define_app's
    # closure). It must use the dict define_app returns. Fake DBOS +
    # fake workflow module exercise the wiring without dbos installed.
    import unittest.mock as mock
    sentinel = object()
    started = {}

    class FakeDBOS:
        def launch(self):
            pass

        def start_workflow(self, fn, task, prompt, opts, workflow_id=None):
            started['fn'] = fn
            started['wid'] = workflow_id
            return 'handle'

    fake_wfmod = type('W', (), {
        'define_app': staticmethod(lambda dbos, mod: {
            'task_workflow': sentinel}),
        })()
    manifest = {'default_model': 'm', 'tasks': [
        {'id': 'T01', 'title': 't', 'agent': 'a', 'section': 'T01'}]}
    import sys
    # launch() does a function-local `import runner_dbos_workflow`, so
    # sys.modules patching is the seam (no dbos installation needed).
    with mock.patch.object(m, 'require_dbos', return_value=FakeDBOS()), \
         mock.patch.object(sys, 'modules',
                           {**sys.modules,
                            'runner_dbos_workflow': fake_wfmod}), \
         mock.patch('runner.load_tasks', return_value=manifest), \
         mock.patch('runner.load_prompts', return_value={'T01': 'p'}), \
         mock.patch.object(m, 'workflow_id_for',
                           return_value='taskauto-T01-X'):
        h = m.launch('T01')
    assert h == 'handle', h
    assert started['fn'] is sentinel, started  # registry fn, not module attr
    assert started['wid'] == 'taskauto-T01-X', started
    print('PASS launch-registry')


if __name__ == '__main__':
    test_import_without_dbos()
    test_workflow_id()
    test_parse_approval_fail_closed()
    test_budget_sqlite_single_worker()
    test_budget_settle_upsert()
    test_budget_sqlite_path_resolution()
    test_budget_store_closes()
    test_budget_sqlite_refused_multiworker()
    test_failure_class_zero_exit()
    test_pg_settle_statement_shape()
    test_pg_acquire_lost_race()
    test_pg_acquire_won_race()
    test_launch_uses_registry_not_module_attr()
    print('ALL PASS')
