"""Static preflight: no execution, no state writes, conservative diagnostics."""
import ast
import os
from pathlib import Path
import tempfile
import unittest

import check_preflight
from audit_schema import REPORT_TOP_LEVEL


def check(code, ident='c1'):
    return {'id': ident, 'argv': ['{python}', '-c', code]}


class PreflightUnitTests(unittest.TestCase):
    def test_valid_program_is_not_executed_and_writes_nothing(self):
        marker = Path(tempfile.mkdtemp()) / 'executed'
        before = set(os.listdir(marker.parent))
        result = check_preflight.analyze(checks=[check(
            "from pathlib import Path\nPath(%r).write_text('x')\nassert True\n" % str(marker))])
        self.assertTrue(result['ok'])
        self.assertFalse(marker.exists())
        self.assertEqual(before, set(os.listdir(marker.parent)))
        self.assertFalse(result['executed'])

    def test_syntax_error_is_hard(self):
        result = check_preflight.analyze(checks=[check('assert (')])
        self.assertFalse(result['ok'])
        self.assertEqual(result['diagnostics'][0]['code'], 'syntax_error')

    def test_provable_audit_top_level_conflict_is_hard(self):
        contract = {'task_type': 'audit'}
        result = check_preflight.analyze(contract=contract, checks=[check(
            "import json, os, pathlib\n"
            "p = json.loads(pathlib.Path(os.environ['TELOS_AUDIT_REPORT']).read_text())\n"
            "assert p.get('unsupported_summary') is not None\n")])
        self.assertFalse(result['ok'])
        self.assertEqual(result['diagnostics'][0]['code'], 'audit_report_top_level')

    def test_unknown_report_key_is_none_is_not_rejected(self):
        result = check_preflight.analyze(contract={'task_type': 'audit'}, checks=[check(
            "import json, os, pathlib\n"
            "p = json.loads(pathlib.Path(os.environ['TELOS_AUDIT_REPORT']).read_text())\n"
            "assert p.get('unknown') is None\n")])
        self.assertTrue(result['ok'])

    def test_disabled_branch_and_unrelated_dict_are_not_rejected(self):
        result = check_preflight.analyze(contract={'task_type': 'audit'}, checks=[check(
            "import json, os, pathlib\n"
            "p = json.loads(pathlib.Path(os.environ['TELOS_AUDIT_REPORT']).read_text())\n"
            "if False:\n    assert p.get('unrelated') is not None\n"
            "other = {}\nassert other['whatever'] == 1\n")])
        self.assertTrue(result['ok'])

    def test_explicit_missing_pytest_path_is_advisory(self):
        result = check_preflight.analyze(checks=[check(
            "import subprocess\n"
            "subprocess.run(['pytest', 'tests/test_generated_case.py'], check=True)\n")])
        self.assertTrue(result['ok'])
        self.assertIn('test_path_not_found',
                      [item['code'] for item in result['diagnostics']])

    def test_test_path_under_a_top_level_project_folder_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'app' / 'tests').mkdir(parents=True)
            (Path(folder) / 'app' / 'tests' / 'test_present.py').write_text('x')
            code = ("import subprocess\n"
                    "subprocess.run(['pytest', 'tests/test_present.py', 'tests/test_absent.py'])\n")
            result = check_preflight.analyze(checks=[check(code)], root=folder)
        flagged = [item['message'] for item in result['diagnostics'] if item['code'] == 'test_path_not_found']
        self.assertEqual(len(flagged), 1)
        self.assertIn('test_absent.py', flagged[0])
        self.assertTrue(result['ok'])

    def test_public_warnings_are_bounded_deduplicated_and_warning_only(self):
        self.assertEqual(check_preflight.public_warnings({'diagnostics': []}), {})
        self.assertEqual(check_preflight.public_warnings(None), {})
        rows = [{'check_id': 'c', 'severity': 'warning', 'code': 'test_path_not_found', 'message': 'path %d' % n}
                for n in range(14)]
        rows += [rows[0], {'check_id': 'c', 'severity': 'error', 'code': 'syntax_error', 'message': 'bad'}]
        shown = check_preflight.public_warnings({'diagnostics': rows})
        self.assertEqual(len(shown['preflight_warnings']), check_preflight.MAX_PUBLIC_WARNINGS)
        self.assertEqual(shown['preflight_warnings_omitted'], 4)
        self.assertNotIn('syntax_error', [w['code'] for w in shown['preflight_warnings']])
        self.assertEqual(set(shown['preflight_warnings'][0]), {'check_id', 'code', 'message'})

    def test_known_audit_top_level_is_accepted(self):
        result = check_preflight.analyze(contract={'task_type': 'audit'}, checks=[check(
            "report = {}\nfor key in %r:\n    assert report.get(key) is not None\n" % (REPORT_TOP_LEVEL,))])
        self.assertTrue(result['ok'])

    def test_dynamic_audit_key_is_advisory(self):
        result = check_preflight.analyze(contract={'task_type': 'audit'}, checks=[check(
            "import json, os, pathlib\nreport = json.loads(pathlib.Path(os.environ['TELOS_AUDIT_REPORT']).read_text())\nkey = input()\nassert report[key]\n")])
        self.assertTrue(result['ok'])
        self.assertEqual(result['diagnostics'][0]['severity'], 'warning')
        self.assertEqual(result['diagnostics'][0]['code'], 'audit_report_dynamic_key')

    def test_missing_generated_path_is_advisory(self):
        result = check_preflight.analyze(checks=[check(
            "from pathlib import Path\nassert Path('generated/out.txt').exists()\n")])
        self.assertTrue(result['ok'])
        self.assertEqual(result['diagnostics'][0]['code'], 'path_not_found')
        self.assertEqual(result['diagnostics'][0]['severity'], 'warning')

    def test_paths_literals_defaults_and_unknown_control_flow_are_not_reports(self):
        prefix = ("import json, os, pathlib\n"
                  "p = json.loads(pathlib.Path(os.environ['TELOS_AUDIT_REPORT']).read_text())\n")
        programs = [
            "p = {'TELOS_AUDIT_REPORT': 1}\nassert p['custom']\n",
            "import os\np = os.environ['TELOS_AUDIT_REPORT']\nassert p['custom']\n",
            prefix + "assert p.get('custom', True)\n",
            prefix + "assert not p.get('custom')\n",
            prefix + "try:\n p['custom']\nexcept KeyError:\n pass\n",
            prefix + "p['custom'] = True\nassert p['custom']\n",
            prefix + "p.update(custom=True)\nassert p['custom']\n",
            prefix + "modify(p)\nassert p['custom']\n",
            "import json, os, pathlib\njson.loads = lambda text: {'custom': True}\n" +
            prefix.split('\n', 1)[1] + "assert p['custom']\n",
            prefix + "if False:\n pass\nelse:\n assert p.get('custom', True)\n",
        ]
        for program in programs:
            with self.subTest(program=program):
                self.assertTrue(check_preflight.analyze(
                    contract={'task_type': 'audit'}, checks=[check(program)])['ok'])

    def test_import_alias_and_separate_path_load_are_detected(self):
        program = ("from json import loads as decode\nfrom pathlib import Path as P\n"
                   "import os as env\npath = P(env.environ['TELOS_AUDIT_REPORT'])\n"
                   "content = path.read_text()\nvalue = decode(content)\n"
                   "assert value['custom']\n")
        self.assertFalse(check_preflight.analyze(
            contract={'task_type': 'audit'}, checks=[check(program)])['ok'])

    def test_non_python_check_is_not_parsed_or_executed(self):
        result = check_preflight.analyze(checks=[{'id': 't', 'argv': ['pytest', '-q']}])
        self.assertTrue(result['ok'])
        self.assertEqual(result['programs'], 0)

    def test_compile_only_never_calls_exec_or_eval(self):
        source = Path(check_preflight.__file__).read_text()
        tree = ast.parse(source)
        forbidden = {'exec', 'eval'}
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        self.assertFalse(forbidden & names)

    def test_shared_report_constant_matches_actual_validation(self):
        audit = Path(check_preflight.__file__).with_name('audit.py').read_text()
        self.assertIn('REPORT_TOP_LEVEL', audit)
        self.assertEqual(REPORT_TOP_LEVEL, ('source_commit', 'environment', 'results', 'findings'))


if __name__ == '__main__':
    unittest.main()
