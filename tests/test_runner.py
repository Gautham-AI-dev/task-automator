"""Offline tests for task-automator runner (no opencode needed)."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import runner


def parse_markers(text):
    """Same algorithm as runner.load_prompts (kept in sync by test)."""
    out, cur = {}, None
    for line in text.splitlines():
        if line.startswith('<!-- === ') and line.endswith(' === -->'):
            cur = line[9:-8]
            out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    return {k: '\n'.join(v).strip() for k, v in out.items()}


def test_markers():
    out = parse_markers('<!-- === A1 === -->\nhello\n<!-- === B2 === -->\nworld\n')
    assert out == {'A1': 'hello', 'B2': 'world'}, out
    # regression: the [10:-9] off-by-one that shipped once
    out2 = parse_markers('<!-- === T01 === -->\nx\n')
    assert 'T01' in out2, out2
    print('PASS markers')


def test_state_crashsafe():
    d = tempfile.mkdtemp()
    p = os.path.join(d, 'state.json')
    runner.save_state(p, {'tasks': {'T1': {'status': 'done'}}})
    assert not os.path.exists(p + '.tmp')
    assert json.load(open(p))['tasks']['T1']['status'] == 'done'
    print('PASS state')


def test_done_checks():
    import time
    d = tempfile.mkdtemp()
    f1 = os.path.join(d, 'out-2024.md')
    open(f1, 'w').write('x\n' * 60)
    now = time.time()
    os.utime(f1, (now + 5, now + 5))  # newer than run start
    assert runner.check_done({'type': 'newfile', 'dir': d,
                              'glob': 'out-*.md'}, now) is True
    assert runner.check_done({'type': 'minlines', 'path': f1,
                              'min_lines': 50}, now) is True
    assert runner.check_done({'type': 'minlines', 'path': f1,
                              'min_lines': 5000}, now) is False
    assert runner.check_done({'type': 'mtime', 'path': f1,
                              'newer_than_run': True}, now) is True
    assert runner.check_done({'type': 'newfile', 'dir': d,
                              'glob': 'nope-*.md'}, now) is False
    print('PASS done-checks')


def test_gate():
    assert runner.gate_open({}) == (True, '')
    d = tempfile.mkdtemp()
    p = os.path.join(d, 't.jsonl')
    open(p, 'w').write('{"timestamp": "2026-09-20T00:00:00Z"}\n'
                       '{"timestamp": "2026-09-30T00:00:00Z"}\n')
    ok, why = runner.gate_open({'gate': {'telemetry': p, 'entries_min': 10,
                                         'span_days_min': 7}})
    assert ok is False and 'entries' in why, (ok, why)
    print('PASS gate')


def test_cost_parse():
    d = tempfile.mkdtemp()
    p = os.path.join(d, 'x.log')
    with open(p, 'w') as f:
        f.write('CMD: test\n')
        f.write('{"type":"step_finish","part":{"tokens":{"input":81118,'
                '"output":15},"cost":0}}\n')
        f.write('not json\n')
        f.write('{"type":"step_finish","part":{"tokens":{"input":100,'
                '"output":5},"cost":0.002}}\n')
    c = runner.parse_cost(p)
    assert c['tokens_in'] == 81218 and c['tokens_out'] == 20, c
    assert c['cost'] == 0.002 and c['complete'] is True, c
    assert runner.parse_cost(os.path.join(d, 'missing.log'))['complete'] \
        is False
    print('PASS cost')


def test_cost_incomplete():
    # opencode #26855: log with text but no step_finish must be flagged,
    # not silently counted as zero-cost authoritative.
    import tempfile
    d = tempfile.mkdtemp()
    p = os.path.join(d, 'y.log')
    with open(p, 'w') as f:
        f.write('CMD: test\n')
        f.write('{"type":"step_start","part":{}}\n')
        f.write('{"type":"text","part":{"text":"hi"}}\n')
    c = runner.parse_cost(p)
    assert c['complete'] is False and 'no-step-finish' in c['reason'], c
    assert c['tokens_in'] == 0 and c['cost'] == 0.0, c
    print('PASS cost-incomplete')


def test_cost_partial_finish():
    # #26855 exact mode: intermediate step_finish present, final missing
    # (last event is text). Sum exists but must NOT count as complete.
    import tempfile
    d = tempfile.mkdtemp()
    p = os.path.join(d, 'z.log')
    with open(p, 'w') as f:
        f.write('{"type":"step_start","part":{}}\n')
        f.write('{"type":"step_finish","part":{"tokens":{"input":50,'
                '"output":5},"cost":0.01}}\n')
        f.write('{"type":"text","part":{"text":"tail with no finish"}}\n')
    c = runner.parse_cost(p)
    assert c['complete'] is False and 'partial-step-finish' in c['reason'], c
    assert c['tokens_in'] == 50 and c['cost'] == 0.01, c
    print('PASS cost-partial')


def test_failure_class():
    assert runner.failure_class(124, True, [True]) == (True, 'timeout/stall')
    assert runner.failure_class(1, False, [True]) == (
        True, 'nonzero-exit-checks-pass')
    r, label = runner.failure_class(1, False, [False])
    assert r is False and label == 'nonzero-exit-checks-fail', (r, label)
    print('PASS failure-class')


def test_ledger_total():
    import tempfile
    d = tempfile.mkdtemp()
    p = os.path.join(d, 'ledger.jsonl')
    open(p, 'w').write('{"task":"T1","cost":0.5}\n{"task":"T2"}\nnope\n')
    assert abs(runner.ledger_total(p) - 0.5) < 1e-9
    assert runner.ledger_total(os.path.join(d, 'missing.jsonl')) == 0.0
    print('PASS ledger-total')


def test_ledger_per_attempt_rows():
    # retried tasks write one row PER ATTEMPT with attempt/outcome fields
    import json
    import tempfile
    import unittest.mock as mock
    d = tempfile.mkdtemp()
    out = os.path.join(d, 'out')
    os.makedirs(out)
    spath = os.path.join(d, 'state.json')
    man, prompts = _mini_manifest(out), {'T01': 'do t1'}
    logdir = os.path.join(d, 'logs')
    os.makedirs(logdir)

    def fake_dispatch(task, prompt, model, timeout, ld, stall_after):
        with open(os.path.join(logdir, task['id'] + '.log'), 'w') as lf:
            lf.write('{"type":"step_finish","part":{"tokens":{"input":1,'
                     '"output":1},"cost":0.5}}\n')
        import time as _t
        now = _t.time() + 5
        f = os.path.join(out, 'health-x.md')
        open(f, 'w').write('x')
        os.utime(f, (now, now))
        return 1, False

    with mock.patch.object(runner, 'HERE', d), \
         mock.patch.object(runner, 'dispatch', fake_dispatch):
        runner.run_tasks(man, prompts, {'tasks': {}}, spath, ['T01'], 'm',
                         60, False, None, 1)
    rows = [json.loads(ln) for ln in
            open(os.path.join(d, 'ledger.jsonl')) if ln.strip()]
    assert len(rows) == 2, rows
    assert [r['attempt'] for r in rows] == [1, 2], rows
    # deterministic: rc=1 both attempts, retries exhausted after attempt 2
    # (max_retries=1), so BOTH rows are attempt-failed — pinned, not or-ed.
    assert [r['outcome'] for r in rows] == ['attempt-failed',
                                           'attempt-failed'], rows
    assert abs(runner.ledger_total(os.path.join(d, 'ledger.jsonl'))
               - 1.0) < 1e-9
    print('PASS ledger-per-attempt')


def test_ledger_rejects_unknown_outcome():
    # schema guard: 'unknown' (or anything outside the enum) fails loud at
    # the call site instead of writing a bad row.
    import tempfile
    d = tempfile.mkdtemp()
    p = os.path.join(d, 'ledger.jsonl')
    try:
        runner.ledger_append(p, 'T1', {'cost': 0.0}, 1, 'unknown')
    except ValueError:
        pass
    else:
        raise AssertionError('unknown outcome must raise')
    print('PASS ledger-enum-guard')


def test_dry_run_ignores_budget_halt():
    # dry-run previews ALL tasks even over cap (advisory, not halt);
    # writes no state.
    import io
    import json
    import tempfile
    import unittest.mock as mock
    from contextlib import redirect_stdout
    d = tempfile.mkdtemp()
    out = os.path.join(d, 'out')
    os.makedirs(out)
    spath = os.path.join(d, 'state.json')
    led = os.path.join(d, 'ledger.jsonl')
    open(led, 'w').write(json.dumps({'task': 'T9', 'cost': 9.0}) + '\n')
    man, prompts = _mini_manifest(out), {'T01': 'do t1'}
    buf = io.StringIO()
    with mock.patch.object(runner, 'HERE', d), redirect_stdout(buf):
        st = {'tasks': {}}
        runner.run_tasks(man, prompts, st, spath, None, 'm', 60,
                         True, 1.0, 0)  # dry=True, breached cap
        assert st == {'tasks': {}}, st  # nothing written
    text = buf.getvalue()
    assert 'dry-run ok' in text, text  # previewed, not halted
    assert 'WOULD-HALT' in text, text  # advisory present
    print('PASS dry-run-budget')


def test_rerun_resets_attempts():
    # RE-RUN after vanished artifacts starts a FRESH attempt sequence
    # (prior count preserved in prior_attempts, not inherited).
    import tempfile
    import unittest.mock as mock
    d = tempfile.mkdtemp()
    out = os.path.join(d, 'out')
    os.makedirs(out)
    spath = os.path.join(d, 'state.json')
    runner.save_state(spath, {'tasks': {'T01': {
        'status': 'done', 'finished_at': 'old',
        'run_start': 1.0, 'attempt': 3}}})  # prior run did 3 attempts
    man, prompts = _mini_manifest(out), {'T01': 'do t1'}
    logdir = os.path.join(d, 'logs')
    os.makedirs(logdir)

    def fake_dispatch(task, prompt, model, timeout, ld, stall_after):
        with open(os.path.join(logdir, task['id'] + '.log'), 'w') as lf:
            lf.write('{"type":"step_finish","part":{"tokens":{"input":1,'
                     '"output":1},"cost":0}}\n')
        return 0, False  # exit 0; no artifact -> FAILED, attempt recorded

    with mock.patch.object(runner, 'HERE', d), \
         mock.patch.object(runner, 'dispatch', fake_dispatch):
        runner.run_tasks(man, prompts, runner.load_state(spath), spath,
                         ['T01'], 'm', 60, False, None, 0)
        rec = runner.load_state(spath)['tasks']['T01']
        assert rec['attempt'] == 1, rec  # fresh sequence, not 4
        assert rec.get('prior_attempts') == 3, rec  # history kept
    print('PASS rerun-attempts')


def test_retry_suppressed_over_budget():
    # cap breached BY the first attempt's own cost: the retry (not the
    # initial dispatch) is suppressed. Task-level halt covers the
    # pre-breached case separately.
    import tempfile
    import unittest.mock as mock
    d = tempfile.mkdtemp()
    out = os.path.join(d, 'out')
    os.makedirs(out)
    spath = os.path.join(d, 'state.json')
    man, prompts = _mini_manifest(out), {'T01': 'do t1'}
    logdir = os.path.join(d, 'logs')
    os.makedirs(logdir)
    calls = []

    def fake_dispatch(task, prompt, model, timeout, ld, stall_after):
        calls.append(1)
        with open(os.path.join(logdir, task['id'] + '.log'), 'w') as lf:
            lf.write('{"type":"step_finish","part":{"tokens":{"input":1,'
                     '"output":1},"cost":5.0}}\n')  # blows the 1.0 cap
        import time as _t
        now = _t.time() + 5
        f = os.path.join(out, 'health-x.md')
        open(f, 'w').write('x')
        os.utime(f, (now, now))
        return 1, False  # transient: nonzero-exit, checks pass

    with mock.patch.object(runner, 'HERE', d), \
         mock.patch.object(runner, 'dispatch', fake_dispatch):
        st = {'tasks': {}}
        runner.run_tasks(man, prompts, st, spath, ['T01'], 'm', 60,
                         False, 1.0, 3)
        assert len(calls) == 1, calls  # initial attempt only, no retry
        rec = runner.load_state(spath)['tasks']['T01']
        assert 'retry suppressed' in rec['reason'], rec
    print('PASS retry-budget-suppress')


def _mini_manifest(outdir):
    return {'default_model': 'm', 'stall_after_sec': 600, 'tasks': [
        {'id': 'T01', 'title': 't1', 'agent': 'a', 'section': 'T01',
         'done': [{'type': 'newfile', 'dir': outdir,
                   'glob': 'health-*.md'}]}]}


def test_legacy_done_reruns():
    # pre-upgrade done entry without run_start: fail-safe re-run path,
    # then carries run_start. dispatch is STUBBED: this test must never
    # launch a real opencode process, even where opencode is installed.
    import tempfile
    import unittest.mock as mock
    d = tempfile.mkdtemp()
    out = os.path.join(d, 'out')
    os.makedirs(out)
    spath = os.path.join(d, 'state.json')
    runner.save_state(spath, {'tasks': {'T01': {'status': 'done',
                                                'finished_at': 'old'}}})
    man, prompts = _mini_manifest(out), {'T01': 'do t1'}
    logdir = os.path.join(d, 'logs')
    os.makedirs(logdir)
    open(os.path.join(logdir, 'T01.log'), 'w').write(
        '{"type":"step_finish","part":{"tokens":{"input":1,"output":1},'
        '"cost":0}}\n')
    f = os.path.join(out, 'health-x.md')
    open(f, 'w').write('x')
    import time
    os.utime(f, (time.time() + 5, time.time() + 5))

    def fake_dispatch(task, prompt, model, timeout, ld, stall_after):
        return 0, False  # exit 0; artifact above satisfies done-check

    with mock.patch.object(runner, 'HERE', d), \
         mock.patch.object(runner, 'dispatch', fake_dispatch):
        st = runner.load_state(spath)
        runner.run_tasks(man, prompts, st, spath, ['T01'], 'm', 60,
                         False, None, 0)
        st2 = runner.load_state(spath)
        assert st2['tasks']['T01'].get('run_start'), st2
    print('PASS legacy-done')


def test_retry_loop():
    # transient failure (rc=1, checks pass) with max_retries=1: dispatch
    # called twice (initial + 1 retry), final state records attempt 2.
    import tempfile
    import unittest.mock as mock
    d = tempfile.mkdtemp()
    out = os.path.join(d, 'out')
    os.makedirs(out)
    spath = os.path.join(d, 'state.json')
    man, prompts = _mini_manifest(out), {'T01': 'do t1'}
    calls = []
    logdir = os.path.join(d, 'logs')
    os.makedirs(logdir)

    def fake_dispatch(task, prompt, model, timeout, ld, stall_after):
        calls.append(1)
        with open(os.path.join(logdir, task['id'] + '.log'), 'w') as lf:
            lf.write('{"type":"step_finish","part":{"tokens":{"input":1,'
                     '"output":1},"cost":0}}\n')
        # each attempt produces a fresh artifact (newfile checks mtime >=
        # that attempt's run_start, so the fixture must refresh it too)
        import time as _t
        now = _t.time() + 5
        os.utime(f, (now, now))
        return 1, False  # nonzero exit, but artifact check below passes

    f = os.path.join(out, 'health-x.md')
    open(f, 'w').write('x')
    import time
    with mock.patch.object(runner, 'HERE', d), \
         mock.patch.object(runner, 'dispatch', fake_dispatch):
        st = {'tasks': {}}
        runner.run_tasks(man, prompts, st, spath, ['T01'], 'm', 60,
                         False, None, 1)
        assert len(calls) == 2, calls
        st2 = runner.load_state(spath)
        rec = st2['tasks']['T01']
        assert rec['attempt'] == 2 and rec['retryable'] is True, rec
    # max_retries=0: single attempt, no retry
    calls.clear()
    spath2 = os.path.join(d, 's2.json')
    with mock.patch.object(runner, 'HERE', d), \
         mock.patch.object(runner, 'dispatch', fake_dispatch):
        st = {'tasks': {}}
        runner.run_tasks(man, prompts, st, spath2, ['T01'], 'm', 60,
                         False, None, 0)
        assert len(calls) == 1, calls
    print('PASS retry-loop')


if __name__ == '__main__':
    test_markers()
    test_state_crashsafe()
    test_done_checks()
    test_gate()
    test_cost_parse()
    test_cost_incomplete()
    test_cost_partial_finish()
    test_failure_class()
    test_ledger_total()
    test_legacy_done_reruns()
    test_retry_loop()
    test_ledger_per_attempt_rows()
    test_ledger_rejects_unknown_outcome()
    test_dry_run_ignores_budget_halt()
    test_rerun_resets_attempts()
    test_retry_suppressed_over_budget()
    print('ALL PASS')
