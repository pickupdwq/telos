"""Optional user notification when an owner thread stalls with a callback waiting.

Telos never acts for the owner. With owner_stall_notify="macos" or "command" it
tells the user, once per stall, that the original conversation needs attention.
Runs in the unsandboxed callback dispatcher, never in an executor. The command
receives no credentials: only a minimal environment plus TELOS_STALL_* fields.
"""
import os
from pathlib import Path
import subprocess
import time

MODES = ('none', 'macos', 'command')
TIMEOUT = 10
OSASCRIPT = '/usr/bin/osascript'
# Text is passed as argv, never interpolated into AppleScript source.
APPLESCRIPT = ('on run argv', 'display notification (item 2 of argv) with title (item 1 of argv)', 'end run')


def mode(cfg):
    value = cfg.get('owner_stall_notify', 'none')
    return value if value in MODES else 'none'


def message(owner, event, now=None):
    since = event.get('owner_stalled_at') or now or time.time()
    waited = max(1, round(((now or time.time()) - min(since, event.get('created_at', since))) / 60))
    folder = Path(owner.get('cwd') or '').name or '?'
    return ('Telos: supervisor stalled / 大总管卡住',
            f'原 Codex 会话（{folder}）显示运行中却已无输出，回调已等待 {waited} 分钟，请打开查看。'
            f'Original conversation ({folder}) is active but silent; a callback has waited {waited} min.')


def command(cfg):
    argv = cfg.get('owner_stall_command')
    if (not isinstance(argv, list) or not 1 <= len(argv) <= 20
            or any(not isinstance(part, str) or not part or '\0' in part for part in argv)):
        raise ValueError('owner_stall_command must be a list of 1-20 nonempty strings')
    if not os.path.isabs(argv[0]) or not os.path.isfile(argv[0]) or not os.access(argv[0], os.X_OK):
        raise ValueError('owner_stall_command[0] must be an absolute path to an executable')
    return argv


def send(b, owner, event, sid):
    """Return None when sent, else a short error. Never raises."""
    cfg = b.config()
    selected = mode(cfg)
    title, text = message(owner, event)
    env = {**b.minimal_env(), 'HOME': str(b.HOME)}
    try:
        if selected == 'macos':
            argv = [OSASCRIPT, *(part for line in APPLESCRIPT for part in ('-e', line)), title, text]
        elif selected == 'command':
            argv = command(cfg)
            env.update(TELOS_STALL_TITLE=title, TELOS_STALL_MESSAGE=text, TELOS_STALL_SESSION_ID=sid,
                       TELOS_STALL_EVENT_ID=str(event.get('id')), TELOS_STALL_THREAD_ID=owner.get('thread_id') or '',
                       TELOS_STALL_CWD=owner.get('cwd') or '', TELOS_STALL_SINCE=str(event.get('owner_stalled_at')))
        else:
            return 'notifications are disabled'
        result = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, timeout=TIMEOUT, start_new_session=True)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return (type(error).__name__ + ': ' + str(error))[:300]
    if result.returncode:
        return f'exit {result.returncode}: ' + result.stderr.decode(errors='replace')[-300:]
    return None
