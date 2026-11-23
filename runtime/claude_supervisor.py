"""Claude Code as the original supervisor: identity, model policy and wake-up.

Codex identifies the supervisor with MCP ``threadId`` metadata and receives
callbacks through its own queue. Claude Code has neither, so the Telos plugin
for Claude supplies three hooks, all running the local ``telos claude-hook``:

* PreToolUse (Telos tools only) writes a one-use attestation per tool call:
  the Claude process, session, effort, current model and whether a subagent
  is calling. Subagent calls are denied there and again in the MCP server.
* SessionStart / PostModelSwitch record the session's current model.
* SessionStart and Stop start a background waiter (``asyncRewake``). When an
  event for this supervisor becomes pending it is marked delivered and the
  waiter exits 2, which wakes Claude with the event text as a notification.

The supervisor ID is the session ID that Claude Code gives the MCP server in
``CLAUDE_CODE_SESSION_ID`` when it starts it; the model cannot change it. The
same Claude process keeps that ID across ``/clear``; ``claude --resume <id>``
restores it in a new process.
"""
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

from processes import process_start
from providers import ProviderError as GateError

TRANSPORT = 'claude_hook'
DEFAULT_POLICY = {'models': ['claude-opus-5-5'], 'efforts': ['xhigh', 'max']}
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
TOOL_USE = re.compile(r'toolu_[A-Za-z0-9_-]{8,128}')
TOOL_MATCHER = 'mcp__(plugin_telos_)?telos__.*'
ATTESTATION_WAIT = 1.5
HOOK_TIMEOUT = 7 * 86400
WAIT_SECONDS = HOOK_TIMEOUT - 3600
MAX_DELIVERIES = 3
MARKETPLACE = 'telos-local'
PLUGIN = 'telos'
LAUNCH_MODEL = 'opus'
HEADER = ('Telos callback for this supervisor (automatic notification; not an error and not user input). '
          'Handle each event below in order.')
# Per tools/call context. The MCP server handles one request at a time.
CURRENT = {}


def root(b):
    return Path(b.STATE) / 'claude'


def policy(cfg):
    """The allowed Claude supervisor models/efforts, or None when disabled."""
    value = cfg.get('claude_supervisor', DEFAULT_POLICY)
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) - {'models', 'efforts'}
            or not isinstance(value.get('models'), list) or not value['models']
            or not all(isinstance(m, str) and m.startswith('claude-') for m in value['models'])
            or not isinstance(value.get('efforts'), list) or not value['efforts']
            or not all(e in EFFORTS for e in value['efforts'])):
        raise GateError('claude_supervisor needs models (claude-* IDs) and efforts (' + '/'.join(EFFORTS) + '), or null')
    return value


def normalize_model(value):
    """claude-opus-5-5[1m] and claude-opus-5-5 are the same supervisor model."""
    return re.sub(r'\[[^\]]*\]$', '', value.strip().lower()) if isinstance(value, str) else None


def session_id(value):
    try:
        return value if isinstance(value, str) and str(uuid.UUID(value)) == value else None
    except ValueError:
        return None


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def _read(path):
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


# MCP side ------------------------------------------------------------------

def begin_call(b, meta, environ=None):
    """Return the trusted supervisor ID for a Claude Code tools/call, else None."""
    CURRENT.clear()
    if not isinstance(meta, dict) or 'claudecode/toolUseId' not in meta:
        return None
    environ = os.environ if environ is None else environ
    session = session_id(environ.get('CLAUDE_CODE_SESSION_ID'))
    if session is None:
        return None
    tool_use = meta.get('claudecode/toolUseId')
    attestation = take_attestation(b, tool_use, session) if isinstance(tool_use, str) else None
    if attestation and attestation.get('agent_id'):
        raise GateError('Telos supervision stays in the main Claude conversation; subagents cannot call Telos tools',
                        code='subagent_not_supervisor', next_action='call_from_main_conversation')
    CURRENT.update(session=session, tool_use_id=tool_use, attestation=attestation, claude_pid=os.getppid(),
                   cwd=environ.get('CLAUDE_PROJECT_DIR') or os.getcwd())
    return session


def take_attestation(b, tool_use, session, wait=ATTESTATION_WAIT):
    if not TOOL_USE.fullmatch(tool_use):
        return None
    path = root(b) / 'calls' / (tool_use + '.json')
    deadline = time.monotonic() + wait
    data = _read(path)
    while data is None and time.monotonic() < deadline:
        time.sleep(0.02)
        data = _read(path)
    if data is None:
        return None
    path.unlink(missing_ok=True)
    # Written by a hook of this same Claude process (or session), for this call.
    if data.get('tool_use_id') != tool_use or (
            data.get('claude_pid') != os.getppid() and data.get('env_session') != session):
        return None
    return data


def is_current(thread_id):
    return bool(CURRENT.get('session')) and CURRENT.get('session') == thread_id


def check(b):
    """Enforce the configured Claude supervisor model/effort for the current call."""
    selected = policy(b.config())
    if selected is None:
        raise GateError('Claude Code supervision is disabled (claude_supervisor is null)',
                        code='claude_supervisor_disabled', next_action='use_configured_supervisor')
    attestation = CURRENT.get('attestation')
    if not attestation:
        raise GateError('Telos Claude hooks are not active in this Claude session. Run `telos claude install`, '
                        'then restart Claude Code; notify=false permits manual operation.',
                        code='claude_hooks_missing', next_action='install_claude_plugin')
    model, effort = normalize_model(attestation.get('model')), attestation.get('effort')
    if model not in {normalize_model(m) for m in selected['models']} or effort not in selected['efforts']:
        raise GateError(f'The Claude supervisor must use {"/".join(selected["models"])} with effort '
                        f'{"/".join(selected["efforts"])}; this session uses {model or "an unknown model"} / '
                        f'{effort or "unknown effort"}. Start it with `telos claude run`, or switch with /model and /effort.',
                        code='supervisor_model_not_allowed', next_action='switch_supervisor_model')
    return model, effort


def bind(b, thread_id):
    if not is_current(thread_id):
        raise GateError('Automatic return requires the calling Claude session')
    model, effort = check(b)
    pid = CURRENT['claude_pid']
    return {'thread_id': thread_id, 'transport': TRANSPORT, 'claude_pid': pid, 'claude_start': process_start(pid),
            'cwd': CURRENT['cwd'], 'model': model, 'effort': effort}


def verify(b, owner):
    if not is_current(owner.get('thread_id')):
        raise GateError('This action belongs to the original Claude supervisor session',
                        code='owner_mismatch', next_action='use_original_supervisor')
    check(b)


def listening(b, owner):
    """Whether a waiter of the owner's Claude process is currently waiting."""
    folder = root(b) / 'waiters'
    for record_path in folder.glob('*.json'):
        record = _read(record_path) or {}
        if not owns(owner, record.get('claude_pid'), record.get('claude_start'), set(record.get('sessions') or [])):
            continue
        try:
            with record_path.with_suffix('.lock').open('a+') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            continue
    return False


def owns(owner, claude_pid, claude_start, sessions):
    return (isinstance(owner, dict) and owner.get('transport') == TRANSPORT
            and (owner.get('thread_id') in sessions
                 or (claude_pid is not None and owner.get('claude_pid') == claude_pid
                     and owner.get('claude_start') == claude_start)))


# Hook side -----------------------------------------------------------------

def hook_main(b, stdin=None, environ=None, out=None, err=None):
    """Entry point for `telos claude-hook`. Never fails the user's Claude turn."""
    stdin, out, err = stdin or sys.stdin, out or sys.stdout, err or sys.stderr
    environ = os.environ if environ is None else environ
    try:
        data = json.loads(stdin.read() or '{}')
    except ValueError:
        return 0
    if not isinstance(data, dict):
        return 0
    event = data.get('hook_event_name')
    try:
        if event == 'PreToolUse':
            return pre_tool_use(b, data, environ, out)
        if event in ('SessionStart', 'PostModelSwitch'):
            record_model(b, data)
        if event in ('SessionStart', 'Stop'):
            return wait(b, data, environ, err)
    except Exception as error:  # noqa: BLE001 - a hook problem must not break Claude
        try:
            _write(root(b) / 'hook-error.json', {'at': time.time(), 'event': event, 'error': type(error).__name__})
        except OSError:
            pass
    return 0


def record_model(b, data):
    model = data.get('to_model') if data.get('hook_event_name') == 'PostModelSwitch' else data.get('model')
    if not isinstance(model, str) or not model:
        return
    pid = os.getppid()
    _write(root(b) / 'sessions' / f'{pid}.json',
           {'claude_pid': pid, 'claude_start': process_start(pid), 'model': model, 'at': time.time(),
            'session_id': session_id(data.get('session_id'))})


def current_model(b, data):
    pid = os.getppid()
    record = _read(root(b) / 'sessions' / f'{pid}.json') or {}
    if record.get('model') and record.get('claude_start') == process_start(pid):
        return record['model']
    return transcript_model(data.get('transcript_path'))


def transcript_model(path):
    """Last model that answered in this transcript (fallback when no hook recorded it)."""
    if not isinstance(path, str) or not path.endswith('.jsonl'):
        return None
    try:
        with open(path, 'rb') as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - (512 << 10)))
            lines = stream.read().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        model = (entry.get('message') or {}).get('model') if entry.get('type') == 'assistant' else None
        if isinstance(model, str) and model.startswith('claude-'):
            return model
    return None


def pre_tool_use(b, data, environ, out):
    if data.get('agent_id'):
        out.write(json.dumps({'hookSpecificOutput': {
            'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
            'permissionDecisionReason': 'Telos supervision stays in the main conversation; '
                                        'subagents cannot call Telos tools. Return to the main conversation.'}}))
        out.flush()
        return 0
    tool_use = data.get('tool_use_id')
    if not isinstance(tool_use, str) or not TOOL_USE.fullmatch(tool_use):
        return 0
    effort = data.get('effort')
    folder = root(b) / 'calls'
    _write(folder / (tool_use + '.json'), {
        'tool_use_id': tool_use, 'claude_pid': os.getppid(), 'session_id': session_id(data.get('session_id')),
        'env_session': session_id(environ.get('CLAUDE_CODE_SESSION_ID')), 'agent_id': data.get('agent_id'),
        'effort': effort.get('level') if isinstance(effort, dict) else None,
        'model': current_model(b, data), 'tool_name': data.get('tool_name'), 'at': time.time()})
    _prune(folder)
    return 0


def _prune(folder, keep_seconds=3600, threshold=200):
    try:
        entries = list(folder.iterdir())
        if len(entries) < threshold:
            return
        cutoff = time.time() - keep_seconds
        for entry in entries:
            if entry.stat().st_mtime < cutoff:
                entry.unlink(missing_ok=True)
    except OSError:
        pass


def wait(b, data, environ, err, *, poll=2.0, limit=WAIT_SECONDS):
    """Background waiter: exit 2 with pending events for this Claude, else 0."""
    claude_pid = os.getppid()
    claude_start = process_start(claude_pid)
    sessions = {s for s in (session_id(data.get('session_id')), session_id(environ.get('CLAUDE_CODE_SESSION_ID'))) if s}
    folder = root(b) / 'waiters'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (folder / f'{claude_pid}.lock').open('a+')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return 0  # This Claude already has a waiter.
    try:
        _write(folder / f'{claude_pid}.json', {'claude_pid': claude_pid, 'claude_start': claude_start,
                                                'sessions': sorted(sessions), 'pid': os.getpid(), 'started_at': time.time()})

        def match(owner):
            return owns(owner, claude_pid, claude_start, sessions)
        # A new turn boundary or a (re)started session: re-offer events that were
        # delivered earlier but never received (the notification may be lost).
        redeliver = data.get('hook_event_name') in ('Stop', 'SessionStart')
        seen = {}
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            if os.getppid() != claude_pid:
                return 0  # Claude exited.
            messages = scan(b, match, seen, redeliver)
            redeliver = False
            if messages:
                err.write(HEADER + '\n\n' + '\n\n'.join(messages) + '\n')
                err.flush()
                return 2
            time.sleep(poll)
        return 0
    finally:
        lock.close()


def scan(b, match, seen, redeliver):
    messages = []
    for path in sorted((Path(b.STATE) / 'runs').glob('*/state.json')):
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            continue
        if seen.get(path) == mtime and not redeliver:
            continue
        state = _read(path)
        seen[path] = mtime
        if not state or not match(state.get('owner')) or state.get('continuation', 'active') != 'active':
            continue
        record = b.delivery_event(state)
        if not record or not (record.get('status') == 'pending' or redeliver and record.get('status') == 'delivered'):
            continue
        message = claim(b, path.parent.name, match, redeliver)
        if message:
            messages.append(message)
    return messages


def claim(b, sid, match, redeliver):
    with b.locked(sid) as run:
        state, _ = b.load_session(run)
        if not match(state.get('owner')) or state.get('continuation', 'active') != 'active':
            return None
        record = b.delivery_event(state)
        if not record:
            return None
        status, attempts = record.get('status'), record.get('attempts', 0)
        if status == 'delivered' and redeliver and attempts >= MAX_DELIVERIES:
            record.update(status='unacknowledged',
                          last_error='Claude was woken but did not call telos_receive; receive this event manually')
            b.write_json(run / 'state.json', state)
            return None
        if not (status == 'pending' or status == 'delivered' and redeliver):
            return None
        record.update(status='delivered', delivered_at=time.time(), attempts=attempts + 1,
                      last_error=None, transport=TRANSPORT)
        b.write_json(run / 'state.json', state)
        return b.event_message(sid, record)


# Install / run -------------------------------------------------------------

def plugin_root(b):
    return Path(b.HOME) / '.local/share/telos-claude'


def _installed_skill(b):
    manifest = _read(Path(b.INSTALL) / 'install-manifest.json') or {}
    candidates = [(manifest.get('skill') or {}).get('path'), Path(b.HOME) / '.agents/skills/telos',
                  Path(b.INSTALL).parent.parent / 'skills/telos']
    for candidate in candidates:
        if candidate and (Path(candidate) / 'SKILL.md').is_file():
            return Path(candidate)
    raise GateError('Installed Telos skill not found; install Telos first')


def _launcher(b):
    manifest = _read(Path(b.INSTALL) / 'install-manifest.json') or {}
    launcher = manifest.get('launcher') or shutil.which('telos')
    if not launcher or not Path(launcher).is_file():
        raise GateError('Telos launcher not found; install Telos first')
    return str(Path(launcher).resolve())


def build_plugin(b, launcher=None, skill=None):
    """Write the local marketplace and plugin; returns the marketplace root."""
    launcher = launcher or _launcher(b)
    skill = Path(skill) if skill else _installed_skill(b)
    base = plugin_root(b)
    plugin = base / 'plugin'
    if plugin.exists():
        shutil.rmtree(plugin)
    (plugin / '.claude-plugin').mkdir(parents=True)
    (base / '.claude-plugin').mkdir(parents=True, exist_ok=True)
    description = 'Telos: Claude Code supervises Codex executors with frozen checks and closed-book acceptance'
    (base / '.claude-plugin' / 'marketplace.json').write_text(json.dumps({
        'name': MARKETPLACE, 'owner': {'name': 'Telos (local)'}, 'description': 'Local Telos installation',
        'plugins': [{'name': PLUGIN, 'source': './plugin', 'description': description}]}, indent=2) + '\n')
    (plugin / '.claude-plugin' / 'plugin.json').write_text(json.dumps({
        'name': PLUGIN, 'version': b.VERSION, 'description': description}, indent=2) + '\n')
    (plugin / '.mcp.json').write_text(json.dumps({'mcpServers': {'telos': {
        'command': launcher, 'args': ['serve']}}}, indent=2) + '\n')
    waiter = {'type': 'command', 'command': launcher, 'args': ['claude-hook'], 'asyncRewake': True,
              'timeout': HOOK_TIMEOUT}
    quick = {'type': 'command', 'command': launcher, 'args': ['claude-hook'], 'timeout': 15}
    (plugin / 'hooks').mkdir()
    (plugin / 'hooks' / 'hooks.json').write_text(json.dumps({'hooks': {
        'SessionStart': [{'hooks': [waiter]}], 'Stop': [{'hooks': [waiter]}],
        'PostModelSwitch': [{'hooks': [quick]}],
        'PreToolUse': [{'matcher': TOOL_MATCHER, 'hooks': [quick]}]}}, indent=2) + '\n')
    shutil.copytree(skill, plugin / 'skills' / 'telos', ignore=shutil.ignore_patterns('.telos-skill.json'))
    return base


def _claude(argv, claude):
    result = subprocess.run([claude, *argv], capture_output=True, text=True, timeout=180)
    return result.returncode, (result.stdout + result.stderr).strip()


def install(b, claude=None, launcher=None, skill=None):
    claude = claude or shutil.which('claude')
    base = build_plugin(b, launcher, skill)
    report = {'marketplace': str(base), 'plugin': f'{PLUGIN}@{MARKETPLACE}', 'steps': []}
    if not claude:
        report['next'] = (f'claude not found. In Claude Code run: /plugin marketplace add {base} then '
                          f'/plugin install {PLUGIN}@{MARKETPLACE}')
        return report
    code, text = _claude(['plugin', 'marketplace', 'list', '--json'], claude)
    known = code == 0 and MARKETPLACE in text
    steps = ([['plugin', 'marketplace', 'update', MARKETPLACE]] if known
             else [['plugin', 'marketplace', 'add', str(base)]])
    code, text = _claude(['plugin', 'list', '--json'], claude)
    installed = code == 0 and f'{PLUGIN}@{MARKETPLACE}' in text
    steps.append(['plugin', 'update', f'{PLUGIN}@{MARKETPLACE}'] if installed
                 else ['plugin', 'install', f'{PLUGIN}@{MARKETPLACE}', '--scope', 'user'])
    for argv in steps:
        code, text = _claude(argv, claude)
        report['steps'].append({'command': 'claude ' + ' '.join(argv), 'returncode': code, 'output': text[-600:]})
        if code != 0:
            raise GateError('Claude plugin step failed: claude ' + ' '.join(argv) + '\n' + text[-600:])
    report['next'] = 'Restart Claude Code (or run /reload-plugins), then start it with `telos claude run`.'
    return report


def run_args(cfg, args):
    selected = policy(cfg) or DEFAULT_POLICY
    argv = ['claude']
    if not any(a == '--model' or a.startswith('--model=') for a in args):
        argv += ['--model', LAUNCH_MODEL if selected['models'][0].startswith('claude-opus') else selected['models'][0]]
    if not any(a == '--effort' or a.startswith('--effort=') for a in args):
        argv += ['--effort', selected['efforts'][0]]
    return argv + list(args)


def status(b):
    waiters = []
    for record_path in sorted((root(b) / 'waiters').glob('*.json')):
        record = _read(record_path) or {}
        alive = record.get('claude_start') is not None and process_start(record.get('claude_pid')) == record['claude_start']
        waiters.append({**{k: record.get(k) for k in ('claude_pid', 'sessions', 'started_at')}, 'claude_running': alive})
    hooks = plugin_root(b) / 'plugin' / 'hooks' / 'hooks.json'
    return {'policy': policy(b.config()), 'plugin_built': hooks.is_file(), 'marketplace': str(plugin_root(b)),
            'waiters': waiters, 'start': ' '.join(run_args(b.config(), []))}


def cli(b, argv):
    command = argv[0] if argv else 'status'
    if command == 'install':
        print(json.dumps(install(b), ensure_ascii=False, indent=2))
    elif command == 'status':
        print(json.dumps(status(b), ensure_ascii=False, indent=2))
    elif command == 'run':
        args = run_args(b.config(), argv[1:])
        claude = shutil.which('claude')
        if not claude:
            raise GateError('claude not found on PATH')
        os.execv(claude, args)
    else:
        raise GateError('Usage: telos claude install | status | run [claude arguments]')
    return 0
