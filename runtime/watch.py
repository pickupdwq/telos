"""Read-only terminal viewer and optional tmux side panel. Standard library only."""
import argparse
from collections import deque
import contextlib
import curses
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import textwrap
import time

import activity
from processes import process_running

PANE = re.compile(r'%[0-9]+\Z')
GROUP = re.compile(r'[0-9a-f]{24}\Z')
SESSION = re.compile(r'[0-9a-f]{32}\Z')


def tmux(origin, *args):
    executable = shutil.which('tmux')
    if not executable:
        raise OSError('tmux is not installed')
    target = ['-S', origin['socket']] if origin else []
    return subprocess.run([executable, *target, *args], check=True,
                          capture_output=True, text=True, timeout=3).stdout.strip()


def pane_context(state_dir, socket, pane):
    if not socket.startswith('/') or not PANE.fullmatch(pane):
        return None
    context = {'socket': socket, 'pane': pane}
    try:
        window = tmux(context, 'display-message', '-p', '-t', pane, '#{window_id}')
        if not re.fullmatch(r'@[0-9]+', window):
            return None
        context['window'] = window
        context['group'] = hashlib.sha256(
            (str(Path(state_dir).resolve()) + '\0' + socket + '\0' + window).encode()).hexdigest()[:24]
        return context
    except (OSError, subprocess.SubprocessError):
        return None


def codex_ancestors():
    """Read executable names only; never inspect process arguments or secrets."""
    result = subprocess.run(['/bin/ps', '-axo', 'pid=,ppid=,comm='], check=True,
                            capture_output=True, text=True, timeout=3)
    parents, codex = {}, []
    for line in result.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) != 3 or not all(v.isdigit() for v in fields[:2]):
            continue
        pid, parent = map(int, fields[:2])
        parents[pid] = parent
        if Path(fields[2]).name == 'codex':
            codex.append(pid)
    ancestors = set()
    for pid in codex:
        seen = set()
        while pid > 1 and pid not in seen:
            ancestors.add(pid)
            seen.add(pid)
            pid = parents.get(pid, 0)
    return ancestors


def discover_origin(state_dir, cwd):
    """Recover a unique local CLI pane when a shared daemon lost tmux env."""
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        return None
    try:
        directory = Path(cwd).resolve()
        rows = tmux(None, 'list-panes', '-a', '-F',
                    '#{socket_path}\t#{pane_id}\t#{pane_pid}\t#{session_attached}\t'
                    '#{pane_dead}\t#{@telos_watch_group}\t#{pane_current_path}')
        candidates = set()
        ancestors = codex_ancestors()
        for row in rows.splitlines():
            fields = row.split('\t')
            if len(fields) != 7:
                continue
            socket, pane, pid, attached, dead, group, path = fields
            if (not socket.startswith('/') or not PANE.fullmatch(pane) or
                    not pid.isdigit() or int(pid) not in ancestors or
                    not attached.isdigit() or int(attached) < 1 or dead != '0' or
                    group or not Path(path).is_absolute() or Path(path).resolve() != directory):
                continue
            candidates.add((socket, pane))
        if len(candidates) == 1:
            socket, pane = candidates.pop()
            return pane_context(state_dir, socket, pane)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        pass
    return None


def origin(state_dir, environ=None, *, cwd=None):
    environ = os.environ if environ is None else environ
    value = environ.get('TMUX', '')
    pane = environ.get('TMUX_PANE', '')
    # Explicit, even stale or partial, pane bindings must never target another pane.
    if value or pane:
        return pane_context(state_dir, value.rsplit(',', 2)[0], pane)
    return discover_origin(state_dir, cwd) if cwd else None


def prepare(b, state):
    """Called in the supervisor's MCP process, never in the sandboxed executor."""
    owner = state.get('owner') or {}
    # Use the trusted caller directory, not the executor's repository/worktree.
    cwd = owner.get('cwd') if owner.get('transport') == 'cli_queue' else None
    context = origin(b.STATE, cwd=cwd)
    if context:
        state['watch_origin'] = context


def panel(b, context, *, keep_open=False):
    if not context:
        return {'status': 'manual', 'command': 'telos watch'}
    group = context['group']
    if not GROUP.fullmatch(group):
        raise ValueError('Invalid observer group')
    directory = b.STATE / 'watch'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / (group + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        panes = tmux(context, 'list-panes', '-t', context['window'], '-F',
                     '#{pane_id}\t#{@telos_watch_group}\t#{pane_dead}')
        for row in panes.splitlines():
            fields = row.split('\t')
            if len(fields) == 3 and fields[1] == group:
                if fields[2] == '0':
                    return {'status': 'reused', 'pane_id': fields[0]}
                command = [sys.executable, str(b.INSTALL / 'telos.py'),
                           'watch', '--ui', '--group', group]
                if keep_open:
                    command.append('--keep-open')
                tmux(context, 'respawn-pane', '-t', fields[0],
                     '-e', 'TELOS_STATE_DIR=' + str(b.STATE),
                     '-e', 'TELOS_CACHE_DIR=' + str(b.CACHE), *command)
                return {'status': 'reopened', 'pane_id': fields[0]}
        # tmux executes multiple argv directly. No shell interpolation of paths.
        command = [sys.executable, str(b.INSTALL / 'telos.py'), 'watch', '--ui', '--group', group]
        if keep_open:
            command.append('--keep-open')
        pane = tmux(context, 'split-window', '-d', '-h', '-p', '40', '-t', context['pane'],
                    '-e', 'TELOS_STATE_DIR=' + str(b.STATE),
                    '-e', 'TELOS_CACHE_DIR=' + str(b.CACHE), '-P', '-F', '#{pane_id}', *command)
        if not PANE.fullmatch(pane):
            raise OSError('tmux did not return a pane ID')
        try:
            tmux(context, 'set-option', '-p', '-t', pane, '@telos_watch_group', group)
        except (OSError, subprocess.SubprocessError):
            # Do not leave an untracked split that future tasks cannot reuse.
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                tmux(context, 'kill-pane', '-t', pane)
            raise
        return {'status': 'opened', 'pane_id': pane}


def after_start(b, state):
    try:
        config = b.config().get('watch', {})
        if isinstance(config, dict) and config.get('auto_tmux', True) is False:
            return
        panel(b, state.get('watch_origin'))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        recorder = activity.for_state(b, state, 'lifecycle')
        if recorder:
            recorder.emit('diagnostic', 'Observer panel unavailable; use telos watch in another terminal.')


def tasks(state_dir, group=None, project=None):
    result = []
    for path in (Path(state_dir) / 'runs').glob('*/state.json'):
        try:
            value = json.loads(path.read_text())
            if value.get('notification_only'):
                continue
            if not re.fullmatch(r'[a-f0-9]{32}', str(value.get('session_id', ''))) or \
                    value['session_id'] != path.parent.name:
                continue
            if group and value.get('watch_origin', {}).get('group') != group:
                continue
            if project and value.get('project_id') != project:
                continue
            result.append({k: value.get(k) for k in (
                'session_id', 'model', 'repo', 'phase', 'continuation', 'created_at', 'project_id')})
        except (OSError, ValueError, AttributeError):
            continue
    return sorted(result, key=lambda item: (item.get('created_at') or 0, item['session_id']))


def group_finished(state_dir, group):
    """Read public lifecycle state only; never inspect contracts or review packets."""
    try:
        states = {}
        for path in (Path(state_dir) / 'runs').glob('*/state.json'):
            value = json.loads(path.read_text())
            sid = value.get('session_id')
            if not isinstance(sid, str) or not SESSION.fullmatch(sid) or sid != path.parent.name:
                return False
            states[sid] = value
        selected = [s for s in states.values() if not s.get('notification_only')
                    and (s.get('watch_origin') or {}).get('group') == group]
        if not selected:
            return False
        projects = {s['project_id'] for s in selected if s.get('project_id')}
        for pid in projects:
            courier = states.get(pid, {})
            # The courier marks final project acceptance without exposing the
            # private project contract. Pending/dependent children keep it open.
            if (not courier.get('notification_only') or courier.get('project_id') != pid or
                    not (courier.get('return_complete') is True or
                         courier.get('continuation') == 'cancelled')):
                return False
        # A cancelled project's workers may still be finishing a provider call,
        # including workers that were started from another tmux window.
        related = [s for s in states.values() if not s.get('notification_only')
                   and (s in selected or s.get('project_id') in projects)]
        for state in related:
            if state.get('phase') != 'accepted' and state.get('continuation') != 'cancelled':
                return False
            pid = state.get('job_pid')
            if pid is not None:
                if type(pid) is not int or pid <= 0 or process_running(pid, state.get('job_process_start')):
                    return False
            elif state.get('phase') == 'running':
                return False
        return True
    except (OSError, ValueError, TypeError, AttributeError):
        # Missing/unreadable lifecycle evidence must never look like completion.
        return False


def close_finished_panel(b, group):
    """Close only this managed viewer, serialized with new task launches."""
    if not isinstance(group, str) or not GROUP.fullmatch(group):
        return False
    try:
        # Dispatch takes these locks in the same order. Remove the pane while
        # holding them so a new launch cannot reuse a viewer that is exiting.
        with (b.STATE / 'coordinator.lock').open('a+') as coordinator, \
                (b.STATE / 'watch' / (group + '.lock')).open('a+') as observer:
            fcntl.flock(coordinator, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(observer, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not group_finished(b.STATE, group):
                return False
            context = origin(b.STATE)
            if not context or context['group'] != group:
                return False
            owner = tmux(context, 'display-message', '-p', '-t', context['pane'],
                         '#{@telos_watch_group}\t#{pane_pid}')
            if owner != f'{group}\t{os.getpid()}':
                return False
            # Explicit removal also handles tmux's remain-on-exit option.
            # Never target the originating supervisor pane or stop an executor.
            tmux(context, 'kill-pane', '-t', context['pane'])
            return True
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return False


def resolve_task(rows, requested=None):
    if not requested:
        return rows[-1]['session_id'] if rows else None
    matched = [r['session_id'] for r in rows if r['session_id'] == requested or
               len(requested) >= 8 and r['session_id'].startswith(requested)]
    if len(matched) != 1:
        raise ValueError('Task ID not found or ambiguous; use telos watch --list')
    return matched[0]


def render(event):
    stamp = time.strftime('%H:%M:%S', time.localtime(event.get('time', 0)))
    tag = activity.clean(event.get('kind', 'event'))
    phase = activity.clean(event.get('phase', 'work'))
    status = activity.clean(event.get('status') or event.get('event_type') or '')
    return f'[{stamp}] {tag} ({phase}) {status}\n' + activity.clean(event.get('text', ''))


def stream(b, sid, *, follow=True, history=False, tail=100, json_output=False, auto_close_group=None):
    reader = activity.Reader(b.run_dir(sid) / activity.FILENAME)
    if not history:
        reader.seek_tail(tail)
    def output(event):
        if json_output:
            print(json.dumps(event, ensure_ascii=False), flush=True)
        else:
            print(render(event), flush=True)
    events = reader.read()
    for event in events if history else deque(events, maxlen=tail):
        output(event)
    if not json_output:
        print(f'Watching {sid}; Ctrl-C exits the viewer only.' if follow else
              f'End of recorded history for {sid}.', flush=True)
        if (b.run_dir(sid) / 'activity-unavailable').exists():
            print('Recording unavailable (I/O error); task execution is unaffected.', flush=True)
    close_check_at = 0
    while follow:
        time.sleep(0.2)
        for event in reader.read():
            output(event)
        if auto_close_group and time.monotonic() >= close_check_at:
            if close_finished_panel(b, auto_close_group):
                return
            close_check_at = time.monotonic() + 1


def ui(b, initial=None, group=None, project=None, tail=200, auto_close=False):
    def display(screen):
        with contextlib.suppress(curses.error):
            curses.curs_set(0)
        screen.timeout(200)
        sid, reader = initial, None
        events = deque(maxlen=2000)
        scroll, following = 0, True
        rows = []
        refresh_at = 0
        layout = None
        lines = []

        def put(y, text, style=0):
            h, w = screen.getmaxyx()
            if 0 <= y < h and w > 1:
                with contextlib.suppress(curses.error):
                    screen.addnstr(y, 0, activity.clean(text), w - 1, style)

        while True:
            if time.monotonic() >= refresh_at:
                rows = tasks(b.STATE, group, project)
                refresh_at = time.monotonic() + 1
                if auto_close and close_finished_panel(b, group):
                    return
            ids = [r['session_id'] for r in rows]
            if sid not in ids:
                sid = resolve_task(rows)
            if sid and (reader is None or reader.path.parent.name != sid):
                reader = activity.Reader(b.run_dir(sid) / activity.FILENAME)
                reader.seek_tail(min(tail, 2000))
                events = deque(reader.read(), maxlen=min(tail, 2000))
                events = deque(events, maxlen=2000)
                scroll, following = 0, True
                layout = None
            changed = list(reader.read()) if reader else []
            events.extend(changed)
            h, w = screen.getmaxyx()
            height = max(1, h - 5)
            if changed or layout != (h, w, sid):
                rendered = deque(maxlen=6000)
                for event in events:
                    for line in render(event).splitlines():
                        rendered.extend(textwrap.wrap(line, max(1, w - 2), replace_whitespace=False) or [''])
                lines = list(rendered)
                layout = (h, w, sid)
            if following:
                scroll = max(0, len(lines) - height)
            scroll = max(0, min(scroll, max(0, len(lines) - height)))
            screen.erase()
            put(0, 'TELOS | READ ONLY | ' + ('LIVE' if following else 'HISTORY'), curses.A_BOLD)
            if sid:
                row = rows[ids.index(sid)]
                put(1, f'{ids.index(sid)+1}/{len(ids)}  {sid}')
                put(2, f"{row['model']} | {row['phase']} / {row['continuation']}")
            else:
                put(1, 'Waiting for tasks in this window...')
            if not lines:
                put(3, 'No recorded events yet. Older tasks have no activity log.')
            for i, line in enumerate(lines[scroll:scroll + height]):
                put(3 + i, line)
            if sid and (b.run_dir(sid) / 'activity-unavailable').exists():
                put(h - 2, 'Recording unavailable (I/O error); task continues.', curses.A_BOLD)
            else:
                put(h - 2, 'Tab/n/p: task  arrows/PgUp/PgDn: history  g/G: top/live')
            put(h - 1, 'Space: follow/pause view   q: close viewer (task keeps running)')
            screen.refresh()
            key = screen.getch()
            if key in (ord('q'), 27, 3):
                return
            if key in (9, ord('n'), ord(']'), ord('p'), ord('[')) and ids:
                step = -1 if key in (ord('p'), ord('[')) else 1
                sid = ids[(ids.index(sid) + step) % len(ids)]
            elif key in (curses.KEY_UP, ord('k'), curses.KEY_PPAGE):
                following = False
                scroll -= height if key == curses.KEY_PPAGE else 1
            elif key in (curses.KEY_DOWN, ord('j'), curses.KEY_NPAGE):
                following = False
                scroll += height if key == curses.KEY_NPAGE else 1
            elif key == ord('g'):
                following, scroll = False, 0
            elif key == ord('G'):
                following = True
            elif key == ord(' '):
                following = not following
    curses.wrapper(display)


def main(b, argv):
    parser = argparse.ArgumentParser(prog='telos watch', description=__doc__)
    parser.add_argument('session_id', nargs='?', help='Task ID or unique prefix (at least 8 characters)')
    parser.add_argument('--list', action='store_true', help='List tasks and exit')
    parser.add_argument('--project', help='Filter tasks by project ID')
    parser.add_argument('--group', help=argparse.SUPPRESS)
    parser.add_argument('--keep-open', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--ui', action='store_true', help='Interactive task switching and history')
    parser.add_argument('--pane', action='store_true', help='Open/reuse the current tmux window observer')
    parser.add_argument('--follow', '-f', dest='follow', action='store_true', default=True)
    parser.add_argument('--no-follow', dest='follow', action='store_false', help='Print history and exit')
    parser.add_argument('--history', action='store_true', help='Print all recorded history (not just the tail)')
    parser.add_argument('--tail', type=int, default=200, help='Initial recent event count (default: 200)')
    parser.add_argument('--json', action='store_true', help='Output JSONL instead of the terminal UI')
    args = parser.parse_args(argv)
    if args.tail < 0 or args.tail > 10000:
        parser.error('--tail must be between 0 and 10000; use --history for all events')
    if args.group and not GROUP.fullmatch(args.group):
        parser.error('Invalid group')
    if args.keep_open and not (args.ui and args.group):
        parser.error('--keep-open is reserved for managed observer panes')
    if args.pane:
        if args.session_id or args.project or args.history or not args.follow or args.json or args.ui:
            parser.error('--pane opens the shared observer; use it without task/output options')
        context = origin(b.STATE)
        if not context:
            raise ValueError('No tmux pane detected; run telos watch --ui in another terminal')
        print(json.dumps(panel(b, context, keep_open=True)))
        return
    rows = tasks(b.STATE, args.group, args.project)
    if args.list:
        for row in rows:
            print(json.dumps(row, ensure_ascii=False) if args.json else
                  activity.clean(f"{row['session_id']}  {row['phase']}  {row['model']}  {row['repo']}"))
        return
    sid = resolve_task(rows, args.session_id)
    terminal = sys.stdin.isatty() and sys.stdout.isatty()
    interactive = args.ui or (terminal and args.follow and not args.json and not args.history)
    if args.ui and (not terminal or args.json or not args.follow or args.history):
        parser.error('--ui requires a terminal and cannot be combined with --json, --history or --no-follow')
    try:
        if interactive:
            try:
                ui(b, sid, args.group, args.project, args.tail,
                   auto_close=bool(args.group and not args.keep_open))
            except curses.error:
                print('Terminal UI unavailable; using read-only text output.', file=sys.stderr)
                if sid:
                    stream(b, sid, follow=args.follow, tail=args.tail,
                           auto_close_group=args.group if not args.keep_open else None)
        elif sid:
            stream(b, sid, follow=args.follow, history=args.history, tail=args.tail, json_output=args.json)
        else:
            print('No tasks found. Start a Telos task, then use telos watch.')
    except (KeyboardInterrupt, BrokenPipeError):
        return
