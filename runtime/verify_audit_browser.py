#!/usr/bin/env python3
"""Opt-in local browser verification; no models, remote sites or installations.

Pass an existing Playwright module and Chromium executable. Uses an isolated
fixture server with deliberately broken mobile layout and denied requests.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import unittest

import audit
import audit_probe
from test_audit import AuditTests, boundary, b


class BrowserAuditTests(unittest.TestCase):
    setUp = AuditTests.setUp
    tearDown = AuditTests.tearDown
    create = AuditTests.create
    data = AuditTests.data
    evaluate = AuditTests.evaluate
    review = AuditTests.review
    accept = AuditTests.accept
    scores = AuditTests.scores

    def test_real_browser_desktop_mobile_interaction_evidence_and_boundary(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                calls.append(self.path)
                if self.path == '/ui/redirect':
                    self.send_response(302); self.send_header('Location', '/outside'); self.end_headers(); return
                self.send_response(200); self.send_header('Content-Type', 'text/html'); self.end_headers()
                self.wfile.write(b'''<!doctype html><html><head><title>Audit fixture</title></head><body>
<div style="width:800px;background:#eee">Deliberately fixed-width layout</div>
<label>Name <input id="name"></label><button id="save" onclick="document.querySelector('#result').textContent='Saved: '+document.querySelector('#name').value">Save</button>
<div id="result">Empty</div><script>fetch('/outside').catch(()=>{});fetch('/ui/redirect').catch(()=>{});</script></body></html>''')
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}'
            policy = boundary()
            policy['targets'] = [{'origin': url, 'path_prefix': '/ui', 'methods': ['GET']}]
            policy['checklist'] = [{'id': name, 'kind': 'visual' if name == 'mobile' else 'interaction',
                                    'prompt': 'Check save flow and layout at ' + name, 'expected': 'Save works and viewport does not overflow',
                                    'viewports': [name]}
                                   for name in ('desktop', 'mobile')]
            policy['browser'] = {'module_path': ARGS.module, 'executable_path': ARGS.browser,
                                 'viewports': [{'name': 'desktop', 'width': 1280, 'height': 800},
                                               {'name': 'mobile', 'width': 390, 'height': 844}],
                                 'actions': ['fill', 'click', 'assert_text']}
            sid = self.create(policy)
            observed = {}
            for name in ('desktop', 'mobile'):
                result = audit_probe.run(b, sid, {'request_id': name, 'check_id': name, 'kind': 'browser',
                    'url': url + '/ui', 'viewport': name, 'steps': [
                        {'action': 'fill', 'selector': '#name', 'value': 'fixture'},
                        {'action': 'click', 'selector': '#save'},
                        {'action': 'assert_text', 'selector': '#result', 'value': 'Saved: fixture'}]})
                self.assertEqual(result['outcome'], 'passed', result)
                self.assertEqual(len(result['screenshots']), 4)
                self.assertIn('Saved: fixture', result['visible_text'])
                self.assertTrue(any('/outside' in r['url'] for r in result['blocked_requests']))
                self.assertTrue(any(r.get('reason') == 'redirect' for r in result['blocked_requests']))
                self.assertEqual(result['layout']['horizontal_overflow'], name == 'mobile')
                observed[name] = result
            self.assertNotIn('/outside', calls)
            state, _ = self.data(sid); root = audit.artifacts(b, state)
            report = b.read_json(root / 'report.json')
            for item in report['results']:
                result = observed[item['id']]
                item.update(status='finding' if item['id'] == 'mobile' else 'passed',
                            actual='Save completes; mobile layout overflows' if item['id'] == 'mobile' else 'Save completes',
                            steps=['Open /ui', 'Fill name', 'Click save', 'Verify result and viewport'],
                            evidence=[result['evidence'], *result['screenshots']])
            report['findings'] = [{'id': 'layout-overflow', 'check_id': 'mobile', 'category': 'defect', 'severity': 'medium',
                                  'title': 'Fixed width exceeds mobile viewport', 'expected': 'Content fits viewport',
                                  'actual': '800px content exceeds 390px viewport', 'location': '/ui, first div',
                                  'steps': ['Open /ui at 390x844'], 'evidence': report['results'][1]['evidence']}]
            b.write_json(root / 'report.json', report)
            state = self.evaluate(sid)
            self.assertEqual(state['phase'], 'awaiting_review', state)
            review = self.review(sid, [{'id': 'layout-overflow', 'disposition': 'confirmed', 'rationale': 'Checked DOM geometry and captured evidence'}])
            missing = {**review, 'visual_evidence_reviewed': []}
            with self.assertRaisesRegex(b.GateError, 'screenshots'):
                b.grade(sid, state['packet_id'], self.scores(), [], audit_review=missing)
            # This verifier uses a deterministic supervisor boundary; screenshot
            # evidence is exported for an actual visual review by its caller.
            result = b.grade(sid, state['packet_id'], self.scores(), [], audit_review=review)
            self.assertTrue(result['accepted'])
            destination = Path(ARGS.report).resolve().parent / 'browser-audit-evidence'
            import shutil
            if destination.exists():
                destination = destination.with_name(destination.name + '-' + sid)
            shutil.copytree(root, destination)
            Path(ARGS.report).write_text(json.dumps({'passed': True, 'source_commit': state['base_commit'],
                'desktop_and_mobile': True, 'interaction_verified': True, 'scope_denial_verified': True,
                'mobile_layout_defect_detected': True, 'visual_review_gate_verified': True,
                'evidence_dir': str(destination), 'boundary': 'Real Chromium and HTTP; deterministic model/exam/supervisor fixtures'}, indent=2) + '\n')
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--module', required=True, help='Existing playwright/index.js')
    parser.add_argument('--browser', required=True, help='Existing Chromium executable')
    parser.add_argument('--report', required=True)
    ARGS = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(BrowserAuditTests)
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
