#!/usr/bin/env python3
"""Opt-in live work -> same-executor closed-book probe in disposable repositories."""
import argparse
import json
import os
from pathlib import Path
import tempfile
import time
from unittest.mock import Mock, patch
import telos as b
from closed_book import validate_evidence


def job(sid, kind, message=None):
    # Keep real job reservation, checks, packet production and callbacks. Only
    # replace the daemon spawn so this disposable probe can await it directly.
    with patch.object(b.subprocess, 'Popen', return_value=Mock(pid=os.getpid())):
        jid = b.start_job(sid, kind, message)['job_id']
    b.background_job(sid, jid)
    state, contract = b.load_session(b.run_dir(sid))
    if state['phase'] == 'error':
        raise RuntimeError(state['last_error'])
    return state, contract


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required; calls consume configured API/Coding Plan quota')
    report = {'version': b.VERSION, 'passed': False, 'synthetic_repository_only': True, 'cases': []}
    cfg = b.config()
    started = time.time()
    try:
        with tempfile.TemporaryDirectory(prefix='telos-live-workflow-') as folder:
            root = Path(folder).resolve()
            b.STATE, b.CACHE, b.CONFIG = root / 'state', root / 'cache', root / 'config.json'
            b.write_json(b.CONFIG, {**cfg, 'job_timeout': 180, 'http_timeout': 120})
            repo = root / 'repo'; repo.mkdir()
            b.git(repo, 'init', '-q')
            (repo / 'calc.py').write_text('# Work memory token: BOOK_742159\ndef add(a, b):\n    raise NotImplementedError\n')
            b.git(repo, 'add', 'calc.py')
            b.git(repo, '-c', 'user.name=Workflow Probe', '-c', 'user.email=probe@localhost', 'commit', '-qm', 'fixture')
            questions = [
                {'id': 'implementation', 'prompt': 'How did you implement calc.add, and why does it handle zero and negative integers?',
                 'reference': 'A direct return of a + b implements Python integer addition.', 'points': 60},
                {'id': 'memory', 'prompt': 'Recall the exact work memory token from the original calc.py comment you inspected during work.',
                 'reference': 'The work memory token was BOOK_742159.', 'points': 40}]
            checks = [{'id': 'addition', 'argv': ['{python}', '-c',
                'from calc import add; assert add(2,3)==5; assert add(-9,4)==-5; assert add(0,0)==0; assert add(10**40,7)==10**40+7']}]
            task = ('Implement integer addition in calc.py. Inspect its original content and preserve the comment. '
                    'Modify only calc.py. Use no dependencies. Run a short Python check and reply briefly.')
            for profile in ('deepseek_official', 'ark_coding_plan'):
                sid = b.create_session(str(repo), task, questions, checks, executor_profile=profile)['session_id']
                print(profile + ': work phase (max)', flush=True)
                state, contract = job(sid, 'send', 'Implement the assigned change now.')
                assert state['phase'] == 'responded'
                thread_id = state['thread_id']
                report['cases'].append({'name': profile + '_work', 'backend': state['last_result']['backend'],
                                        'usage': state['last_result']['usage']})
                print(profile + ': same-thread closed-book examination (max)', flush=True)
                state, contract = job(sid, 'evaluate')
                assert state['phase'] == 'awaiting_review', 'Expected supervisor review; model must never self-accept'
                packet = b.review_packet(sid)['evidence']
                validate_evidence(b.bridge_services(), state, contract, packet)
                exam = packet['exam']
                assert exam['executor_thread_id'] == thread_id
                assert 'BOOK_742159' in next(a['answer'] for a in exam['answers'] if a['id'] == 'memory')
                assert all(c['status'] == 'passed' for c in packet['checks'])
                assert b.git(state['worktree'], 'rev-parse', 'HEAD') == packet['commit']
                assert not b.git(state['worktree'], 'status', '--porcelain')
                report['cases'].append({'name': profile + '_closed_book', 'backend': exam['backend'],
                    'same_thread': True, 'tool_call_count': exam['tool_call_count'], 'usage': exam['usage'],
                    'answers': exam['answers'], 'checks': packet['checks'], 'phase': state['phase'],
                    'transport_attempts': exam['attempts']})
            report['passed'] = True
    except Exception as error:
        report['error'] = str(error)[:3000]
    finally:
        report['seconds'] = round(time.time() - started, 2)
        b.write_json(args.report.resolve(), report)
    print(json.dumps({'passed': report['passed'], 'completed_cases': [c['name'] for c in report['cases']],
                      'report': str(args.report.resolve()), 'seconds': report['seconds']}), flush=True)
    raise SystemExit(not report['passed'])


if __name__ == '__main__': main()
