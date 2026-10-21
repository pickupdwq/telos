"""Platform dispatch. The macOS profile and argv remain unchanged."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def linux_runtime_paths(install, home):
    """Only OS/toolchain files; never grant an entire host home or workspace."""
    paths = [Path(p) for p in ('/usr', '/bin', '/sbin', '/lib', '/lib64',
             '/etc/ld.so.cache', '/etc/ssl', '/etc/hosts', '/etc/resolv.conf',
             '/etc/localtime', '/etc/nsswitch.conf', '/etc/passwd', '/etc/group')]
    paths += [Path(sys.base_prefix), Path(sys.executable), Path(install) / 'commands',
              Path(home) / '.local/share/telos-cli', Path(home) / '.local/share/exam_loop-cli']
    for name in ('node', 'codex'):
        found = shutil.which(name)
        if found:
            executable = Path(found).resolve()
            paths.append(executable)
            if name == 'codex':
                for parent in executable.parents:
                    if parent.name == '@openai':
                        paths.extend(p for p in parent.glob('codex*') if p.is_dir())
                        break
    return paths


def linux_profile(*, readable, writable, protected, private, network, ports):
    if type(network) is not bool or any(type(p) is not int or not 1 <= p <= 65535 for p in ports):
        raise ValueError('Invalid Linux sandbox network policy')
    def paths(values):
        return sorted({str(Path(p).resolve()) for p in values})
    writes = paths(writable)
    if '/' in writes:
        raise ValueError('The host root cannot be writable')
    aliases = {str(Path(p).absolute()): str(Path(p).resolve()) for p in [*readable, *writable]
               if Path(p).exists() and Path(p).absolute() != Path(p).resolve()}
    helper = shutil.which('bwrap')
    return {'backend': 'linux', 'readable': paths(readable), 'writable': writes, 'aliases': aliases,
            'protected': paths(protected), 'private': paths(private),
            'network': network, 'ports': sorted(set(ports)),
            'bwrap': str(Path(helper).resolve()) if helper else None}


def command(profile, argv):
    if isinstance(profile, str):
        return ['/usr/bin/sandbox-exec', '-p', profile, *argv]
    if not isinstance(profile, dict) or profile.get('backend') != 'linux':
        raise ValueError('Unsupported sandbox policy; isolation cannot be disabled')
    return [sys.executable, '-I', str(Path(__file__).with_name('linux_sandbox.py')),
            json.dumps(profile, separators=(',', ':')), '--', *map(str, argv)]


def linux_probe():
    if not shutil.which('bwrap'):
        return {'available': False, 'reason': 'Install bubblewrap and libseccomp2; Linux isolation is mandatory'}
    with tempfile.TemporaryDirectory(prefix='telos-isolation-probe-') as folder:
        policy = linux_profile(readable=linux_runtime_paths(Path(__file__).parent, Path.home()),
                               writable=[folder], protected=[], private=[], network=False, ports=[])
        try:
            result = subprocess.run(command(policy, [sys.executable, '-I', '-c', 'print("TELOS_ISOLATED")']),
                                    cwd=folder, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as error:
            return {'available': False, 'reason': 'Linux isolation probe failed: ' + str(error)[:500]}
        if result.returncode or result.stdout.strip() != 'TELOS_ISOLATED':
            return {'available': False, 'reason': 'Linux isolation unavailable; host must permit user/mount/PID/network '
                    'namespaces, Landlock and seccomp. No unsandboxed fallback. ' + result.stderr.strip()[-700:]}
    return {'available': True, 'reason': 'Real Bubblewrap/Landlock/seccomp process probe passed'}


def capability(*, platform=None):
    platform = platform or sys.platform
    if platform == 'darwin':
        return {'backend': 'seatbelt', 'available': Path('/usr/bin/sandbox-exec').is_file(),
                'reason': 'macOS sandbox-exec required'}
    if platform == 'linux':
        return {'backend': 'bubblewrap', **linux_probe()}
    return {'backend': None, 'available': False, 'reason': 'Only macOS and Linux are supported'}
