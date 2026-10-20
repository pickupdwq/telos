"""Read allowlist and a per-job, authenticated, fixed-destination model relay."""
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import secrets
import shutil
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
import sys
from identity import data_path


def runtime_paths(install, home):
    from preflight import git_runtime_paths
    # No entire user home, /tmp, /var, source repository or sibling project.
    return [Path(x) for x in ('/System', '/usr', '/bin', '/sbin', '/opt/homebrew',
            '/Library/Apple', '/Library/Frameworks', '/private/var/db/dyld',
            '/private/etc/ssl', '/private/etc/hosts', '/private/etc/resolv.conf',
            '/private/etc/localtime', '/dev/null', '/dev/tty', '/dev/random', '/dev/urandom')] + [
            Path(sys.base_prefix), install / 'commands', data_path(home, '.local/share/{name}-cli')] + [
            Path(p).resolve() for name in ('node',) if (p := shutil.which(name))] + git_runtime_paths()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class RelayCloseout:
    """Stop admissions, then give existing responses bounded time to settle."""

    def __init__(self, duration=None, request_window=0, grace=5):
        self.deadline = time.monotonic() + duration if duration is not None else None
        self.request_window = request_window
        self.grace = grace
        self.condition = threading.Condition()
        self.active = {}
        self.closing_at = None
        self.reason = None

    def start(self, duration):
        """Arm once at executor launch, after preflight and local setup."""
        with self.condition:
            if self.deadline is not None or self.closing_at is not None:
                raise RuntimeError('Execution deadline already started or closing')
            self.deadline = time.monotonic() + duration

    def begin(self, interrupt):
        with self.condition:
            if self.closing_at is not None:
                raise TimeoutError('Execution is closing; no upstream request was sent')
            if self.deadline is not None and time.monotonic() + self.request_window >= self.deadline:
                self.stop('execution_deadline')
                raise TimeoutError('Execution admission deadline reached; no upstream request was sent')
            lease = object()
            self.active[lease] = [interrupt, None]
            return lease

    def stop(self, reason):
        with self.condition:
            if self.closing_at is None:
                self.closing_at = time.monotonic() + self.grace
                self.reason = reason
            self.condition.notify_all()

    @staticmethod
    def response_socket(response):
        # CPython urllib HTTP(S)Response owns this socket. Other response types
        # still have the bounded request timeout and interrupted ledger fallback.
        return getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)

    def track(self, lease, response):
        with self.condition:
            if lease in self.active:
                self.active[lease][1] = self.response_socket(response)

    def timeout(self, configured):
        with self.condition:
            ends = [x for x in (self.closing_at,
                               self.deadline + self.grace if self.deadline is not None else None)
                    if x is not None]
        remaining = min(ends) - time.monotonic() if ends else configured
        if remaining <= 0:
            raise TimeoutError('Execution closeout deadline reached')
        return min(configured, remaining)

    def end(self, lease):
        with self.condition:
            self.active.pop(lease, None)
            self.condition.notify_all()

    def drain(self):
        self.stop('execution_finished')
        with self.condition:
            while self.active:
                remaining = self.closing_at - time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(remaining)
            pending = list(self.active.values())
        for interrupt, sock in pending:
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            # Never leave an interrupted reservation looking like an ongoing
            # request forever. Missing provider usage remains unknown, not zero.
            interrupt()


# Only the current SSE event is retained. The cap matches the largest response
# body the relay accepts elsewhere; an oversized event is discarded rather than
# buffered without bound. If the discarded event was the terminal event, the
# request completes with unknown usage and a configured token budget stops the
# next paid request. Total stream length is never buffered.
SSE_MAX_EVENT_BYTES = 16 * 1024 * 1024
_TERMINAL_EVENTS = {'response.completed': 'completed', 'response.incomplete': 'incomplete',
                    'response.failed': 'failed', 'response.cancelled': 'cancelled',
                    'response.canceled': 'canceled'}


class SSEUsageTracker:
    """Incrementally parse SSE events while bytes stream through unchanged."""

    def __init__(self, max_event_bytes=SSE_MAX_EVENT_BYTES):
        self.max_event_bytes = max_event_bytes
        self.line = bytearray()
        self.skip_lf = False
        self.data_lines = []
        self.event_bytes = 0
        self.overflow = False
        self.line_discarded = False
        self.usage = None
        self.outcome = None

    def _discard_event(self):
        self.overflow = True
        self.line.clear()
        self.line_discarded = True
        self.data_lines = []

    def _reset_event(self):
        self.overflow = False
        self.line_discarded = False
        self.data_lines = []
        self.event_bytes = 0

    def _dispatch(self):
        if self.overflow:
            self._reset_event()
            return
        data = b'\n'.join(self.data_lines)
        self._reset_event()
        if not data or data.strip() == b'[DONE]':
            return
        try:
            event = json.loads(data.decode('utf-8', errors='replace'))
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        kind = event.get('type')
        if kind not in _TERMINAL_EVENTS:
            return
        # Duplicate terminal events replace, never accumulate.
        self.outcome = _TERMINAL_EVENTS[kind]
        response = event.get('response')
        candidate = response.get('usage') if isinstance(response, dict) else None
        if not isinstance(candidate, dict):
            candidate = event.get('usage')
        if isinstance(candidate, dict):
            self.usage = candidate

    def _line_done(self):
        line = bytes(self.line)
        self.line.clear()
        discarded = self.line_discarded
        self.line_discarded = False
        if self.overflow:
            # Keep discarding until a blank line really terminates this event.
            if line or discarded:
                return
            self._reset_event()
            return
        if not line:
            self._dispatch()
            return
        if line == b'data':
            value = b''
        elif line.startswith(b'data:'):
            value = line[5:]
            if value.startswith(b' '):
                value = value[1:]
        else:
            return
        self.event_bytes += len(value) + 1
        if self.event_bytes > self.max_event_bytes:
            # The oversized data line already ended at this boundary, so the
            # next empty line is the event terminator (not a pending line tail).
            self.overflow = True
            self.line_discarded = False
            self.data_lines = []
            return
        self.data_lines.append(value)

    def feed(self, chunk):
        for byte in chunk:
            if self.skip_lf:
                self.skip_lf = False
                if byte == 0x0A:
                    continue
            if byte in (0x0A, 0x0D):
                self._line_done()
                if byte == 0x0D:
                    self.skip_lf = True
                continue
            if self.overflow:
                self.line_discarded = True
                continue
            if len(self.line) + 1 > self.max_event_bytes - self.event_bytes:
                self._discard_event()
                continue
            self.line.append(byte)

    def finish(self):
        if self.line:
            self._line_done()
        if self.data_lines or self.overflow:
            self._dispatch()
        return self.usage, self.outcome


def usage_from_payload(raw, content_type):
    """Extract one usage object from a JSON/SSE Responses or Chat payload."""
    if not raw:
        return None
    text = raw.decode('utf-8', errors='replace') if isinstance(raw, (bytes, bytearray)) else str(raw)
    if 'text/event-stream' in (content_type or ''):
        usage = None
        for block in text.replace('\r\n', '\n').split('\n\n'):
            data = '\n'.join(line[5:].lstrip() for line in block.splitlines() if line.startswith('data:'))
            if not data or data == '[DONE]':
                continue
            try:
                event = json.loads(data)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get('type') in ('response.completed', 'response.incomplete', 'response.failed',
                                     'response.cancelled', 'response.canceled'):
                response = event.get('response')
                candidate = response.get('usage') if isinstance(response, dict) else None
                if not isinstance(candidate, dict):
                    candidate = event.get('usage')
                if isinstance(candidate, dict):
                    # Duplicate terminal events replace, never accumulate.
                    usage = candidate
        return usage
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    response = payload.get('response')
    if isinstance(response, dict) and isinstance(response.get('usage'), dict):
        return response['usage']
    usage = payload.get('usage')
    return usage if isinstance(usage, dict) else None


def response_outcome(raw, content_type):
    """Return the terminal Responses status without retaining provider bodies.

    Malformed JSON and SSE streams that end before a terminal event are reported
    as failures so accounting never records them as completed successes.
    """
    stream = 'text/event-stream' in (content_type or '')
    json_body = 'application/json' in (content_type or '')
    if not raw:
        return 'missing_terminal' if stream else ('malformed' if json_body else None)
    text = raw.decode('utf-8', errors='replace') if isinstance(raw, (bytes, bytearray)) else str(raw)
    if stream:
        outcome = None
        for block in text.replace('\r\n', '\n').split('\n\n'):
            data = '\n'.join(line[5:].lstrip() for line in block.splitlines() if line.startswith('data:'))
            if not data or data == '[DONE]':
                continue
            try:
                event = json.loads(data)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get('type')
            if kind == 'response.completed':
                outcome = 'completed'
            elif kind == 'response.incomplete':
                outcome = 'incomplete'
            elif kind == 'response.failed':
                outcome = 'failed'
            elif kind in ('response.cancelled', 'response.canceled'):
                outcome = 'cancelled'
        return outcome or 'missing_terminal'
    try:
        payload = json.loads(text)
    except ValueError:
        return 'malformed' if json_body else None
    if not isinstance(payload, dict):
        return 'malformed' if json_body else None
    response = payload.get('response')
    status = response.get('status') if isinstance(response, dict) else payload.get('status')
    if not isinstance(status, str):
        return None
    status = status.lower()
    return status if status in ('completed', 'incomplete', 'failed', 'cancelled', 'canceled') else None


def _terminal(status, outcome):
    if outcome in ('failed', 'incomplete', 'cancelled', 'canceled', 'malformed', 'missing_terminal'):
        return 'failed', 'response_' + outcome
    return status, None


def _bailian_request(raw, binding):
    """Pair historical tool results as required by Bailian's Responses API.

    Codex can retain [call1, call2, result1, result2] even when the catalogue
    disables parallel tools. Only reorder a complete, contiguous tool batch;
    never move results across messages or discard ambiguous history.
    """
    request = json.loads(raw)
    if request.get('model') != binding['model']:
        raise ValueError('Bailian request changed the frozen model')
    request['parallel_tool_calls'] = False
    history = request.get('input')
    if isinstance(history, list):
        pairs = {'function_call': 'function_call_output', 'custom_tool_call': 'custom_tool_call_output'}
        output_types = set(pairs.values())
        ordered, seen = [], set()
        index = 0
        while index < len(history):
            item = history[index]
            kind = item.get('type')
            if kind not in pairs:
                if kind in output_types:
                    raise ValueError('Bailian history has an unmatched tool result')
                ordered.append(item)
                index += 1
                continue
            calls = []
            while index < len(history) and history[index].get('type') in pairs:
                call = history[index]
                call_id = call.get('call_id')
                if not isinstance(call_id, str) or not call_id or call_id in seen:
                    raise ValueError('Bailian history has a missing or duplicate tool call ID')
                seen.add(call_id)
                calls.append(call)
                index += 1
            outputs = {}
            while index < len(history) and history[index].get('type') in output_types:
                output = history[index]
                call_id = output.get('call_id')
                if not isinstance(call_id, str) or not call_id or call_id in outputs:
                    raise ValueError('Bailian history has a missing or duplicate tool result ID')
                outputs[call_id] = output
                index += 1
            if set(outputs) != {call['call_id'] for call in calls} or any(
                    outputs[call['call_id']]['type'] != pairs[call['type']] for call in calls):
                raise ValueError('Bailian history requires complete adjacent tool call/result batches')
            for call in calls:
                ordered.extend((call, outputs[call['call_id']]))
        request['input'] = ordered
    return json.dumps(request, ensure_ascii=False).encode()


@contextlib.contextmanager
def model_relay(endpoint, key, timeout=180, policy=None, binding=None, validate=None, diagnostics=None,
                usage_ledger=None, usage_context=None, budget_failure=None, closeout=None):
    """Only the bridge holds upstream credentials. No arbitrary URL/proxy methods."""
    token = secrets.token_urlsafe(48)
    local = binding is not None and binding.get('provider') == 'local'
    bailian = binding is not None and binding.get('provider') == 'bailian'
    opener = urllib.request.build_opener(NoRedirect(), *([urllib.request.ProxyHandler({})] if local else []))
    context = usage_context or {}
    closeout = closeout or RelayCloseout()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
                self.send_error(403); return
            allowed = ('/v1/responses',) if policy or local or bailian else ('/v1/responses', '/v1/responses/compact')
            if self.path not in allowed:
                self.send_error(404); return
            if budget_failure and budget_failure.get('message'):
                # A budget stop or accounting failure is terminal for this relay:
                # never forward another paid request in the same invocation.
                self.send_error(400, 'Task budget or usage accounting stop; no upstream request was sent')
                return
            started = time.monotonic()
            request_id = None
            finished = False
            seen_usage = None
            content_type = ''
            tracker = SSEUsageTracker()
            outcome = None
            lease = None
            client_gone = False
            finish_lock = threading.Lock()
            upstream_request_id = None

            def complete(status, raw_usage=None, error_kind=None):
                nonlocal finished
                with finish_lock:
                    if request_id is None or finished:
                        return
                    finished = True
                    try:
                        usage_ledger.finish(request_id, raw_usage, status=status,
                                            latency_ms=(time.monotonic() - started) * 1000,
                                            error_kind=error_kind,
                                            **({'upstream_request_id': upstream_request_id}
                                               if upstream_request_id else {}))
                    except Exception as error:
                        if diagnostics is not None:
                            diagnostics['usage_failure'] = str(error)[:300]
                        if budget_failure is not None:
                            budget_failure['message'] = ('Usage accounting failed; further paid requests are blocked: '
                                                         + str(error)[:300])
                            budget_failure['accounting'] = True
                            budget_failure.setdefault('snapshot', None)

            def track_response(response):
                nonlocal upstream_request_id
                closeout.track(lease, response)
                candidate_id = response.headers.get('x-request-id') if response.headers else None
                if candidate_id and not any(secret and secret in candidate_id for secret in (key, token)):
                    upstream_request_id = candidate_id

            try:
                if validate:
                    validate()
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 16 * 1024 * 1024:
                    self.send_error(413); return
                body = self.rfile.read(length)
                if bailian:
                    body = _bailian_request(body, binding)
                if policy:
                    body = policy.prepare(body)
                suffix = self.path.removeprefix('/v1')
                chat_request = None
                if local:
                    request = json.loads(body)
                    if request.get('model') != binding['model']:
                        raise ValueError('Local request changed the frozen model')
                    if binding['wire_api'] == 'chat':
                        from chat_adapter import prepare
                        chat_request, tool_types = prepare(request)
                        body, suffix = json.dumps(chat_request, ensure_ascii=False).encode(), '/chat/completions'
                    elif binding['reasoning_effort'] == 'none':
                        request.pop('reasoning', None)
                        body = json.dumps(request, ensure_ascii=False).encode()
                lease = closeout.begin(lambda: complete('failed', tracker.usage, 'closeout_timeout'))
                if usage_ledger is not None:
                    try:
                        request_id = usage_ledger.begin(context.get('phase', 'work'),
                                                        provider=context.get('provider'),
                                                        model=context.get('model'),
                                                        job=context.get('job'))
                    except Exception as error:
                        from usage import BudgetExceeded
                        if isinstance(error, BudgetExceeded):
                            if budget_failure is not None:
                                budget_failure['message'] = str(error)
                                budget_failure['snapshot'] = error.snapshot
                            self.send_error(400, 'Task budget exhausted; no upstream request was sent')
                            return
                        if budget_failure is not None:
                            budget_failure['message'] = ('Usage accounting failed before forwarding; '
                                                         'no upstream request was sent: ' + str(error)[:300])
                            budget_failure['accounting'] = True
                            budget_failure['snapshot'] = None
                        self.send_error(400, 'Usage accounting failed; no upstream request was sent')
                        return
                headers = {'Content-Type': 'application/json'}
                if key:
                    headers['Authorization'] = 'Bearer ' + key
                req = urllib.request.Request(endpoint.rstrip('/') + suffix, data=body, headers=headers)
                try:
                    response = opener.open(req, timeout=closeout.timeout(timeout))
                except urllib.error.HTTPError as error:
                    # Provider error bodies can still report the usage of the
                    # attempted request. Extract only bounded usage, never log
                    # or forward arbitrary provider diagnostics.
                    failed_usage = None
                    failed_type = ''
                    try:
                        with error:
                            track_response(error)
                            failed_type = error.headers.get('Content-Type', '') if error.headers else ''
                            failed_usage = usage_from_payload(error.read(2 * 1024 * 1024), failed_type)
                    except Exception:
                        failed_usage = None
                    complete('failed', failed_usage, 'http_' + str(error.code))
                    # Never return arbitrary provider diagnostics or redirect locations.
                    if policy and (error.code in (408, 429) or error.code >= 500):
                        policy.transport_error = 'Exam endpoint HTTP ' + str(error.code)
                    self.send_error(error.code, 'Model endpoint rejected request'); return
                with response:
                    track_response(response)
                    validated = None
                    content_type = response.headers.get('Content-Type', 'application/json')
                    if chat_request is not None:
                        from chat_adapter import convert, encode
                        raw = response.read(16 * 1024 * 1024 + 1)
                        if len(raw) > 16 * 1024 * 1024:
                            raise ValueError('Local response exceeds size limit')
                        try:
                            original = json.loads(raw)
                        except ValueError:
                            original = None
                        if isinstance(original, dict) and isinstance(original.get('usage'), dict):
                            seen_usage = original['usage']
                        converted = convert(original, request['model'], tool_types)
                        if converted['status'] != 'completed':
                            raise ValueError('Local Chat response is incomplete; no partial tool call was forwarded')
                        validated, content_type = encode(converted, request.get('stream', False))
                        if policy:
                            try:
                                policy.validate_response(validated, content_type)
                            except Exception:
                                complete('failed', seen_usage, 'protocol')
                                raise
                        complete('completed', seen_usage)
                    elif policy:
                        validated = response.read(policy.MAX_RESPONSE_BYTES + 1)
                        seen_usage = usage_from_payload(validated, content_type)
                        try:
                            policy.validate_response(validated, content_type)
                        except Exception:
                            complete('failed', seen_usage, 'protocol')
                            raise
                        complete('completed', seen_usage)
                    elif 'application/json' in content_type:
                        validated = response.read(16 * 1024 * 1024 + 1)
                        if len(validated) > 16 * 1024 * 1024:
                            raise ValueError('Model response exceeds size limit')
                        seen_usage = usage_from_payload(validated, content_type)
                        outcome = response_outcome(validated, content_type)
                    elif 'text/event-stream' not in content_type:
                        validated = response.read(16 * 1024 * 1024 + 1)
                        if len(validated) > 16 * 1024 * 1024:
                            raise ValueError('Model response exceeds size limit')
                        seen_usage = usage_from_payload(validated, content_type)
                        outcome = response_outcome(validated, content_type)
                    try:
                        self.send_response(response.status)
                        self.send_header('Content-Type', content_type)
                        self.send_header('Connection', 'close')
                        self.end_headers()
                    except OSError:
                        client_gone = True
                        closeout.stop('client_disconnected')
                    if validated is not None:
                        status, error_kind = _terminal('completed', outcome)
                        complete('failed' if client_gone else status, seen_usage,
                                 'client_disconnected' if client_gone else error_kind)
                        if not client_gone:
                            self.wfile.write(validated); self.wfile.flush()
                        return
                    while True:
                        read_timeout = closeout.timeout(timeout)
                        sock = closeout.response_socket(response)
                        if sock is not None:
                            sock.settimeout(read_timeout)
                        chunk = response.read1(65536)
                        if not chunk:
                            break
                        # Account before delivery: a broken downstream must not
                        # discard a terminal usage event that already arrived.
                        tracker.feed(chunk)
                        if tracker.outcome:
                            status, error_kind = _terminal('completed', tracker.outcome)
                            complete('failed' if client_gone else status, tracker.usage,
                                     'client_disconnected' if client_gone else error_kind)
                        if not client_gone:
                            try:
                                self.wfile.write(chunk); self.wfile.flush()
                            except OSError:
                                client_gone = True
                                closeout.stop('client_disconnected')
                    seen_usage, outcome = tracker.finish()
                    outcome = outcome or 'missing_terminal'
                    status, error_kind = _terminal('completed', outcome)
                    complete('failed' if client_gone else status, seen_usage,
                             'client_disconnected' if client_gone else error_kind)
            except Exception as error:
                if seen_usage is None:
                    seen_usage, _ = tracker.finish()
                complete('failed', seen_usage, 'protocol' if seen_usage is not None else 'transport')
                if diagnostics is not None:
                    detail = str(error).replace(token, '[REDACTED]')
                    diagnostics['failure'] = (detail.replace(key, '[REDACTED]') if key else detail)[:600]
                if policy:
                    # The private policy error is consumed by the bridge, not by
                    # a model; provider output cannot become an executable event.
                    policy.failure = str(error)
                    if isinstance(error, (OSError, TimeoutError, urllib.error.URLError)):
                        policy.transport_error = 'Exam endpoint transport failed'
                try:
                    self.send_error(400, 'Model protocol or frozen endpoint validation failed')
                except OSError:
                    pass
                self.close_connection = True
            finally:
                if lease is not None:
                    closeout.end(lease)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', token, server.server_port
    finally:
        closeout.drain()
        server.shutdown(); server.server_close(); worker.join(timeout=2)
