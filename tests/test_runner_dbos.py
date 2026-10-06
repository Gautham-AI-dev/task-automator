"""Offline tests for runner_dbos (no DBOS, no Postgres needed)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import runner_dbos as m


def test_import_without_dbos():
    assert m._DBOS_AVAILABLE is False  # not installed in CI fixture
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
    # settle on a never-acquired scope must create the row, not no-op;
    # semantics: spent = spent - estimate + actual.
    os.environ['TASK_AUTO_SINGLE_WORKER'] = '1'
    try:
        d = tempfile.mkdtemp()
        s = m.BudgetStore('sqlite:///' + os.path.join(d, 'b.db'))
        s.acquire('run:new', 1.0)   # tracked spend = 1.0
        s.settle('run:new', 1.0, 0.4)  # reservation replaced by actual
        cur = s.conn.cursor()
        cur.execute('SELECT spent_usd FROM taskauto_budget WHERE scope=?',
                    ('run:new',))
        assert abs(cur.fetchone()[0] - 0.4) < 1e-9
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


if __name__ == '__main__':
    test_import_without_dbos()
    test_workflow_id()
    test_parse_approval_fail_closed()
    test_budget_sqlite_single_worker()
    test_budget_settle_upsert()
    test_budget_sqlite_path_resolution()
    test_budget_sqlite_refused_multiworker()
    test_failure_class_zero_exit()
    print('ALL PASS')
