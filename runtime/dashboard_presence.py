"""Read-only discovery of Claude Code conversations connected to this Telos state."""
import json
import os
from pathlib import Path
import stat
import subprocess
import time

from claude_supervisor import session_id
from processes import process_info


def _record(directory, pid):
    if directory.is_symlink():
        return {}
    try:
        fd = os.open(directory / f'{pid}.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
                return {}
            value = json.loads(stream.read(16385))
        return value if isinstance(value, dict) and value.get('claude_pid') == pid else {}
    except (OSError, ValueError, RecursionError):
        return {}


def _probe(args):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            timeout=2, encoding='utf-8', errors='replace')
    if result.returncode not in (0, 1):
        raise OSError('process observation unavailable')
    return result.stdout


def _bridges(parents):
    # Only inspect children of conversations recorded by hooks in THIS state root.
    children = _probe(['/usr/bin/pgrep', '-P', ','.join(sorted(parents)), '-f',
                       r'(^|/)telos\.py[[:space:]]+serve$']).split()
    if not children:
        return []
    pairs = _probe(['/bin/ps', '-p', ','.join(children), '-o', 'ppid=,pid='])
    return [tuple(map(int, line.split())) for line in pairs.splitlines() if len(line.split()) == 2]


def _working_directories(pids):
    if not pids:
        return {}
    try:
        output = _probe(['/usr/sbin/lsof', '-n', '-w', '-a', '-p', ','.join(map(str, pids)),
                         '-d', 'cwd', '-Fpn'])
    except (OSError, subprocess.TimeoutExpired):
        return {}
    result, pid = {}, None
    for line in output.splitlines():
        if line.startswith('p') and line[1:].isdigit():
            pid = int(line[1:])
        elif pid is not None and line.startswith('n/'):
            result[pid] = Path(line[1:]).name
    return result


def _connected(root):
    sessions, waiters = root / 'sessions', root / 'waiters'
    if root.is_symlink():
        return []
    parents = {path.stem for folder in (sessions, waiters) if not folder.is_symlink()
               for path in folder.glob('*.json') if path.stem.isascii() and path.stem.isdecimal()
               and 0 < len(path.stem) <= 10}
    if not parents:
        return []
    live = {}
    for parent, child in _bridges(parents):
        if str(parent) not in parents or parent in live:
            continue
        info = process_info(parent)
        if not info or info[0].startswith('Z'):
            continue
        session, waiter = _record(sessions, parent), _record(waiters, parent)
        # Old records survive exits. A matching PID alone isn't a connection.
        if session.get('claude_start') != info[1]:
            session = {}
        if waiter.get('claude_start') != info[1]:
            waiter = {}
        aliases = waiter.get('sessions')
        ids = [session.get('session_id'), *(aliases if isinstance(aliases, list) else [])]
        ids = list(dict.fromkeys(i for i in ids if session_id(i)))
        if ids:
            model = session.get('model')
            live[parent] = (child, {'id': ids[0], 'sessions': ids, 'label': 'Claude Code',
                                    'model': model[:120] if isinstance(model, str) else ''})
    directories = _working_directories([child for child, _ in live.values()])
    rows = [{**row, 'project': directories.get(child, '')} for child, row in live.values()]
    return sorted(rows, key=lambda row: (row['project'].casefold(), row['id']))


class ClaudePresence:
    def __init__(self, state):
        self.root = Path(state) / 'claude'
        self.checked = None
        self.rows = None

    def snapshot(self):
        # The task feed samples faster; avoid probing OS processes on every frame.
        if self.checked is None or time.monotonic() - self.checked >= 3:
            try:
                self.rows = _connected(self.root)
            except (OSError, subprocess.TimeoutExpired):
                self.rows = None  # Unavailable is distinct from no connections.
            self.checked = time.monotonic()
        return self.rows
