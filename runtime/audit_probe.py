"""Authenticated, bounded audit probes; executors never get unrestricted network.

HTTP requests do not follow redirects. Browser probes run a fixed declarative
driver against an explicitly supplied existing Playwright/browser installation.
"""
import contextlib
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

import audit
from isolation import NoRedirect
from providers import ProviderError as GateError
from processes import terminate_and_drain


HELP = {
    'http': {'request_id': 'unique-operation-id', 'check_id': 'checklist-id', 'kind': 'http',
             'url': 'http://127.0.0.1:8000/allowed-path', 'method': 'GET', 'headers': {}, 'body': None},
    'browser': {'request_id': 'unique-operation-id', 'check_id': 'checklist-id', 'kind': 'browser',
                'url': 'http://127.0.0.1:8000/allowed-page', 'viewport': 'desktop',
                'steps': [{'action': 'fill', 'selector': '#name', 'value': 'fixture'},
                          {'action': 'click', 'selector': '#save'},
                          {'action': 'assert_text', 'selector': '#result', 'value': 'Saved'}]},
    'notes': ['Use only actions/targets/methods granted in the public audit contract.',
              'Reuse a request_id only to retrieve that exact operation; a pending operation is never blindly repeated.',
              'Each browser probe uses a fresh context; include the complete flow (including login) in steps.',
              'Record returned evidence and screenshots in report.json. HTTP/assertion failures are findings, not quality failures.',
              'Do not include real credentials or personal data in reports; use scoped test accounts.']}


def validate_request(policy, request):
    audit.fields(request, ('request_id', 'check_id', 'kind', 'url', 'method', 'headers', 'body', 'viewport', 'steps'),
                 ('request_id', 'check_id', 'kind', 'url'), 'audit probe request')
    for key in ('request_id', 'check_id', 'url'):
        audit.text(request[key], key)
    item = next((i for i in policy['checklist'] if i['id'] == request['check_id']), None)
    if not item or request['kind'] not in ('http', 'browser'):
        raise GateError('Probe requires a known checklist item and http/browser kind')
    method = request.get('method', 'GET')
    if not audit.url_allowed(policy, request['url'], method):
        raise GateError('Probe URL/method is outside the frozen network boundary')
    if request['kind'] == 'http':
        if item['kind'] != 'api' or any(k in request for k in ('viewport', 'steps')):
            raise GateError('HTTP probes belong to API checklist items')
        headers = request.get('headers', {})
        if not isinstance(headers, dict) or len(headers) > 30 or any(
                not isinstance(k, str) or not isinstance(v, str) or '\n' in k + v or '\r' in k + v
                or k.lower() in ('host', 'proxy-authorization', 'connection', 'transfer-encoding', 'content-length')
                for k, v in headers.items()):
            raise GateError('Invalid probe request headers')
        body = request.get('body')
        if body is not None and (not isinstance(body, str) or len(body.encode()) > 1_000_000):
            raise GateError('HTTP request body must be a bounded string')
        if method in ('GET', 'HEAD', 'OPTIONS') and body is not None:
            raise GateError('Read-only HTTP methods cannot carry request bodies')
    else:
        browser = policy.get('browser')
        if item['kind'] not in ('interaction', 'visual') or not browser or any(k in request for k in ('method', 'body', 'headers')):
            raise GateError('Browser probes require a page checklist item and browser contract')
        if request.get('viewport') not in {v['name'] for v in browser['viewports']}:
            raise GateError('Viewport is outside the frozen browser boundary')
        if not isinstance(request.get('steps'), list) or len(request['steps']) > 30:
            raise GateError('Browser probe requires at most 30 declarative steps')
        for step in request['steps']:
            audit.fields(step, ('action', 'selector', 'value'), ('action',), 'browser step')
            if step['action'] not in browser['actions']:
                raise GateError('Browser action is outside the frozen boundary')
            if step['action'] not in ('screenshot', 'assert_url'):
                audit.text(step.get('selector'), 'selector')
            if step['action'] in ('fill', 'select', 'press', 'assert_text', 'assert_url'):
                audit.text(step.get('value'), 'step value')
            if step['action'] == 'assert_url' and not audit.url_allowed(policy, step['value']):
                raise GateError('Expected URL is outside scope')
    return request


def http_request(policy, request):
    req = urllib.request.Request(request['url'], method=request.get('method', 'GET'),
                                 data=request['body'].encode() if request.get('body') is not None else None,
                                 headers=request.get('headers', {}))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(req, timeout=policy['limits']['probe_timeout_seconds'])
    except urllib.error.HTTPError as error:
        response = error
    with response:
        body = response.read(1_000_001)
        return {'kind': 'http', 'url': request['url'], 'method': request.get('method', 'GET'),
                'status_code': response.code, 'outcome': 'passed' if 200 <= response.code < 300 else 'failed',
                'headers': {k: v for k, v in response.headers.items() if k.lower() in ('content-type', 'location')},
                'body': body[:1_000_000].decode(errors='replace'), 'truncated': len(body) > 1_000_000}


def http_probe(b, policy, request):
    # Socket timeouts alone are not a wall-clock budget for a slow/dripping body.
    # A fixed worker process gives the complete HTTP operation a hard deadline.
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--http'],
                                input=json.dumps({'policy': policy, 'request': request}), text=True,
                                capture_output=True, timeout=policy['limits']['probe_timeout_seconds'], env=b.minimal_env())
    except subprocess.TimeoutExpired:
        raise GateError('HTTP probe exceeded its frozen wall-clock timeout') from None
    if result.returncode:
        raise GateError('HTTP probe failed: ' + result.stderr[-1000:])
    return json.loads(result.stdout)


def browser_probe(b, policy, request, folder):
    browser = policy['browser']
    # Browser code and installed dependencies are trusted explicit local inputs;
    # arbitrary executor JS/shell commands are never accepted by this broker.
    driver = Path(__file__).with_name('audit_browser.cjs')
    node = shutil.which('node')
    if not node:
        raise GateError('Browser audit requires Node.js and an existing Playwright installation')
    module = Path(browser['module_path'])
    executable = Path(browser['executable_path'])
    if str(module.resolve()) != browser['module_path'] or str(executable.resolve()) != browser['executable_path']:
        raise GateError('Frozen browser dependency was redirected')
    app = next((p for p in executable.parents if p.suffix == '.app'), executable.parent)
    dependencies = [module.parent.parent, app, driver, Path(node).resolve()]
    env = b.minimal_env()
    env.update(HOME=str(folder), TMPDIR=str(folder), TMPPREFIX=str(folder / 'tmp'),
               # Chromium's GetTempDir on macOS reads MAC_CHROMIUM_TMPDIR, not TMPDIR;
               # force the staging folder even when the parent env carries a value.
               MAC_CHROMIUM_TMPDIR=str(folder))
    command = ['/usr/bin/sandbox-exec', '-p', b.sandbox_profile(network=True, writable=[folder],
               readable=dependencies), node, str(driver)]
    payload = {'policy': policy, 'request': request, 'folder': str(folder)}
    p = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env=env, cwd=folder, start_new_session=True)
    try:
        out, err = p.communicate(json.dumps(payload).encode(), timeout=policy['limits']['probe_timeout_seconds'] + 10)
    except subprocess.TimeoutExpired as expired:
        out, err, incomplete = terminate_and_drain(p, expired)
        b.write_json(folder / 'timeout.json', {'kind': 'browser_timeout',
                     'output_incomplete': incomplete, 'accepted': False})
        raise GateError('Browser probe timed out; inspect evidence before retrying') from None
    if p.returncode:
        raise GateError('Browser adapter failed: ' + err.decode(errors='replace')[-1000:])
    result = json.loads(out)
    result['kind'] = 'browser'
    return result


def run(b, sid, request, job_id=None):
    with (b.run_dir(sid) / 'audit-probe.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise GateError('An audit probe is already running for this task') from None
        return _run(b, sid, request, job_id)


def _run(b, sid, request, job_id=None):
    with b.locked(sid) as root:
        state, contract = b.load_session(root)
        if contract.get('task_type') != 'audit' or state.get('archived') or state.get('continuation') != 'active':
            raise GateError('Audit probe needs an active audit task')
        if state.get('project_id'):
            project = b.project_manager().load(state['project_id'])
            if project['continuation'] != 'active':
                raise GateError('Project is paused/cancelled')
        allowed = state['phase'] in ('ready', 'responded', 'needs_clarification', 'not_accepted')
        if job_id:
            allowed = state['phase'] == 'running' and state.get('job_kind') == 'send' and state.get('job_id') == job_id
        if not allowed:
            raise GateError('Probes are allowed only during audit work, before evaluation')
        policy = contract['audit']; validate_request(policy, request)
        path = root / 'audit-probes.json'
        ledger = b.read_json(path) if path.exists() else {'requests': {}, 'completed': []}
        request_hash = b.digest(request)
        previous = ledger['requests'].get(request['request_id'])
        if previous:
            if previous['hash'] != request_hash:
                raise GateError('Probe request_id was reused with different arguments')
            if 'result' not in previous:
                raise GateError('Previous probe outcome is uncertain; it will not be repeated automatically')
            return previous['result']
        if len(ledger['requests']) >= policy['limits']['max_probes']:
            raise GateError('Frozen audit probe budget exhausted')
        ledger['requests'][request['request_id']] = {'hash': request_hash, 'at': time.time()}
        b.write_json(path, ledger)
    # Stage out of executor reach; publish evidence only after the operation ends.
    ident = secrets.token_hex(12)
    # Chromium's Unix-domain socket paths have a small platform limit. Grant
    # only this private short directory, never the entire system temp root.
    staging = Path(tempfile.mkdtemp(prefix='telos-audit-', dir='/private/tmp')).resolve()
    started = time.time()
    try:
        try:
            result = http_probe(b, policy, request) if request['kind'] == 'http' else browser_probe(b, policy, request, staging)
        except (GateError, OSError, ValueError) as error:
            result = {'kind': request['kind'], 'outcome': 'blocked', 'error': str(error)[:1500]}
        result.update(check_id=request['check_id'], observed_at=started, seconds=round(time.time() - started, 3),
                      source_commit=state['base_commit'], environment=policy['environment'])
        b.write_json(staging / 'probe.json', result)
        # Browser profiles/temp files are never published or exposed as evidence.
        publish = root / ('audit-publish-' + ident)
        publish.mkdir(mode=0o700)
        for item in staging.iterdir():
            if item.name == 'probe.json' or item.name.startswith('step-') and item.suffix == '.png':
                if item.is_symlink() or not item.is_file():
                    raise GateError('Invalid browser evidence file')
                shutil.copyfile(item, publish / item.name)
        captured = audit.manifest(publish, policy['limits']['max_artifact_bytes'])
        destination = audit.artifacts(b, state) / ('probe-' + ident)
        # Atomic publication avoids an executor-created symlink directing writes.
        if destination.exists() or destination.is_symlink() or destination.parent.is_symlink():
            raise GateError('Unsafe audit evidence destination')
        existing = audit.manifest(destination.parent, policy['limits']['max_artifact_bytes'])
        if sum(f['bytes'] for f in existing.values()) + sum(f['bytes'] for f in captured.values()) > policy['limits']['max_artifact_bytes']:
            raise GateError('Audit evidence exceeds the frozen artifact budget')
        publish.rename(destination)
        prefix = destination.name + '/'
        evidence = prefix + 'probe.json'
        result = {**result, 'evidence': evidence,
                  'screenshots': [prefix + name for name in captured if name.endswith('.png')]}
        with b.locked(sid):
            ledger = b.read_json(path)
            ledger['requests'][request['request_id']]['result'] = result
            ledger['completed'].append({'check_id': request['check_id'], 'kind': request['kind'],
                                        'viewport': request.get('viewport'),
                                        'evidence': evidence, 'files': {prefix + n: v['sha256'] for n, v in captured.items()}})
            b.write_json(path, ledger)
        return result
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if 'publish' in locals() and publish.exists():
            shutil.rmtree(publish)


@contextlib.contextmanager
def service(b, state, contract):
    token = secrets.token_urlsafe(32)
    b.write_json(audit.artifacts(b, state) / 'audit-probe-help.json', HELP)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            if self.path != '/probe' or not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
                self.send_error(403); return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 1_100_000:
                    raise GateError('Probe request exceeds size limit')
                request = json.loads(self.rfile.read(length))
                result = run(b, state['session_id'], request, job_id=state['job_id'])
                body = json.dumps(result).encode(); status = 200
            except Exception as error:
                body = json.dumps({'error': str(error)[:1500]}).encode(); status = 400
            self.send_response(status); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        yield {'url': f'http://127.0.0.1:{server.server_port}/probe', 'token': token, 'port': server.server_port}
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == '__main__':
    if sys.argv[1:] != ['--http']:
        raise SystemExit('Internal HTTP probe worker; use the Telos audit tools')
    try:
        payload = json.load(sys.stdin)
        print(json.dumps(http_request(payload['policy'], payload['request'])))
    except Exception as error:
        print(str(error)[:1000], file=sys.stderr)
        raise SystemExit(1)
