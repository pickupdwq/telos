"""Synthetic-only fixed path correction and bounded round recovery regressions."""
import copy
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_path_correction as correction
import test_audit_check_correction as fixtures
import test_projects
import test_telos
from test_callbacks import OWNER, OTHER
from test_telos import b


@contextlib.contextmanager
def no_background_job():
    real = subprocess.Popen
    def launch(argv, **kwargs):
        if '_job' in argv:
            raise AssertionError('Unexpected background job launch')
        return real(argv, **kwargs)
    with patch.object(b.subprocess, 'Popen', side_effect=launch):
        yield


def program(python='/usr/bin/python3', runner=False):
    command = ("[python,runner,str(root),'pytest','tests/test_device_import_preview.py','tests/other.py','-q']" if runner else
               "[python,'-m','pytest','tests/test_device_import_preview.py','-q']")
    return ("import pathlib,subprocess\n# 中文定位\nroot=pathlib.Path.cwd()\n"
            f"python={python!r}\nrunner={correction.RUNNER!r}\n"
            f"result=subprocess.run({command},cwd=" + ("root" if runner else "root/'ai-device-risk'") +
            ",capture_output=True,text=True)\nprint(result.stdout)\nprint(result.stderr)\nresult.check_returncode()\n")


def contract(code):
    return {'task_type': 'implementation', 'checks': [{'id': 'backend', 'argv': ['{python}', '-c', code]}],
            'max_rounds': 5, 'threshold': 90, 'questions': [{'reference': 'private synthetic reference'}]}


class SyntaxTests(unittest.TestCase):
    def test_fixed_runner_and_standard_invocation_replace_one_span_only(self):
        for runner in (False, True):
            original = contract(program(runner=runner))
            fixed, change = correction.corrected_contract(original)
            self.assertEqual(fixed['checks'][0]['argv'][2], program(runner=runner).replace(correction.OLD, correction.NEW))
            self.assertEqual(change['invocation'], 'fixed_runner' if runner else 'python_pytest')
            copied = copy.deepcopy(fixed); copied['checks'] = original['checks']
            self.assertEqual(copied, original)
            start, end = change['span']
            self.assertEqual(change['before_program'].encode()[start:end], repr(correction.OLD).encode())

    def test_unsupported_and_ambiguous_programs_rejected(self):
        code = program(runner=True)
        variants = [code.replace(correction.RUNNER, '/tmp/other.py'),
                    code.replace("'pytest'", "'different'"),
                    code.replace("str(root)", "str(root/'elsewhere')"),
                    code.replace("'tests/other.py'", repr(correction.OLD)),
                    code.replace("'tests/other.py'", 'extra'),
                    code.replace("root=pathlib.Path.cwd()", "root=pathlib.Path('/tmp')"),
                    code.replace("result=subprocess.run", "python='/tmp/evil/python'\nresult=subprocess.run"),
                    code.replace("result=subprocess.run", "subprocess=object()\nresult=subprocess.run"),
                    code.replace("result=subprocess.run", "subprocess.run=print\nresult=subprocess.run"),
                    code.replace("result=subprocess.run", "pathlib.Path.cwd=print\nresult=subprocess.run"),
                    code.replace("result=subprocess.run", "setattr(subprocess,'run',print)\nresult=subprocess.run"),
                    code.replace("result=subprocess.run", "exec('python=1')\nresult=subprocess.run"),
                    code.replace("result=subprocess.run", "s=subprocess\ns.run=print\nresult=subprocess.run"),
                    code.replace("result=subprocess.run", "getattr(subprocess,'run')\nresult=subprocess.run"),
                    code.replace("result=subprocess.run", "from builtins import exec as f\nf('x=1')\nresult=subprocess.run"),
                    code.replace("result=subprocess.run", "import builtins as b\nb.exec('x=1')\nresult=subprocess.run"),
                    code.replace("result=subprocess.run", "__builtins__.__dict__['exec']('subprocess.run=print')\nresult=subprocess.run"),
                    code.replace('root=pathlib.Path.cwd()', "import os\nos.chdir('/tmp')\nroot=pathlib.Path.cwd()"),
                    code.replace('root=pathlib.Path.cwd()', "from os import chdir as move\nmove('/tmp')\nroot=pathlib.Path.cwd()"),
                    code.replace('root=pathlib.Path.cwd()', "import os\nmove=os.chdir\nmove('/tmp')\nroot=pathlib.Path.cwd()"),
                    code.replace("result=subprocess.run", "def f(python): return python\nresult=subprocess.run"),
                    code.replace('capture_output=True', 'shell=True,capture_output=True'),
                    code.replace('capture_output=True', "executable='/tmp/other',capture_output=True"),
                    code.replace('capture_output=True', 'env={},capture_output=True'),
                    code.replace('capture_output=True', 'preexec_fn=print,capture_output=True'),
                    code.replace("'tests/test_device_import_preview.py'", "'--ignore','tests/test_device_import_preview.py'"),
                    code.replace("'tests/test_device_import_preview.py'", "'-k','tests/test_device_import_preview.py'"),
                    code.replace("'tests/test_device_import_preview.py'", "'--rootdir','tests/test_device_import_preview.py'"),
                    code.replace("'tests/test_device_import_preview.py'", "'-q','tests/test_device_import_preview.py'"),
                    code.replace(repr(correction.OLD), "'tests/' 'test_device_import_preview.py'"),
                    code.replace(correction.OLD, correction.NEW),
                    program().replace("cwd=root/'ai-device-risk'", 'cwd=root')]
        for variant in variants:
            with self.subTest(code=variant), self.assertRaises(ValueError):
                correction.corrected_contract(contract(variant))
        original = contract(code); original['checks'] *= 2
        with self.assertRaises(ValueError):
            correction.corrected_contract(original)
        original = contract(code); original['task_type'] = 'audit'
        with self.assertRaises(ValueError):
            correction.corrected_contract(original)


class PathCorrectionTests(unittest.TestCase):
    tearDown = test_telos.BridgeTests.tearDown
    data = fixtures.CorrectionTests.data
    evaluate = fixtures.CorrectionTests.evaluate
    save = fixtures.CorrectionTests.save
    scores = test_telos.BridgeTests.scores

    def setUp(self):
        clean = {k:v for k,v in os.environ.items() if k in
                 ('PATH','LANG','LC_ALL','TMPDIR','HOME','TELOS_TEST_PYTEST_PYTHON')}
        sanitized = patch.dict(os.environ, clean, clear=True)
        sanitized.start()
        try:
            fixtures.CorrectionTests.setUp(self)
        except Exception:
            sanitized.stop()
            raise
        self.patches.insert(0, sanitized)
        minimal = b.minimal_env
        clean_checks = patch.object(b, 'minimal_env', side_effect=lambda: {
            **minimal(), 'PYTEST_DISABLE_PLUGIN_AUTOLOAD':'1', 'PYTEST_ADDOPTS':'-p no:cacheprovider'})
        clean_checks.start(); self.patches.append(clean_checks)
        app = self.repo / correction.APP
        (app / 'tests').mkdir(parents=True)
        (app / correction.NEW).write_text('from app_logic import add\ndef test_add():\n assert add(2,3)==5\n')
        (app / 'app_logic.py').write_text('def add(a,b): return a+b\n')
        b.git(self.repo, 'add', '.'); b.git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-qm', 'test fixture')

    def create(self, max_rounds=5, code=None, **kwargs):
        args = dict(repo=str(self.repo), task='Repair addition', questions=self.q, max_rounds=max_rounds,
                    checks=contract(code or program())['checks'], notify=True, owner_thread_id=OWNER)
        args.update(kwargs)
        sid = b.create_session(**args)['session_id']
        state, _ = self.data(sid)
        state.update(phase='responded', thread_id=OTHER, job_id='a' * 32, job_kind='send', turns=1,
                     last_result={'summary': 'Completed fixture work'})
        b.record_event(state); self.save(sid, state)
        self.assertTrue(b.receive_event(sid, state['last_event_id'])['proceed'])
        return sid

    def args(self, sid):
        state, _ = self.data(sid)
        return dict(session_id=sid, request_id='path-correction-v1', reason='Correct the known test filename typo',
                    user_authorization='Synthetic user authorized fixed path and at most five evidenced recovery rounds',
                    event_id=state['last_event_id'])

    def call(self, args, caller=OWNER):
        return b.call_tool('telos_correct_test_path', args, {'threadId': caller})

    def apply(self, sid, args=None):
        args = args or self.args(sid)
        preview = self.call(args)
        return self.call({**args, 'apply': True, 'proposal_hash': preview['proposal_hash']})

    def attempts(self, sid, count, *, bad=None):
        state, original = self.data(sid)
        root = b.run_dir(sid)
        for number in range(1, count + 1):
            packet = {'session_id': sid, 'round': number, 'commit': state['base_commit'],
                      'contract_hash': state['contract_hash'], 'checks': [
                          {'id': 'backend', 'status': 'failed', 'returncode': 1,
                           'stdout': 'no tests ran in 0.01s\n',
                           'stderr': 'ERROR: file or directory not found: ' + correction.OLD +
                                     '\nsubprocess.CalledProcessError: returned non-zero exit status 4.\n'}],
                      'exam': {'skipped': 'executable_checks_failed'}}
            if bad: bad(packet)
            b.write_json(root / f'attempt-{number}.json', packet)
        state.update(rounds=count, phase='not_accepted', packet_id=b.digest(packet),
                     last_result={'exam_received': False}, submitted_commit=state['base_commit'])
        self.save(sid, state)

    def test_preview_is_read_only_apply_is_idempotent_and_contract_bytes_unchanged(self):
        sid = self.create(); self.attempts(sid, 3)
        root = b.run_dir(sid); args = self.args(sid)
        before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
        stamp = (root / 'state.json').stat().st_mtime_ns
        preview = self.call(args)
        self.assertTrue(preview['preview'])
        self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()})
        self.assertEqual((root / 'state.json').stat().st_mtime_ns, stamp)
        with self.assertRaises(b.GateError): self.call({**args, 'apply': True, 'proposal_hash': '0' * 64})
        result = self.call({**args, 'apply': True, 'proposal_hash': preview['proposal_hash']})
        self.assertFalse(result['accepted'])
        state, fixed = self.data(sid)
        self.assertEqual(state['rounds'], 3); self.assertEqual(fixed['max_rounds'], 5)
        self.assertEqual(result['correction']['recovery']['granted'], 3)
        self.assertNotIn('packet_id', state)
        for name, raw in before.items():
            if name != 'state.json': self.assertEqual((root/name).read_bytes(), raw, name)
        after = (root/'state.json').read_bytes()
        self.assertTrue(self.call({**args, 'apply': True, 'proposal_hash': preview['proposal_hash']})['duplicate'])
        self.assertEqual((root/'state.json').read_bytes(), after)
        with self.assertRaises(b.GateError): self.call({**args, 'reason': 'different'})
        with self.assertRaises(b.GateError): b.review_packet(sid)
        self.assertEqual(b.status(sid, 'summary')['test_path_correction']['effective_contract_hash'], b.digest(fixed))

    def test_recovery_is_bounded_and_only_consumed_above_original_ceiling(self):
        sid = self.create(max_rounds=10); self.attempts(sid, 7)
        self.apply(sid)
        state, original = self.data(sid)
        self.assertEqual(correction.round_limit(state, original), 15)
        for rounds, used, ordinary in ((7,0,3), (10,0,0), (11,1,0), (15,5,0)):
            state['rounds'] = rounds; self.save(sid, state)
            current, _ = self.data(sid)
            status = correction.public(current)['recovery']
            self.assertEqual((status['granted'], status['consumed'], status['remaining'], status['original_remaining']),
                             (5, used, 5-used, ordinary))
        with no_background_job(), self.assertRaisesRegex(b.GateError, 'Maximum exam rounds'):
            b.start_job(sid, 'evaluate', event_id=state['last_event_id'])
        state['rounds'] = 6; self.save(sid, state)
        with self.assertRaises(b.GateError): self.data(sid)

    def test_only_proven_missing_path_attempts_grant_recovery(self):
        mutations = [lambda p:p.update(contract_hash='0'*64), lambda p:p.update(session_id='other'),
                     lambda p:p.update(round=2), lambda p:p.update(round=True),
                     lambda p:p['checks'][0].update(returncode=True),
                     lambda p:p['exam'].update(skipped='awaiting_supervisor_evaluate'),
                     lambda p:p['exam'].update(answers=[{'id':'q1','answer':'unexpected exam'}]),
                     lambda p:p['checks'][0].update(status='passed'), lambda p:p['checks'][0].update(returncode=2),
                     lambda p:p['checks'][0].update(stdout='1 failed in 0.1s'),
                     lambda p:p['checks'][0].update(stderr='AssertionError: product failed'),
                     lambda p:p['checks'][0].update(stderr='ERROR: file or directory not found: tests/another.py\nexit status 4'),
                     lambda p:p['checks'][0].update(stderr='ERROR: file or directory not found: '+correction.OLD)]
        for mutate in mutations:
            sid = self.create(); self.attempts(sid, 1, bad=mutate)
            with self.subTest(mutate=mutate):
                self.assertEqual(self.call(self.args(sid))['correction']['recovery']['granted'], 0)

    def test_git_proof_rejects_existing_missing_changed_and_symlink_targets(self):
        for variant in ('old_exists', 'new_missing', 'new_changed', 'new_link', 'parent_link'):
            sid = self.create(); state, _ = self.data(sid)
            app = Path(state['worktree']) / correction.APP
            if variant == 'old_exists': (app/correction.OLD).write_text('def test_other(): pass\n')
            elif variant == 'new_missing': (app/correction.NEW).unlink()
            elif variant == 'new_changed': (app/correction.NEW).write_text('def test_weak(): pass\n')
            elif variant == 'new_link':
                (app/correction.NEW).unlink(); (app/correction.NEW).symlink_to('../app_logic.py')
            else:
                (app/correction.NEW).unlink(); (app/'tests').rmdir(); (app/'tests').symlink_to('/tmp')
            b.git(state['worktree'], 'add', '-A')
            b.git(state['worktree'], '-c','user.name=Test','-c','user.email=test@localhost','commit','-qm',variant)
            with self.subTest(variant=variant), self.assertRaises(b.GateError): self.call(self.args(sid))

    def test_owner_event_and_controls_reject_without_modification(self):
        sid=self.create(); args=self.args(sid); state,_=self.data(sid)
        for caller in (None,OTHER):
            with self.assertRaises(b.GateError): self.call(args,caller)
        for extra in ({'caller':OWNER},{'old_path':correction.OLD},{'new_path':correction.NEW}):
            with self.assertRaises(b.GateError): self.call({**args,**extra})
        controls=[{'phase':v} for v in ('running','accepted','awaiting_review','ready')]
        controls += [{'continuation':v} for v in ('paused','cancelled')]
        controls += [{'archived':True},{'clarification':{'questions':['why']}},{'last_result':{'exam_received':True}},
                     {'auto_check':{'status':'running'}},{'auto_check':{'status':'queued'}}]
        for control in controls:
            self.save(sid,{**state,**control}); raw=(b.run_dir(sid)/'state.json').read_bytes()
            with self.subTest(control=control),self.assertRaises(b.GateError): self.call(args)
            self.assertEqual((b.run_dir(sid)/'state.json').read_bytes(),raw)
        self.save(sid,state)
        with self.assertRaises(b.GateError): self.call({**args,'event_id':'unknown'})
        state['events'][args['event_id']]['status']='pending'; self.save(sid,state)
        with self.assertRaises(b.GateError): self.call(args)

    def test_recovery_does_not_reset_other_counts_usage_policy_or_no_progress(self):
        sid=self.create(); self.attempts(sid,2); state,_=self.data(sid)
        state.update(turns=17,infra_failures=2,auto_check={'status':'failed'},auto_check_history={'old':{'status':'failed'}},
                     progress_guard={'version':1,'streams':{'work':{'streak':3,'seen':[]}},'plans':[],'observed':[], 'replans_without_progress':0})
        self.save(sid,state); self.apply(sid); after,_=self.data(sid)
        for key in ('rounds','turns','infra_failures','auto_check_history','progress_guard','thread_id','owner','model','continuation'):
            self.assertEqual(state.get(key),after.get(key),key)
        with no_background_job(),self.assertRaisesRegex(b.GateError, 'progress'):
            b.start_job(sid,'evaluate',event_id=state['last_event_id'])

    def test_correction_or_history_tampering_fails_closed(self):
        sid=self.create(); self.attempts(sid,1); self.apply(sid); state,_=self.data(sid)
        row=state[correction.KEY]
        variants=[None,{**row,'extra':True},{k:v for k,v in row.items() if k!='recovery'}]
        for key,value in (('version',True),('kind','arbitrary'),('session_id','other'),('owner_thread_id',OTHER),
                          ('original_contract_hash','0'*64),('effective_contract_hash','0'*64),('proposal_hash','0'*64),
                          ('record_hash','0'*64),('arguments_hash','0'*64),('source_proof',{}),('prior_evidence',[])):
            variants.append({**row,key:value})
        changed=copy.deepcopy(row); changed['recovery']['granted']=5; variants.append(changed)
        changed=copy.deepcopy(row); changed['change']['after_program']+='\nassert True'; variants.append(changed)
        changed=copy.deepcopy(row); changed['arguments']['event_id']='other-event'
        changed['arguments_hash']=b.digest(changed['arguments']); variants.append(changed)
        for variant in variants:
            for rehash in (False,True):
                bad=copy.deepcopy(variant)
                if rehash and isinstance(bad,dict) and correction.CORE <= set(bad):
                    bad['proposal_hash']=b.digest({k:bad[k] for k in correction.CORE})
                    bad['record_hash']=b.digest({k:v for k,v in bad.items() if k!='record_hash'})
                if bad==row:continue
                self.save(sid,{**state,correction.KEY:bad})
                with self.subTest(rehash=rehash,variant=variant),self.assertRaises(b.GateError):self.data(sid)
        self.save(sid,{**state,'audit_check_correction':None})
        with self.assertRaises(b.GateError):self.data(sid)
        self.save(sid,state)
        path=b.run_dir(sid)/'attempt-1.json'; path.write_bytes(path.read_bytes()+b' ')
        with self.assertRaises(b.GateError):self.data(sid)

    def pytest_fixture(self):
        python=Path(os.environ.get('TELOS_TEST_PYTEST_PYTHON',sys.executable)).absolute()
        clean={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'PYTEST_DISABLE_PLUGIN_AUTOLOAD':'1'}
        if subprocess.run([str(python),'-I','-c','import pytest'],env=clean,capture_output=True).returncode:
            self.skipTest('Set TELOS_TEST_PYTEST_PYTHON to an existing pytest interpreter for real sandbox lifecycle checks')
        return str(python), sorted(set(str(p) for p in (python.parent.parent,python.resolve().parent.parent)))

    def test_real_pytest_failure_correction_recovery_exam_and_acceptance(self):
        python,deps=self.pytest_fixture()
        sid=self.create(max_rounds=1,code=program(python),dependency_paths=deps)
        failed=self.evaluate(sid); self.assertEqual(failed['phase'],'not_accepted',failed.get('last_error'))
        root=b.run_dir(sid); original=(root/'contract.json').read_bytes(); old=(root/'attempt-1.json').read_bytes()
        result=self.apply(sid); self.assertEqual(result['correction']['recovery']['granted'],1)
        self.assertEqual(self.data(sid)[0]['rounds'],1)
        good=self.evaluate(sid); self.assertEqual(good['phase'],'awaiting_review',good.get('last_error'))
        packet=b.review_packet(sid,'summary')
        self.assertEqual(packet['test_path_correction']['recovery']['consumed'],1)
        self.assertEqual(packet['identity']['executor_thread_id'],OTHER)
        self.assertTrue(b.grade(sid,good['packet_id'],self.scores(),[],event_id=good['last_event_id'])['accepted'])
        self.assertEqual((root/'contract.json').read_bytes(),original)
        self.assertEqual((root/'attempt-1.json').read_bytes(),old)
        self.assertEqual(good['rounds'],2)

    def test_real_product_failure_is_retained_after_path_correction(self):
        python,deps=self.pytest_fixture()
        sid=self.create(max_rounds=1,code=program(python),dependency_paths=deps)
        state,_=self.data(sid); logic=Path(state['worktree'])/correction.APP/'app_logic.py'
        logic.write_text('def add(a,b): return a-b\n')
        self.evaluate(sid); self.apply(sid)
        bad=self.evaluate(sid)
        self.assertEqual(bad['phase'],'not_accepted',bad.get('last_error'))
        self.assertFalse(bad['last_result']['exam_received'])
        self.assertEqual(correction.public(bad)['recovery']['remaining'],0)
        self.assertFalse(b.status(sid)['accepted'])
        with self.assertRaisesRegex(b.GateError,'Maximum exam rounds'):
            b.start_job(sid,'evaluate',event_id=bad['last_event_id'])


class ProjectPathTests(unittest.TestCase):
    setUp=PathCorrectionTests.setUp
    tearDown=PathCorrectionTests.tearDown
    data=PathCorrectionTests.data
    args=PathCorrectionTests.args
    call=PathCorrectionTests.call
    apply=PathCorrectionTests.apply
    attempts=PathCorrectionTests.attempts
    save=PathCorrectionTests.save
    evaluate=PathCorrectionTests.evaluate
    pytest_fixture=PathCorrectionTests.pytest_fixture
    scores=PathCorrectionTests.scores
    task=test_projects.ProjectTests.task
    start=test_projects.ProjectTests.start
    state=test_projects.ProjectTests.state
    check_sync=test_projects.ProjectTests.check_sync

    def create(self, real=False):
        python,deps=self.pytest_fixture() if real else ('/usr/bin/python3',[])
        task=self.task('repair',paths=[correction.APP],checks=contract(program(python))['checks'],dependency_paths=deps,max_rounds=1)
        manager=b.project_manager()
        pid=manager.create(repo=str(self.repo),task='Repair synthetic project',tasks=[task],questions=self.q,
                           checks=self.checks,decision_reason='Isolated fixture',strategy='single',notify=True,caller=OWNER)['project_id']
        manager.budget(pid,'unlimited','Retain user policy','Synthetic user authorization',caller=OWNER)
        self.start(pid)
        sid=manager.load(pid)['tasks'][0]['session_id']; state,_=self.data(sid)
        state.update(phase='responded',thread_id=OTHER,last_result={'summary':'Fixture complete'})
        b.record_event(state); self.save(sid,state)
        self.assertTrue(b.receive_event(sid,state['last_event_id'])['proceed'])
        return pid,sid

    def test_project_controls_metadata_and_unlimited_policy_are_preserved(self):
        pid,sid=self.create(); self.attempts(sid,1)
        manager=b.project_manager(); original=manager.load(pid)
        for change in ({'continuation':'paused'},{'continuation':'cancelled'},{'phase':'accepted'},
                       {'phase':'verifying'},{'phase':'awaiting_final_review'},
                       {'owner':{**original['owner'],'thread_id':OTHER}}):
            manager.save({**original,**change})
            with self.subTest(change=change),self.assertRaises(b.GateError):self.call(self.args(sid))
        manager.save(original); raw=manager.path(pid).read_bytes()
        self.apply(sid)
        self.assertEqual(manager.path(pid).read_bytes(),raw)
        summary=manager.status(pid,'summary')
        self.assertEqual(summary['tasks'][0]['test_path_correction']['recovery']['granted'],1)
        after=manager.load(pid)
        for key in ('contract','contract_hash','jobs_started','max_jobs','job_budget_policy','history'):
            self.assertEqual(original[key],after[key],key)
        for key in ('checks','questions','session_id','usage_id','executor','max_rounds'):
            self.assertEqual(original['tasks'][0][key],after['tasks'][0][key],key)

    def test_project_final_review_uses_new_effective_hash_and_packet(self):
        pid,sid=self.create(real=True)
        self.evaluate(sid); self.apply(sid); state=self.evaluate(sid)
        self.assertEqual(state['phase'],'awaiting_review',state.get('last_error'))
        self.assertTrue(b.grade(sid,state['packet_id'],self.scores(),[],event_id=state['last_event_id'])['accepted'])
        manager=b.project_manager(); manager.integrate(pid)
        review=self.check_sync(pid); evidence=manager.review(pid,'summary')['evidence']
        self.assertEqual(evidence['dependency_contracts']['repair']['contract_hash'],state['contract_hash'])
        self.assertEqual(evidence['deliveries']['repair']['packet_id'],state['packet_id'])
        self.assertEqual(evidence['test_path_corrections']['repair']['recovery']['consumed'],1)
        self.assertTrue(manager.finish(pid,review['packet_id'],self.scores(),[],
                                      'Reviewed fresh corrected check and integration evidence')['accepted'])


if __name__=='__main__':
    unittest.main()
