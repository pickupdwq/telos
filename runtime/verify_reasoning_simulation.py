#!/usr/bin/env python3
"""Real CLI -> Telos relay -> local Postman fixture, through accepted=true."""
import argparse
import contextlib
import json
from pathlib import Path
import unittest
import urllib.request
from urllib.parse import urlsplit
import uuid
from unittest.mock import patch

import isolation
from test_effort_policy import JobTests, b


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--installed-config', type=Path)
    args = parser.parse_args()
    url = urlsplit(args.base_url)
    if (url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port
            or url.username or url.password or url.query or url.fragment
            or url.path != '/reasoning/compatible-mode/v1'):
        parser.error('Use http://127.0.0.1:<port>/reasoning/compatible-mode/v1')
    run_id = uuid.uuid4().hex
    real_relay = isolation.model_relay
    observed = []

    class Simulation(JobTests):
        def setUp(self):
            super().setUp()
            if args.installed_config:
                # Isolate real policy/defaults; no credentials or real state are used.
                cfg = json.loads(args.installed_config.read_text())
                self.assertIs(cfg['dynamic_reasoning_enabled'], True)
                self.assertIs(cfg['task_token_limits_enabled'], False)
                b.write_json(b.CONFIG, {**cfg, 'job_timeout': 35, 'watch': {'auto_tmux': False}})

        def create(self, **kwargs):
            return super().create(context='REASONING_FIXTURE_ID: ' + run_id, **kwargs)

        @contextlib.contextmanager
        def model(self, reply):
            def relay(endpoint, key, timeout, **kwargs):
                return real_relay(args.base_url, key, timeout, **kwargs)
            with patch.object(b, 'provider_credential', return_value=('SYNTHETIC_FIXTURE_ONLY', 'test')), \
                    patch.object(isolation, 'model_relay', side_effect=relay):
                yield observed
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                evidence_url = f'http://127.0.0.1:{url.port}/reasoning/fixture/evidence/{run_id}'
                with opener.open(evidence_url, timeout=10) as response:
                    observed.extend(json.load(response)['requests'])

    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([
        Simulation('test_real_cli_resume_http_and_grade_use_selected_effort')]))
    if not result.wasSuccessful():
        raise SystemExit(1)
    print(json.dumps({'passed': True, 'accepted': True, 'same_cli_thread': True,
                      'efforts_received': [r['reasoning']['effort'] for r in observed],
                      'boundary': 'real CLI, relay, checks and grade; local deterministic model fixture'}, indent=2))


if __name__ == '__main__':
    main()
