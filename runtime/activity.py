"""Private, append-only execution telemetry. Never used as acceptance evidence.

Only the bridge writes this file, outside the worker sandbox. Readers have no
connection to the executor's stdin. Exam content is never recorded. Work-phase
reasoning comes only from the optional relay observer, not CLI item events.
"""
from collections import deque
import contextlib
import fcntl
import json
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import threading
import time
import unicodedata
import uuid

FILENAME = 'activity.jsonl'
MAX_LINE = 4 * 1024 * 1024
MAX_TEXT = 32000
LOG_LOCK_TIMEOUT = 0.05
SENSITIVE = re.compile(
    r'(?i)(\b(?:authorization|api[_-]?key|access[_-]?token|password|secret|'
    r'telos_(?:relay|audit)_token)\b[\"\']?\s*[:=]\s*[\"\']?)([^\s\"\'&,;]+)')
BEARER = re.compile(r'(?i)\bBearer\s+[^\s\"\'&,;]+')
KEY = re.compile(r'\bsk-[A-Za-z0-9_-]{12,}')


def clean(value, secrets=()):
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, '[REDACTED]')
    text = BEARER.sub('Bearer [REDACTED]', text)
    text = SENSITIVE.sub(r'\1[REDACTED]', text)
    text = KEY.sub('[REDACTED]', text)
    # Untrusted commands/output must not control the observer terminal (OSC,
    # escape sequences, carriage-return overwrites, bidi controls).
    text = ''.join(c for c in text if c in '\n\t' or not unicodedata.category(c).startswith('C'))
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + '\n[truncated]'


class Recorder:
    def __init__(self, directory, session_id, job_id=None, phase='work', secrets=()):
        self.directory = Path(directory)
        self.session_id, self.job_id, self.phase = session_id, job_id, phase
        self.secrets = secrets
        self.failed = False

    def emit(self, kind, text='', **fields):
        """Logging failure cannot change a task's result or block its pipes."""
        if self.failed:
            return False
        record = {'version': 1, 'id': uuid.uuid4().hex, 'time': time.time(),
                  'session_id': self.session_id, 'job_id': self.job_id,
                  'phase': self.phase, 'kind': kind, 'text': clean(text, self.secrets)}
        record.update({k: clean(v, self.secrets) for k, v in fields.items()
                       if k in ('item_id', 'status', 'event_type') and v is not None})
        if type(fields.get('offset')) is int and fields['offset'] >= 0:
            record['offset'] = fields['offset']
        try:
            # The run directory already exists; never create arbitrary paths.
            fd = os.open(self.directory / FILENAME,
                         os.O_APPEND | os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'a+b', buffering=0) as stream:
                deadline = time.monotonic() + LOG_LOCK_TIMEOUT
                while True:
                    try:
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(.001)
                raw = (json.dumps(record, ensure_ascii=False) + '\n').encode()
                # Finish an interrupted prior line before appending a new one.
                # Each complete event is durable independently of the viewer.
                if stream.seek(0, os.SEEK_END):
                    stream.seek(-1, os.SEEK_END)
                    if stream.read(1) != b'\n':
                        stream.write(b'\n')
                view = memoryview(raw)
                while view:
                    view = view[stream.write(view):]
            return True
        except OSError:
            self.failed = True
            # Constant text only: never print private data to MCP stdout/stderr.
            try:
                (self.directory / 'activity-unavailable').touch(mode=0o600)
            except OSError:
                pass
            return False

    def event(self, event):
        if not isinstance(event, dict):
            return
        kind = event.get('type', '')
        if not isinstance(kind, str):
            return
        # A closed-book answer may contain recalled private question content.
        if self.phase == 'exam':
            if kind in ('turn.started', 'turn.completed', 'turn.failed', 'error'):
                self.emit('status', 'Exam ' + kind, event_type=kind)
            return
        if kind in ('thread.started', 'turn.started', 'turn.completed'):
            self.emit('status', kind, event_type=kind)
        elif kind in ('error', 'turn.failed'):
            self.emit('error', event.get('message') or event.get('error') or kind, event_type=kind)
        elif kind.startswith('item.') and isinstance(event.get('item'), dict):
            item = event['item']
            item_type = item.get('type', '')
            fields = {'item_id': item.get('id'), 'status': item.get('status'), 'event_type': kind}
            if item_type == 'agent_message':
                self.emit('message', item.get('text', ''), **fields)
            elif item_type == 'command_execution':
                if kind == 'item.started':
                    self.emit('tool', item.get('command', ''), **fields)
                else:
                    self.emit('command', str(item.get('command', '')) + '\n'
                              + str(item.get('aggregated_output', ''))
                              + '\nexit_code=' + str(item.get('exit_code')), **fields)
            elif item_type == 'file_change':
                changes = item.get('changes', [])
                text = '\n'.join(str(c.get('kind', 'change')) + ' ' + str(c.get('path', ''))
                                 for c in changes if isinstance(c, dict)) if isinstance(changes, list) else ''
                self.emit('files', text, **fields)
            elif item_type in ('mcp_tool_call', 'dynamic_tool_call', 'custom_tool_call', 'web_search'):
                # Arguments/results are worker-visible data, never the contract.
                parts = [str(item.get(k, '')) for k in
                         ('server', 'tool', 'name', 'query', 'arguments', 'result', 'error') if k in item]
                self.emit('tool', '\n'.join(parts), **fields)
            elif item_type == 'error':
                self.emit('error', item.get('message', ''), **fields)
            elif item_type == 'todo_list':
                self.emit('plan', json.dumps(item.get('items', []), ensure_ascii=False), **fields)


def for_state(b, state, phase='work', secrets=()):
    # Small bridge test doubles and legacy external users may omit STATE.
    if not hasattr(b, 'STATE'):
        return None
    return Recorder(b.run_dir(state['session_id']), state['session_id'],
                    state.get('job_id'), phase, secrets)


def status(b, state, text=None):
    recorder = for_state(b, state, 'lifecycle')
    if recorder:
        continuation = state.get('continuation', 'active')
        recorder.emit('status', text or f"{state['phase']} / {continuation}",
                      status=state['phase'])


def files(b, state):
    """Include shell-based edits, also after an interrupted work attempt."""
    recorder = for_state(b, state)
    if not recorder:
        return
    from preflight import git_executable
    try:
        executable = git_executable()
        if not executable:
            recorder.emit('diagnostic', 'Worktree change summary unavailable')
            return
        result = subprocess.run([executable, '-c', 'core.fsmonitor=false',
                                 '-c', 'core.hooksPath=/dev/null', 'status', '--short'],
                                cwd=state['worktree'], capture_output=True, text=True, timeout=2,
                                env={**b.minimal_env(), 'GIT_OPTIONAL_LOCKS': '0'})
        if result.returncode == 0 and result.stdout.strip():
            recorder.emit('files', result.stdout, status='worktree')
        elif result.returncode:
            recorder.emit('diagnostic', 'Worktree change summary unavailable')
    except (OSError, ValueError, subprocess.SubprocessError):
        recorder.emit('diagnostic', 'Worktree change summary unavailable')


class OutputObserver:
    """Bounded, disposable display writer; pipe readers never wait for disk I/O."""
    MAX_BYTES = 4 * 1024 * 1024

    def __init__(self, recorder):
        self.recorder = recorder
        self.queue = queue.Queue(maxsize=64)
        self.lock = threading.Lock()
        self.pending_bytes = 0
        self.stopping = threading.Event()
        self.gap = threading.Event()
        self.worker = threading.Thread(target=self.run, name='telos-output-observer', daemon=True)
        self.worker.start()

    def submit(self, method, args, size):
        if self.stopping.is_set():
            return
        with self.lock:
            # A stderr flood must not crowd out structured executor events.
            # Reserve half of both queue limits; pipe readers still never wait.
            diagnostic = method == 'emit'
            byte_limit = self.MAX_BYTES // 2 if diagnostic else self.MAX_BYTES
            if (size > byte_limit - self.pending_bytes or
                    diagnostic and self.queue.qsize() >= self.queue.maxsize // 2):
                self.gap.set()
                return
            try:
                self.queue.put_nowait((method, args, size))
            except queue.Full:
                self.gap.set()
            else:
                self.pending_bytes += size

    def run(self):
        try:
            while not self.stopping.is_set() or not self.queue.empty():
                try:
                    method, args, size = self.queue.get(timeout=.05)
                except queue.Empty:
                    continue
                try:
                    getattr(self.recorder, method)(*args)
                finally:
                    with self.lock:
                        self.pending_bytes -= size
                if self.gap.is_set():
                    self.gap.clear()
                    self.recorder.emit('diagnostic', 'Observer backlog: some display events omitted; execution continues.')
        except Exception:
            # Telemetry exceptions cannot stop lifecycle collection or the CLI.
            self.stopping.set()

    def close(self):
        self.stopping.set()
        self.worker.join(timeout=.2)


def communicate(process, payload, timeout, recorder):
    """Drain both pipes while running; retain only lifecycle JSON for the caller.

    No PTY, no viewer dependency, and no changes to stdin or process-group
    ownership. The writer also runs independently so a full stdin pipe cannot
    prevent the timeout from being enforced.
    """
    lifecycle, stderr_tail = deque(maxlen=128), bytearray()
    first_thread = []
    observer = OutputObserver(recorder)
    import select
    stopping = threading.Event()

    def diagnostic(text):
        observer.submit('emit', ('diagnostic', text), len(text.encode()))

    def line(raw, source):
        if source == 'stderr':
            if recorder.phase != 'exam':
                diagnostic(raw.decode(errors='replace'))
            return
        try:
            event = json.loads(raw)
        except (ValueError, UnicodeError):
            diagnostic('Unrecognized executor event (content omitted)')
            return
        if isinstance(event, dict) and event.get('type') in (
                'thread.started', 'turn.completed', 'error', 'turn.failed'):
            if event['type'] == 'thread.started' and not first_thread:
                first_thread.append(bytes(raw))
            else:
                lifecycle.append(bytes(raw))
        observer.submit('event', (event,), len(raw))

    def read(pipe, source):
        pending = bytearray()
        dropping = False
        try:
            fd = pipe.fileno()
            os.set_blocking(fd, False)
            while not stopping.is_set():
                if not select.select([fd], [], [], .1)[0]:
                    continue
                try:
                    chunk = os.read(fd, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    break
                if source == 'stderr':
                    stderr_tail.extend(chunk)
                    del stderr_tail[:-16000]
                pending.extend(chunk)
                while b'\n' in pending:
                    raw, _, rest = pending.partition(b'\n')
                    pending = bytearray(rest)
                    if not dropping and len(raw) <= MAX_LINE:
                        line(raw, source)
                    dropping = False
                if len(pending) > MAX_LINE:
                    pending.clear()
                    dropping = True
                    diagnostic('Oversized executor event omitted')
            if pending and not dropping:
                line(pending, source)
        except (OSError, ValueError):
            diagnostic('Executor output stream closed')
        finally:
            pipe.close()

    def write():
        try:
            fd, offset = process.stdin.fileno(), 0
            os.set_blocking(fd, False)
            while offset < len(payload) and not stopping.is_set():
                if not select.select([], [fd], [], .1)[1]:
                    continue
                try:
                    offset += os.write(fd, payload[offset:offset + 65536])
                except BlockingIOError:
                    continue
        except (OSError, ValueError):
            pass
        finally:
            with contextlib.suppress(OSError, ValueError):
                process.stdin.close()

    readers = [threading.Thread(target=read, args=(pipe, source), daemon=True)
               for pipe, source in ((process.stdout, 'stdout'), (process.stderr, 'stderr'))]
    for reader in readers:
        reader.start()
    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    expired = None
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        expired = error
        from processes import kill_process_tree, wait_killed
        kill_process_tree(process)
        wait_killed(process)
    finally:
        deadline = time.monotonic() + 2
        for reader in readers:
            reader.join(timeout=max(0, deadline - time.monotonic()))
        stopping.set()
        for thread in [*readers, writer]:
            thread.join(timeout=.2)
        observer.close()
    out, err = b'\n'.join([*first_thread, *lifecycle]), bytes(stderr_tail)
    if expired:
        expired.output, expired.stderr = out, err
        raise expired
    return out, err


class Reader:
    """An independent byte cursor. A partial final line is retried next time."""
    def __init__(self, path):
        self.path = Path(path)
        self.offset = 0
        self.identity = None
        self.dropping = False

    def seek_tail(self, count):
        """Find recent complete lines without scanning a long history forward."""
        self.dropping = False
        try:
            with self.path.open('rb') as stream:
                st = os.fstat(stream.fileno())
                self.identity = (st.st_dev, st.st_ino)
                position, needed = st.st_size, count + 1
                self.offset = 0
                while position and needed > 0:
                    size = min(position, 65536)
                    position -= size
                    stream.seek(position)
                    chunk = stream.read(size)
                    found = chunk.count(b'\n')
                    if found < needed:
                        needed -= found
                        continue
                    # The chunk holds enough newlines: find the needed-th one from
                    # the end in one C-level split, staying within this bound.
                    prefix = chunk.rsplit(b'\n', needed)[0]
                    self.offset = position + len(prefix) + 1
                    return
        except FileNotFoundError:
            pass

    def read(self):
        try:
            with self.path.open('rb') as stream:
                st = os.fstat(stream.fileno())
                identity = (st.st_dev, st.st_ino)
                if self.identity != identity or st.st_size < self.offset:
                    self.offset = 0
                    self.dropping = False
                self.identity = identity
                stream.seek(self.offset)
                while raw := stream.readline(MAX_LINE + 1):
                    if self.dropping:
                        self.offset = stream.tell()
                        self.dropping = not raw.endswith(b'\n')
                        continue
                    if not raw.endswith(b'\n'):
                        if len(raw) <= MAX_LINE:
                            break
                        # Keep this state across EOF polls until the same line
                        # ends. Its appended tail is never a separate record.
                        self.dropping = True
                        self.offset = stream.tell()
                        continue
                    self.offset = stream.tell()
                    if len(raw) > MAX_LINE:
                        continue
                    try:
                        value = json.loads(raw)
                    except (ValueError, UnicodeError):
                        continue
                    if isinstance(value, dict) and value.get('version') == 1:
                        yield value
        except FileNotFoundError:
            return
