"""DBOS workflow + steps. Imported ONLY when DBOS is installed, from
runner_dbos.define_app() after DBOS.launch(). Never import at module top
level, so `python -m pytest tests/` and plain imports work without dbos.
"""
import asyncio
import os
import subprocess
import time

import runner


def define_app(DBOS, mod):
    """Register workflow/steps on the given DBOS object. Returns names."""

    @DBOS.step(retries_allowed=False)
    def step_gate(task):
        return runner.gate_open(task)

    @DBOS.step(retries_allowed=False)
    def step_acquire(budget_url, scopes, estimate):
        store = mod.BudgetStore(budget_url)
        verdicts = {s: store.acquire(s, estimate) for s in scopes}
        return verdicts

    @DBOS.step(timeout_seconds=1800, retries_allowed=False)
    async def step_dispatch(task, prompt, model, timeout, logdir):
        os.makedirs(logdir, exist_ok=True)
        log = os.path.join(logdir, task['id'] + '.log')
        cmd = ['opencode', 'run', '--agent', task['agent'], '-m', model,
               '--title', task['id'] + ' ' + task['title'],
               '--format', 'json', prompt]
        with open(log, 'w', encoding='utf-8') as lf:
            lf.write('CMD: opencode run --agent %s -m %s\n'
                     % (task['agent'], model))
            lf.flush()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=lf, stderr=subprocess.STDOUT)
            except FileNotFoundError:
                lf.write('\nERROR: opencode executable not found\n')
                return 127
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
                return proc.returncode
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                lf.write('\nTIMEOUT after %ds\n' % timeout)
                return 124

    @DBOS.step(retries_allowed=False)
    def step_verify(task, run_start):
        return [runner.check_done(c, run_start) for c in task['done']]

    @DBOS.step(retries_allowed=False)
    def step_ledger(task_id, log):
        cost = runner.parse_cost(log)
        runner.ledger_append(
            os.path.join(runner.HERE, 'ledger.jsonl'), task_id, cost)
        return cost

    @DBOS.step(retries_allowed=False)
    def step_settle(budget_url, scopes, estimate, actual):
        store = mod.BudgetStore(budget_url)
        for s in scopes:
            store.settle(s, estimate, actual)

    @DBOS.step(retries_allowed=False)
    def step_release(budget_url, scopes, estimate):
        store = mod.BudgetStore(budget_url)
        for s in scopes:
            store.settle(s, estimate, 0.0)

    @DBOS.step(retries_allowed=True, max_attempts=3)
    def step_notify_runbook(task_id, reason):
        # delivery to pager/runbook goes here; retryable by design
        return {'task': task_id, 'notified': True, 'reason': reason}

    @DBOS.workflow()
    async def task_workflow(task, prompt, opts):
        run_start = time.time()
        ok, why = step_gate(task)
        if not ok:
            return {'status': 'waiting', 'reason': why}
        estimate = float(opts.get('estimate_usd', 1.0))
        scopes = opts.get('scopes') or ['run:default',
                                         'task:%s' % task['id']]
        verdicts = step_acquire(opts['budget_url'], scopes, estimate)
        denied = [s for s, v in verdicts.items() if not v]
        if denied:
            step_notify_runbook(task['id'],
                                'budget denied: %s' % denied)
            return {'status': 'denied', 'scopes': denied}
        try:
            rc = await step_dispatch(task, prompt, opts['model'],
                                     opts['timeout'],
                                     os.path.join(runner.HERE, 'logs'))
        except Exception as e:  # timeouts surface here, not as rc
            step_release(opts['budget_url'], scopes, estimate)
            return {'status': 'failed', 'error': repr(e)}
        checks = step_verify(task, run_start)
        cost = step_ledger(task['id'],
                           os.path.join(runner.HERE, 'logs',
                                        task['id'] + '.log'))
        step_settle(opts['budget_url'], scopes, estimate,
                    float(cost.get('cost', 0.0)))
        if rc == 0 and all(checks):
            final = {'status': 'done', 'checks': checks, 'cost': cost}
        else:
            retryable, label = runner.failure_class(rc, rc in (124, 125),
                                                    checks)
            final = {'status': 'failed' if not retryable else 'retryable',
                     'exit': rc, 'checks': checks, 'label': label}
            step_notify_runbook(task['id'], 'non-done: %s' % (final,))
        if opts.get('needs_approval') and final.get('status') == 'done':
            DBOS.send('approver', {'task': task['id'], 'result': final})
            try:
                msg = await asyncio.wait_for(
                    DBOS.recv(timeout=float(opts.get('approval_timeout',
                                                     7 * 86400))),
                    timeout=float(opts.get('approval_timeout', 7 * 86400)))
                approved = mod.parse_approval(msg)
            except asyncio.TimeoutError:
                approved = False
            final['approved'] = approved
            if not approved:
                final['status'] = 'awaiting-approval-failed-closed'
        return final

    return {'task_workflow': task_workflow}
