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
    assert c == {'tokens_in': 81218, 'tokens_out': 20, 'cost': 0.002}, c
    assert runner.parse_cost(os.path.join(d, 'missing.log')) == \
        {'tokens_in': 0, 'tokens_out': 0, 'cost': 0.0}
    print('PASS cost')


if __name__ == '__main__':
    test_markers()
    test_state_crashsafe()
    test_done_checks()
    test_gate()
    test_cost_parse()
    print('ALL PASS')
