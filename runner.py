"""OSD autonomous task runner: executes catalog tasks via headless
`opencode run`, checkpoints every task to state.json, resumes after
any break (intended or crash). Usage:
  python runner.py status [--state FILE]
  python runner.py dry-run [--only T01,..]
  python runner.py run [--only ...] [--model ...] [--state FILE]
  python runner.py resume [--model ...] [--state FILE]
  python runner.py reset [--only ...] [--state FILE]
Done-check types: newfile (glob newer than run start), minlines,
mtime (file newer than run start). Time gate: T12-style telemetry
span/entries. State writes are crash-safe (tmp + os.replace).
Cost ledger: ledger.jsonl accumulates per-task tokens/cost parsed
from --format json step_finish events. Step_finish can be missing
(upstream opencode #26855: CLI may exit on idle before emitting it);
such entries are flagged complete=false with a reconcile warning, never
counted as authoritative. Stall watchdog kills runs
whose log stops growing (stall_after_sec) and marks them stalled.
Retry policy: timeouts/stalls and nonzero-exits with passing checks are
retryable (see --max-retries); other failures need human review.
Global --budget cap halts before the next dispatch once the ledger
total reaches it."""
import argparse
import fnmatch
import glob
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def load_tasks():
    with open(os.path.join(HERE, 'tasks.json'), encoding='utf-8') as f:
        return json.load(f)


def load_prompts():
    text = open(os.path.join(HERE, 'PROMPTS.md'), encoding='utf-8').read()
    out, cur = {}, None
    for line in text.splitlines():
        if line.startswith('<!-- === ') and line.endswith(' === -->'):
            cur = line[9:-8]
            out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    return {k: '\n'.join(v).strip() for k, v in out.items()}


def load_state(path):
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    return {'tasks': {}}


def save_state(path, state):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def check_done(check, run_start):
    t = check['type']
    if t == 'newfile':
        if not os.path.isdir(check['dir']):
            return False
        for f in glob.glob(os.path.join(check['dir'], check['glob'])):
            if os.path.getmtime(f) >= run_start:
                return True
        return False
    if t == 'minlines':
        if not os.path.exists(check['path']):
            return False
        with open(check['path'], encoding='utf-8', errors='ignore') as f:
            return sum(1 for _ in f) >= check['min_lines']
    if t == 'mtime':
        return (os.path.exists(check['path'])
                and os.path.getmtime(check['path']) >= run_start)
    return False


def gate_open(task):
    g = task.get('gate')
    if not g:
        return True, ''
    p = g['telemetry']
    if not os.path.exists(p):
        return False, 'telemetry file absent'
    with open(p, encoding='utf-8', errors='ignore') as f:
        lines = [ln for ln in f if ln.strip()]
    if len(lines) < g.get('entries_min', 10):
        return False, 'only %d entries (need %d)' % (len(lines), g.get('entries_min', 10))
    try:
        ts = [json.loads(ln).get('timestamp', '') for ln in lines]
        ts = sorted(t for t in ts if t)
        from datetime import datetime, timezone
        lo = datetime.fromisoformat(ts[0].replace('Z', '+00:00'))
        hi = datetime.fromisoformat(ts[-1].replace('Z', '+00:00'))
        span = (hi - lo).total_seconds() / 86400.0
    except Exception as e:
        return False, 'timestamp parse failed: %s' % e
    if span < g.get('span_days_min', 7):
        return False, 'span %.1fd < %dd' % (span, g.get('span_days_min', 7))
    return True, ''


def parse_cost(log):
    """Extract summed input/output tokens + cost from --format json log.

    Returns dict with tokens_in/tokens_out/cost plus completeness flags.
    Known upstream gap (opencode #26855): `run --format json` can exit on
    idle before emitting the final step_finish event, so a log whose last
    event is text/step_start with no step_finish is flagged
    complete=False rather than silently under-counting."""
    tin = tout = 0
    cost = 0.0
    saw_step_finish = False
    last_event = None
    try:
        with open(log, encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.strip()
                if not line.startswith('{'):
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                etype = d.get('type')
                if etype in ('step_start', 'step_finish', 'text',
                             'tool_use', 'reasoning', 'error'):
                    last_event = etype
                part = d.get('part', {})
                if etype == 'step_finish' and isinstance(part, dict):
                    saw_step_finish = True
                    tk = part.get('tokens', {}) or {}
                    tin += int(tk.get('input', 0) or 0)
                    tout += int(tk.get('output', 0) or 0)
                    try:
                        cost += float(part.get('cost', 0) or 0)
                    except Exception:
                        pass
    except FileNotFoundError:
        return {'tokens_in': 0, 'tokens_out': 0, 'cost': 0.0,
                'complete': False, 'reason': 'log-missing'}
    complete = saw_step_finish
    reason = 'ok' if complete else 'no-step-finish(last=%s)' % last_event
    return {'tokens_in': tin, 'tokens_out': tout, 'cost': round(cost, 6),
            'complete': complete, 'reason': reason}


def ledger_append(ledger, tid, cost):
    entry = {'task': tid, 'finished_at': time.strftime('%Y-%m-%dT%H:%M:%S')}
    entry.update(cost)
    if not cost.get('complete', True):
        entry['complete'] = False
    with open(ledger, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry) + '\n')


def ledger_total(ledger):
    total = 0.0
    try:
        with open(ledger, encoding='utf-8', errors='ignore') as f:
            for line in f:
                try:
                    total += float(json.loads(line).get('cost', 0) or 0)
                except Exception:
                    continue
    except FileNotFoundError:
        pass
    return total


def failure_class(rc, stalled, checks):
    """Classify a non-done outcome for retry policy.

    Returns (retryable: bool, label: str). Timeouts/stalls are transient;
    nonzero exits with passing artifact checks are transient (agent died
    after doing the work); nonzero exits with failing checks need a human
    look before burning more budget."""
    if stalled or rc in (124, 125):
        return True, 'timeout/stall'
    if rc != 0 and all(checks):
        return True, 'nonzero-exit-checks-pass'
    return False, 'nonzero-exit-checks-fail'


def dispatch(task, prompt, model, timeout, logdir, stall_after):
    os.makedirs(logdir, exist_ok=True)
    log = os.path.join(logdir, task['id'] + '.log')
    cmd = ['opencode', 'run', '--agent', task['agent'], '-m', model,
           '--title', task['id'] + ' ' + task['title'],
           '--format', 'json', prompt]
    with open(log, 'w', encoding='utf-8') as lf:
        lf.write('CMD: opencode run --agent %s -m %s\n' % (task['agent'], model))
        lf.flush()
        try:
            p = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT)
            deadline = time.time() + timeout
            last_growth = time.time()
            last_size = 0
            while True:
                rc = p.poll()
                if rc is not None:
                    return rc, False
                now = time.time()
                try:
                    size = os.path.getsize(log)
                except OSError:
                    size = 0
                if size != last_size:
                    last_size, last_growth = size, now
                if now - last_growth > stall_after:
                    p.kill()
                    lf.write('\nSTALLED: no log growth for %ds; killed\n' % stall_after)
                    return 125, True
                if now > deadline:
                    p.kill()
                    lf.write('\nTIMEOUT after %ds\n' % timeout)
                    return 124, True
                time.sleep(15)
        except FileNotFoundError:
            lf.write('\nERROR: opencode executable not found\n')
            return 127, False


def cmd_status(manifest, state, only):
    for t in manifest['tasks']:
        if only and t['id'] not in only:
            continue
        st = state['tasks'].get(t['id'], {})
        ok, why = gate_open(t)
        gate = 'open' if ok else ('WAITING (%s)' % why)
        print('%-4s %-28s status=%-9s gate=%s' % (
            t['id'], t['title'][:28], st.get('status', 'pending'), gate))


def run_tasks(manifest, prompts, state, spath, only, model, timeout, dry,
                budget=None, max_retries=0):
    spent = ledger_total(os.path.join(HERE, 'ledger.jsonl'))
    for t in manifest['tasks']:
        if only and t['id'] not in only:
            continue
        st = state['tasks'].get(t['id'], {})
        if st.get('status') == 'done':
            prev_start = st.get('run_start', 0)
            recheck = [check_done(c, prev_start) for c in t['done']]
            if prev_start and all(recheck):
                print('%s: skip (done %s, artifacts re-verified)' % (
                    t['id'], st.get('finished_at', '?')))
                continue
            print('%s: RE-RUN (was done %s but artifacts missing: %s)' % (
                t['id'], st.get('finished_at', '?'), recheck))
        if budget is not None and spent >= budget:
            print('BUDGET HALT: spent %.4f >= cap %.4f; stopping' % (
                spent, budget))
            break
        ok, why = gate_open(t)
        if not ok:
            print('%s: WAITING (%s)' % (t['id'], why))
            if not dry:
                state['tasks'][t['id']] = {'status': 'waiting', 'reason': why}
                save_state(spath, state)
            continue
        prompt = prompts.get(t['section'])
        if not prompt:
            print('%s: ERROR no prompt section %s' % (t['id'], t['section']))
            if not dry:
                state['tasks'][t['id']] = {'status': 'failed', 'reason': 'no-prompt'}
                save_state(spath, state)
            continue
        if dry:
            print('%s: dry-run ok (agent=%s, prompt=%d chars)' % (
                t['id'], t['agent'], len(prompt)))
            continue
        print('%s: dispatching to %s ...' % (t['id'], t['agent']), flush=True)
        run_start = time.time()
        state['tasks'][t['id']] = {'status': 'running',
                                   'started_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
                                   'run_start': run_start,
                                   'attempt': st.get('attempt', 0) + 1}
        save_state(spath, state)
        rc, stalled = dispatch(t, prompt, model, timeout,
                               os.path.join(HERE, 'logs'),
                               manifest.get('stall_after_sec', 600))
        cost = parse_cost(os.path.join(HERE, 'logs', t['id'] + '.log'))
        ledger_append(os.path.join(HERE, 'ledger.jsonl'), t['id'], cost)
        spent += cost.get('cost', 0.0)
        if not cost.get('complete', True):
            print('%s: WARNING ledger incomplete (%s); reconcile via '
                  'session DB/export, do not trust totals' % (
                      t['id'], cost.get('reason')))
        checks = [check_done(c, run_start) for c in t['done']]
        if rc == 0 and all(checks):
            state['tasks'][t['id']] = {'status': 'done',
                                       'finished_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
                                       'run_start': run_start}
            print('%s: DONE (exit 0, checks %s)' % (t['id'], checks))
        elif stalled:
            state['tasks'][t['id']] = {'status': 'stalled', 'exit': rc,
                                       'checks': checks,
                                       'run_start': run_start,
                                       'reason': 'killed after stall/timeout'}
            print('%s: STALLED (exit %s checks %s)' % (t['id'], rc, checks))
        else:
            retryable, label = failure_class(rc, stalled, checks)
            attempts = state['tasks'].get(t['id'], {}).get('attempt', 1)
            will_retry = retryable and attempts <= max_retries
            state['tasks'][t['id']] = {'status': 'failed', 'exit': rc,
                                       'checks': checks,
                                       'run_start': run_start,
                                       'retryable': retryable,
                                       'reason': 'exit %s checks %s [%s]%s' % (
                                           rc, checks, label,
                                           ' retrying' if will_retry else '')}
            print('%s: FAILED (exit %s checks %s [%s]%s)' % (
                t['id'], rc, checks, label,
                ' retrying' if will_retry else ''))
        save_state(spath, state)
    print('run complete; resume anytime with: python runner.py resume')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['status', 'dry-run', 'run', 'resume', 'reset'])
    ap.add_argument('--only', default='')
    ap.add_argument('--model', default='')
    ap.add_argument('--state', default=os.path.join(HERE, 'state.json'))
    ap.add_argument('--timeout', type=int, default=1800)
    ap.add_argument('--budget', type=float, default=None,
                    help='global cost cap USD; halt before next dispatch once '
                         'ledger total reaches it')
    ap.add_argument('--max-retries', type=int, default=0,
                    help='auto-retry transient failures (stall/timeout, '
                         'nonzero-exit with passing checks) this many times')
    a = ap.parse_args()
    manifest = load_tasks()
    prompts = load_prompts()
    state = load_state(a.state)
    only = [x for x in a.only.split(',') if x] or None
    model = a.model or manifest.get('default_model', '')
    if a.cmd == 'status':
        cmd_status(manifest, state, only)
    elif a.cmd == 'dry-run':
        run_tasks(manifest, prompts, state, a.state, only, model, a.timeout,
                  True, a.budget, a.max_retries)
    elif a.cmd in ('run', 'resume'):
        run_tasks(manifest, prompts, state, a.state, only, model, a.timeout,
                  False, a.budget, a.max_retries)
    elif a.cmd == 'reset':
        if only:
            for i in only:
                state['tasks'].pop(i, None)
        else:
            state = {'tasks': {}}
        save_state(a.state, state)
        print('reset done')


if __name__ == '__main__':
    main()
