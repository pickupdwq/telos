#!/usr/bin/env python3
"""Controlled offline comparison; no live-model speedup is inferred."""
import argparse
import json
from pathlib import Path
import threading
import time
from unittest.mock import patch

from test_telos import AutoChecksTests, b
import check_runtime


def scenario(automatic, delay):
    fixture = AutoChecksTests()
    fixture.setUp()
    started = time.time()
    try:
        program = ('import time; print("fixture_check_started", time.time()); time.sleep(0.25); '
                   'from calc import add; assert add(2,3)==5; assert add(-2,5)==3')
        sid = fixture.create(auto_run_checks=automatic, checks=[{'id':'same-addition-check',
            'argv':['{python}','-c',program], 'freshness':{'mode':'snapshot'}}])
        root = b.run_dir(sid)
        invoked = threading.Event()
        actual_observe = check_runtime.observe
        def observe(operation, *args, **kwargs):
            result = actual_observe(operation, *args, **kwargs)
            if operation == 'mark' and args[0] == 'checks_started': invoked.set()
            return result
        with patch.object(b, 'executable_checks', wraps=b.executable_checks) as checks, patch.object(check_runtime, 'observe', side_effect=observe):
            fixture.deliver(sid)
            failures = []
            worker = None
            if automatic:
                def run():
                    try: fixture.complete_checks(sid)
                    except BaseException as error: failures.append(str(error))
                worker = threading.Thread(target=run)
                worker.start()
                if not invoked.wait(10): raise AssertionError('Check stage failed to start')
            # Explicit controlled supervisor delay: this is not model latency.
            time.sleep(delay)
            supervisor_action = time.time()
            if worker:
                worker.join(10)
                if worker.is_alive() or failures: raise AssertionError(failures or 'Worker did not complete')
            completed = fixture.evaluate(sid)
            packet = b.review_packet(sid)
            verdict = b.grade(sid, packet['packet_id'], fixture.scores(), [])
            assert verdict['accepted'], completed
            assert checks.call_count == 1, checks.call_count
        state = fixture.state(sid)
        timeline = b.status(sid)['timing']
        check_start = next(x['at'] for x in timeline['stages']['milestones'] if x['name'] == 'checks_started')
        assert (check_start < supervisor_action) == automatic
        return {'automatic':automatic,'accepted':verdict['accepted'], 'check_executions':checks.call_count,
                'exam_rounds':state['rounds'],'check_started_before_supervisor_action':check_start < supervisor_action,
                'supervisor_next_action_at':supervisor_action,'requirement_fixture_started_at':started,
                'finished_at':time.time(), 'timeline':timeline,
                'packet':packet['evidence'],'usage':b.status(sid)['usage']}
    finally:
        fixture.tearDown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    args = parser.parse_args()
    delay = .35
    old, new = scenario(False, delay), scenario(True, delay)
    assert old['packet']['checks'][0]['id'] == new['packet']['checks'][0]['id']
    assert old['exam_rounds'] == new['exam_rounds'] == 1
    report = {'passed':True,'fixture':'same offline task/check, real Git and macOS sandbox',
              'controlled_supervisor_delay_seconds':delay, 'live_models_verified':False,
              'boundary':'Controlled scheduling overlap only; no real model speedup ratio.', 'original':old,'automatic':new}
    args.report.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps({'passed':True,'check_executions':[old['check_executions'],new['check_executions']],
                      'checks_before_supervisor':[old['check_started_before_supervisor_action'],new['check_started_before_supervisor_action']]}))


if __name__ == '__main__': main()
