"""Local background-process liveness and reaping; no task decisions or model calls."""
import os
import signal
from subprocess import DEVNULL, PIPE, Popen, TimeoutExpired, run
import threading


def process_info(pid):
    """Inspect even children of another MCP process; unavailable probes are unknown."""
    if type(pid) is not int or pid <= 0:
        return None
    try:
        probe = Popen(['/bin/ps', '-p', str(pid), '-o', 'stat=', '-o', 'lstart='],
                      stdout=PIPE, stderr=DEVNULL, env={'LC_ALL': 'C', 'PATH': '/usr/bin:/bin'})
        try:
            output, _ = probe.communicate(timeout=5)
        except TimeoutExpired as error:
            probe.kill()
            drain_killed(probe, error)
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


def wait_killed(process, timeout=0.5):
    """Never make foreground completion depend on kernel reaping latency."""
    try:
        process.wait(timeout=timeout)
    except TimeoutExpired:
        reap_background(process)


def drain_killed(process, expired=None, timeout=0.5):
    """Bound pipe draining even when an orphan retains a writer. No reader threads."""
    try:
        out, err = process.communicate(timeout=timeout)
        return out or b'', err or b'', False
    except TimeoutExpired as error:
        out = error.output or getattr(expired, 'output', None) or b''
        err = error.stderr or getattr(expired, 'stderr', None) or b''
        # communicate uses nonblocking pipe I/O on supported POSIX hosts; no
        # competing buffered reader holds these stream locks.
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
        wait_killed(process, timeout)
        return out, err, True


def terminate_and_drain(process, expired=None):
    kill_process_tree(process)
    return drain_killed(process, expired)


def _process_table():
    """Bounded, metadata-only process snapshot for descendant cleanup."""
    try:
        result = run(['/bin/ps', '-A', '-o', 'pid=', '-o', 'ppid=', '-o', 'lstart='],
                     stdout=PIPE, stderr=DEVNULL, timeout=0.25,
                     env={'LC_ALL': 'C', 'PATH': '/usr/bin:/bin'})
        if result.returncode:
            return {}
        table = {}
        for line in result.stdout.decode('ascii', errors='replace').splitlines():
            fields = line.split(None, 2)
            if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
                table[int(fields[0])] = (int(fields[1]), fields[2])
        return table
    except (OSError, TimeoutExpired):
        return {}


def kill_process_tree(process):
    """Best-effort cleanup, including descendants that created another group.

    Reparented children may no longer be attributable to this check. Callers
    must still bound pipe draining; process discovery is not an EOF guarantee.
    """
    table = _process_table()
    children = {}
    for pid, (parent, _) in table.items():
        children.setdefault(parent, []).append(pid)
    descendants, pending = [], [process.pid]
    seen = {process.pid}
    while pending:
        for pid in children.get(pending.pop(), ()):
            if pid not in seen:
                seen.add(pid)
                descendants.append(pid)
                pending.append(pid)
    # Recheck identity before signalling: never kill an unrelated reused PID.
    current = _process_table() if descendants else {}
    for pid in reversed(descendants):
        if pid in current and current[pid][1] == table[pid][1]:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.kill()
        except ProcessLookupError:
            pass
