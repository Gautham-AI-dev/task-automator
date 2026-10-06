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
        # no cap configured -> acquire passes through
        assert s.acquire('run:x', 5.0) is True
        s.set_cap('run:x', 2.0)
        assert s.acquire('run:x', 1.0) is True
        # 1.0 spent of 2.0 cap: another 1.5 must be denied
        assert s.acquire('run:x', 1.5) is False
        # settle 1.0 estimate -> 0.25 actual frees the reservation
        s.settle('run:x', 1.0, 0.25)
        assert s.acquire('run:x', 1.5) is True
    finally:
        del os.environ['TASK_AUTO_SINGLE_WORKER']
    print('PASS budget-sqlite')


def test_budget_sqlite_refused_multiworker():
    os.environ.pop('TASK_AUTO_SINGLE_WORKER', None)
    try:
        m.BudgetStore('sqlite:///:memory:')
    except RuntimeError as e:
        assert 'refused' in str(e), e
    else:
        raise AssertionError('multi-worker SQLite must be refused')
    print('PASS budget-refuse')


if __name__ == '__main__':
    test_import_without_dbos()
    test_workflow_id()
    test_parse_approval_fail_closed()
    test_budget_sqlite_single_worker()
    test_budget_sqlite_refused_multiworker()
    print('ALL PASS')
