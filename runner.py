"""OSD autonomous task runner: executes catalog tasks via headless
`opencode run`, checkpoints every task to state.json, resumes after
any break (intended or crash). Usage:
  python runner.py status [--state FILE]
  python runner.py dry-run [--only T01,..]
  python runner.py run [--only ...] [--model ...] [--state FILE]
      [--budget USD] [--max-retries N]
  python runner.py resume [--model ...] [--state FILE]
  python runner.py reset [--only ...] [--state FILE]
Done-check types: newfile (glob newer than run start), minlines,
mtime (file newer than run start). Time gate: T12-style telemetry
span/entries. State writes are crash-safe (tmp + os.replace).
Cost ledger: ledger.jsonl accumulates per-attempt rows (task, attempt,
outcome, tokens, cost) parsed from --format json step_finish events.
Step_finish can be missing
(upstream opencode #26855: CLI may exit on idle before emitting it);
only a log whose LAST event is step_finish counts as complete, others
are flagged with a reconcile warning and their sums treated as lower
bounds — so the --budget halt fires LATE (permissive) on incomplete
rows: true spend may already exceed the cap. Stall
watchdog kills runs whose log stops growing (stall_after_sec) and marks
them stalled. Retry policy: timeouts/stalls and nonzero-exits with
passing checks are retryable (--max-retries N re-dispatches
in-process; stalled tasks included); other failures need human review.
Global --budget cap halts before the next dispatch once the ledger
total reaches it."""
import argparse
import glob
import json
import os
import subprocess
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
    idle before emitting the final step_finish event. Only a log whose
    LAST event is step_finish counts as complete=True; a log with
    earlier-but-not-final step_finish events is flagged
    partial-step-finish (sums are a lower bound), and a log with none at
    all is flagged no-step-finish. Both cases need session DB/export
    reconciliation rather than trusted totals."""
    tin = tout = 0
    cost = 0.0
    n_step_finish = 0
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
                    n_step_finish += 1
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
    # #26855 mode: final step_finish missing while earlier ones exist.
    # Only a log whose LAST event is step_finish counts as complete;
    # anything else (incl. partial sums) is flagged for reconciliation.
    complete = n_step_finish > 0 and last_event == 'step_finish'
    if complete:
        reason = 'ok'
    elif n_step_finish > 0:
        reason = ('partial-step-finish(%d seen, last=%s): final event '
                  'missing, sum is a lower bound' % (n_step_finish,
                                                     last_event))
    else:
        reason = 'no-step-finish(last=%s)' % last_event
    return {'tokens_in': tin, 'tokens_out': tout, 'cost': round(cost, 6),
            'complete': complete, 'reason': reason}


def ledger_append(ledger, tid, cost, attempt, outcome):
    # One row PER ATTEMPT (not per task): every dispatch costs money and
    # collapsing retries would hide spend. attempt + outcome are REQUIRED
    # (no defaults): 'unknown' outcomes are a schema violation, fail loud
    # at the call site instead of writing bad rows. outcome is 'done' or
    # 'attempt-failed' — see docs/budget.md.
    if outcome not in ('done', 'attempt-failed'):
        raise ValueError("ledger outcome must be 'done'/'attempt-failed', "
                         'got %r' % (outcome,))
    entry = {'task': tid, 'attempt': attempt, 'outcome': outcome,
             'finished_at': time.strftime('%Y-%m-%dT%H:%M:%S')}
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
    after doing the work); anything else — nonzero exit with failing
    checks, or exit 0 with failing checks (agent claimed success but
    produced nothing verifiable) — needs a human look."""
    if stalled or rc in (124, 125):
        return True, 'timeout/stall'
    if rc != 0 and all(checks):
        return True, 'nonzero-exit-checks-pass'
    if rc == 0:
        return False, 'zero-exit-checks-fail'
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
        rerun = False
        if st.get('status') == 'done':
            prev_start = st.get('run_start')
            if prev_start is None:
                # pre-upgrade state entry: fail-safe re-run once, then the
                # fresh entry carries run_start and skips normally.
                print('%s: RE-RUN (legacy done entry without run_start; '
                      'fail-safe, artifacts checked post-run)' % t['id'])
                rerun = True
            else:
                recheck = [check_done(c, prev_start) for c in t['done']]
                if all(recheck):
                    print('%s: skip (done %s, artifacts re-verified)' % (
                        t['id'], st.get('finished_at', '?')))
                    continue
                print('%s: RE-RUN (was done %s but artifacts missing: %s)' % (
                    t['id'], st.get('finished_at', '?'), recheck))
                rerun = True
        over_budget = budget is not None and spent >= budget
        if dry:
            # dry-run previews EVERYTHING (contract: zero side effects,
            # full visibility). A breached cap is advisory here, never a
            # halt — the real run will enforce it before dispatching.
            if over_budget:
                print('%s: (BUDGET WOULD-HALT: spent %.4f >= cap %.4f)' % (
                    t['id'], spent, budget))
        elif over_budget:
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
        # RE-RUNs (legacy entries, vanished artifacts) start a FRESH
        # attempt sequence: prior attempts proved nothing durable, and
        # inheriting the old count would silently shrink this run's retry
        # allowance to zero. History is preserved in prior_attempts.
        if rerun:
            attempt = 0
            prior = st.get('attempt', 0)
        else:
            attempt = st.get('attempt', 0)
            prior = 0
        outcome = None
        while True:
            attempt += 1
            run_start = time.time()
            entry = {
                'status': 'running',
                'started_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
                'run_start': run_start, 'attempt': attempt}
            if prior:
                entry['prior_attempts'] = prior
            state['tasks'][t['id']] = entry
            save_state(spath, state)
            rc, stalled = dispatch(t, prompt, model, timeout,
                                   os.path.join(HERE, 'logs'),
                                   manifest.get('stall_after_sec', 600))
            cost = parse_cost(os.path.join(HERE, 'logs', t['id'] + '.log'))
            checks = [check_done(c, run_start) for c in t['done']]
            done = (rc == 0 and all(checks))
            # Ledger row is written for EVERY attempt (spend is real even
            # when the attempt fails); the outcome field marks whether this
            # attempt finished the task. Written before the retry decision
            # so suppressed/retried attempts are all auditable.
            # Ledger attempt numbers are MONOTONIC per task across all runs
            # (prior + attempt): the ledger is append-only, so reusing
            # fresh-sequence numbers after a RE-RUN would create duplicate
            # attempt rows and break highest-attempt-wins ordering. State
            # attempt stays per-run-sequence (retry-allowance semantic).
            ledger_append(os.path.join(HERE, 'ledger.jsonl'), t['id'], cost,
                          prior + attempt,
                          'done' if done else 'attempt-failed')
            spent += cost.get('cost', 0.0)
            if not cost.get('complete', True):
                print('%s: WARNING ledger incomplete (%s); totals are a '
                      'lower bound, reconcile via session DB/export' % (
                          t['id'], cost.get('reason')))
            if done:
                outcome = ('done', {
                    'status': 'done',
                    'finished_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
                    'run_start': run_start, 'attempt': attempt})
                print('%s: DONE (exit 0, checks %s, attempt %d)' % (
                    t['id'], checks, attempt))
                break
            retryable, label = failure_class(
                rc, stalled or rc in (124, 125), checks)
            if retryable and attempt <= max_retries:
                if budget is not None and spent >= budget:
                    outcome = ('failed', {
                        'status': 'failed', 'exit': rc, 'checks': checks,
                        'run_start': run_start, 'attempt': attempt,
                        'retryable': True,
                        'reason': 'retry suppressed: spent %.4f >= cap %.4f '
                                  '[%s]' % (spent, budget, label)})
                    print('%s: RETRY SUPPRESSED (spent %.4f >= cap %.4f)' % (
                        t['id'], spent, budget))
                    break
                print('%s: RETRY %d/%d (exit %s checks %s [%s])' % (
                    t['id'], attempt, max_retries, rc, checks, label))
                continue
            outcome = ('stalled' if stalled else 'failed', {
                'status': 'stalled' if stalled else 'failed', 'exit': rc,
                'checks': checks, 'run_start': run_start, 'attempt': attempt,
                'retryable': retryable,
                'reason': ('killed after stall/timeout' if stalled else
                           'exit %s checks %s [%s]' % (rc, checks, label))})
            print('%s: %s (exit %s checks %s%s, attempt %d)' % (
                t['id'], outcome[0].upper(), rc, checks,
                '' if stalled else ' [%s]' % label, attempt))
            break
        if prior:
            outcome[1]['prior_attempts'] = prior
        state['tasks'][t['id']] = outcome[1]
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
         'nonzero-exit with passing checks) up to N re-dispatches '
         'in-process; 0 (default) records retryable and stops')
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
