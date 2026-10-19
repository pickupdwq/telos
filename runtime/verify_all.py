#!/usr/bin/env python3
"""Run every locally declared regression once, with real Git/macOS sandboxes."""
import importlib
import json
from pathlib import Path
import sys
import unittest

suite = unittest.TestSuite()
for module_name in ['test_telos', 'test_callbacks', 'test_repairs', 'test_projects', 'test_experience', 'test_rsi', 'test_providers', 'test_closed_book', 'test_maintenance', 'test_hardening', 'test_identity', 'test_audit']:
    module = importlib.import_module(module_name)
    for cls in vars(module).values():
        if isinstance(cls, type) and issubclass(cls, unittest.TestCase) and cls.__module__ == module_name:
            for name in cls.__dict__:
                if name.startswith('test_'):
                    suite.addTest(cls(name))
result = unittest.TextTestRunner(verbosity=2).run(suite)
report = {'tests': result.testsRun, 'passed': result.wasSuccessful(),
          'failures': len(result.failures), 'errors': len(result.errors), 'skipped': len(result.skipped),
          'real_git_and_sandbox': True, 'model_boundary': 'deterministic mocks; separate live report required'}
if len(sys.argv) > 1:
    Path(sys.argv[1]).write_text(json.dumps(report, indent=2) + '\n')
raise SystemExit(not result.wasSuccessful())
