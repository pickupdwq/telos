"""Local background-process liveness and reaping; no task decisions or model calls."""
import os
from subprocess import DEVNULL, PIPE, Popen, TimeoutExpired
import threading


def process_info(pid):
    """Inspect even children of another MCP process; unavailable probes are unknown."""
    if type(pid) is not int or pid <= 0:
        return None
    try:
        with Popen(['/bin/ps', '-p', str(pid), '-o', 'stat=', '-o', 'lstart='],
                   stdout=PIPE, stderr=DEVNULL, env={'LC_ALL': 'C', 'PATH': '/usr/bin:/bin'}) as probe:
            try:
                output, _ = probe.communicate(timeout=5)
            except TimeoutExpired:
                probe.kill()
                probe.communicate()
                return None
        fields = output.decode('ascii', errors='replace').strip().split(None, 1)
        return tuple(fields) if probe.returncode == 0 and len(fields) == 2 else None
    except OSError:
        return None


def process_start(pid):
    info = process_info(pid)
    return info[1] if info else None


def process_running(pid, expected_start=None):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        ended, _ = os.waitpid(pid, os.WNOHANG)
        if ended == pid:
            return False
    except ChildProcessError:
        pass  # Another MCP process owns this child, or a waiter already reaped it.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Lack of visibility is not proof that work stopped.
    info = process_info(pid)
    if info:
        return not info[0].startswith('Z') and (not expected_start or info[1] == expected_start)
    try:
        os.kill(pid, 0)  # It may have exited during the probe.
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def reap_background(process):
    # Keep the Popen handle alive and reap promptly, even after a successful job
    # stops appearing in the running-task scan. Daemon exit never kills the child.
    threading.Thread(target=process.wait, name='telos-child-wait', daemon=True).start()
