"""No-model environment checks inside the executor's effective sandbox.

Only version and repository-root probes run here. No repository-controlled
hooks, filters, shell startup files or model calls are used, and the probe runs
inside the same Seatbelt profile, environment and cwd as the work request.
"""
from __future__ import annotations
import functools
import os
from pathlib import Path
import shutil
import subprocess
import sys

CLT_GIT = Path('/Library/Developer/CommandLineTools/usr/bin/git')
XCODE_GIT = Path('/Applications/Xcode.app/Contents/Developer/usr/bin/git')


def _executable(path):
    path = Path(path)
    return path.is_file() and os.access(path, os.X_OK)


@functools.lru_cache(maxsize=1)
def git_executable():
    """Prefer a real Git binary over Apple's /usr/bin/git xcrun shim."""
    found = shutil.which('git')
    if found and Path(found) != Path('/usr/bin/git') and _executable(found):
        return found
    for candidate in (CLT_GIT, XCODE_GIT):
        if _executable(candidate):
            return str(candidate)
    return found


@functools.lru_cache(maxsize=1)
def _git_runtime_dirs():
    git = git_executable()
    if not git:
        return ()
    paths = [Path(git)]
    try:
        exec_path = subprocess.run([git, '--exec-path'], capture_output=True, text=True,
                                   timeout=10, env={'PATH': '/usr/bin:/bin'})
        if exec_path.returncode == 0 and exec_path.stdout.strip():
            paths.append(Path(exec_path.stdout.strip()))
    except (OSError, subprocess.TimeoutExpired):
        pass
    for candidate in (Path(git).parent.parent / 'share/git-core',
                      Path(git).parent.parent / 'libexec/git-core'):
        if candidate.is_dir():
            paths.append(candidate)
    return tuple(dict.fromkeys(p.resolve() for p in paths if p.exists()))


def git_runtime_paths():
    """Narrow read-only paths needed by the selected Git executable."""
    return list(_git_runtime_dirs())


def _redact(text, secrets):
    for secret in secrets:
        if isinstance(secret, str) and secret:
            text = text.replace(secret, '[REDACTED]')
    return text


def _diagnostic(p, label, secrets):
    out = p.stdout.decode(errors='replace') if isinstance(p.stdout, bytes) else (p.stdout or '')
    err = p.stderr.decode(errors='replace') if isinstance(p.stderr, bytes) else (p.stderr or '')
    detail = _redact((err.strip() + '\n' + out.strip()).strip(), secrets)[-700:]
    if 'xcrun_db' in detail or ("couldn't create cache file" in detail and 'Operation not permitted' in detail):
        detail += (' Apple Git is using the xcrun shim, which cannot write its cache inside the sandbox. '
                   'Use the already installed CommandLineTools git executable instead.')
    return f'Environment preflight failed before any model request at {label} (exit {p.returncode}): ' + (detail or 'no diagnostic output')


def run(b, cli, cwd, env, profile, *, check_repo=False, secrets=(), timeout=20,
        runner=None, sandbox_exec='/usr/bin/sandbox-exec'):
    """Verify the chosen CLI, Python and Git in the exact work sandbox.

    Returns bounded version evidence. Raises b.GateError without making a model
    request when any dependency is missing or inaccessible.
    """
    runner = runner or subprocess.run
    git = git_executable()
    if not git:
        raise b.GateError('Environment preflight failed before any model request: Git is not installed or not on PATH; install Apple Command Line Tools or Git')
    probes = [
        ('dedicated CLI', [str(cli), '--version']),
        # Isolated mode keeps the task cwd/repository off sys.path and ignores
        # PYTHON* startup environment, so repository json.py/ssl.py or startup
        # customizations cannot run inside the no-model probe.
        ('Python', [sys.executable, '-I', '-c', 'import json, ssl, sys; print(".".join(map(str, sys.version_info[:3])))']),
        ('Git', [git, '--version']),
    ]
    if check_repo:
        probes.append(('Git worktree', [git, '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null',
                                       '-C', str(cwd), 'rev-parse', '--show-toplevel']))
    evidence = {'status': 'passed', 'cwd': str(cwd), 'checks': []}
    for label, argv in probes:
        command = [sandbox_exec, '-p', profile, *argv]
        try:
            p = runner(command, cwd=str(cwd), env=env, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise b.GateError(f'Environment preflight timed out before any model request at {label}; inspect the selected CLI/runtime and retry')
        except OSError as error:
            raise b.GateError(f'Environment preflight could not run the sandbox probe at {label}: {_redact(str(error), secrets)[:500]}') from None
        if p.returncode:
            raise b.GateError(_diagnostic(p, label, secrets))
        out = p.stdout.decode(errors='replace') if isinstance(p.stdout, bytes) else (p.stdout or '')
        err = p.stderr.decode(errors='replace') if isinstance(p.stderr, bytes) else (p.stderr or '')
        if label == 'Git' and ('xcrun_db' in err or ("couldn't create cache file" in err and 'Operation not permitted' in err)):
            raise b.GateError(_diagnostic(p, label, secrets))
        evidence['checks'].append({'name': label, 'output': _redact(out.strip()[:240], secrets)})
    return evidence
