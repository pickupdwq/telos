"""Linux isolation contracts; unsupported hosts are never treated as passing."""
import importlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest


def backend(test):
    test.assertTrue(Path(__file__).with_name('sandbox.py').exists(),
                    'A platform-aware sandbox backend is required')
    return importlib.import_module('sandbox')


class PlatformSandboxTests(unittest.TestCase):
    def test_macos_command_preserves_policy_and_literal_arguments(self):
        sandbox = backend(self)
        profile = '(version 1)\n(allow default)'
        argv = ['/bin/echo', 'literal $HOME; $(false)']
        self.assertEqual(sandbox.command(profile, argv),
                         ['/usr/bin/sandbox-exec', '-p', profile, *argv])

    def test_linux_command_uses_isolated_python_and_retains_policy(self):
        sandbox = backend(self)
        profile = {'backend': 'linux', 'readable': [], 'writable': [],
                   'protected': [], 'private': [], 'network': False, 'ports': []}
        command = sandbox.command(profile, ['/bin/echo', 'a b'])
        self.assertEqual(command[:2], [sys.executable, '-I'])
        self.assertTrue(command[2].endswith('/linux_sandbox.py'))
        self.assertEqual(json.loads(command[3]), profile)
        self.assertEqual(command[4:], ['--', '/bin/echo', 'a b'])

    def test_invalid_profile_is_not_executed(self):
        sandbox = backend(self)
        for profile in ({'backend': 'disabled'}, None, 42):
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                sandbox.command(profile, ['/bin/true'])

    def test_linux_policy_rejects_invalid_ports_and_root_write(self):
        sandbox = backend(self)
        for ports in ([0], [65536], [True], ['443']):
            with self.subTest(ports=ports), self.assertRaises(ValueError):
                sandbox.linux_profile(readable=[], writable=[], protected=[], private=[],
                                      network=False, ports=ports)
        with self.assertRaises(ValueError):
            sandbox.linux_profile(readable=[], writable=['/'], protected=[], private=[],
                                  network=False, ports=[])

    def test_platform_capability_does_not_claim_linux_without_a_probe(self):
        sandbox = backend(self)
        from unittest.mock import patch
        with patch.object(sandbox, 'linux_probe', return_value={
                'available': False, 'reason': 'mount namespace denied'}):
            result = sandbox.capability(platform='linux')
        self.assertFalse(result['available'])
        self.assertIn('mount', result['reason'])

    def test_linux_telos_policy_uses_explicit_grants(self):
        backend(self)
        import telos
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder, patch.object(telos.sys, 'platform', 'linux'):
            try:
                policy = telos.sandbox_profile(writable=[folder], relay_port=12345)
            except telos.GateError as error:
                self.fail('Linux policy is still rejected: ' + str(error))
        self.assertEqual(policy['backend'], 'linux')
        self.assertEqual(policy['writable'], [str(Path(folder).resolve())])
        self.assertEqual(policy['ports'], [12345])
        self.assertIn(str(telos.STATE), policy['private'])

    def test_linux_preflight_uses_the_same_platform_command(self):
        sandbox = backend(self)
        import preflight
        from test_preflight import FakeBridge, FakeRunner
        policy = {'backend': 'linux', 'readable': [], 'writable': [],
                  'protected': [], 'private': [], 'network': False, 'ports': []}
        runner = FakeRunner()
        preflight.run(FakeBridge(), '/bin/sh', '/', {'PATH': '/usr/bin:/bin'}, policy, runner=runner)
        self.assertEqual(runner.calls[0][0], sandbox.command(policy, ['/bin/sh', '--version']))

    def test_symlinked_runtime_entrypoint_keeps_its_visible_name(self):
        sandbox = backend(self)
        with tempfile.TemporaryDirectory() as folder:
            entry = Path(folder)/'python-entry'
            entry.symlink_to(sys.executable)
            policy = sandbox.linux_profile(readable=[entry], writable=[], protected=[],
                                           private=[], network=False, ports=[])
            self.assertEqual(policy.get('aliases', {}).get(str(entry)), str(entry.resolve()))


class LinuxSecurityTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'linux', 'Linux kernel security primitives')
    def test_worker_cannot_open_unix_socket_or_parent_environment(self):
        backend(self)
        security = Path(__file__).with_name('linux_security.py')
        self.assertTrue(security.exists())
        code = ('import errno,os,socket\n'
                'try: socket.socket(socket.AF_UNIX)\n'
                'except PermissionError: pass\n'
                'else: raise AssertionError("Unix socket creation allowed")\n'
                'try: open("/proc/%s/environ" % os.getppid()).read()\n'
                'except PermissionError: pass\n'
                'else: raise AssertionError("parent environment exposed")\n'
                'print("isolation enforced")\n')
        result = subprocess.run([sys.executable, '-I', str(security), '--',
                                 sys.executable, '-I', '-c', code],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('isolation enforced', result.stdout)


class LinuxBrokerTests(unittest.TestCase):
    def module(self):
        self.assertTrue(Path(__file__).with_name('linux_sandbox.py').exists(),
                        'The fixed-destination Linux relay broker is required')
        return importlib.import_module('linux_sandbox')

    def test_broker_only_connects_to_frozen_loopback_ports(self):
        linux = self.module()
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0)); listener.listen()
            port = listener.getsockname()[1]
            def echo():
                conn, _ = listener.accept()
                with conn:
                    conn.sendall(conn.recv(100))
            thread = threading.Thread(target=echo, daemon=True); thread.start()
            with linux.connection_broker([port]) as address:
                with linux.broker_connection(address, port) as connection:
                    connection.sendall(b'fixed destination')
                    self.assertEqual(connection.recv(100), b'fixed destination')
                with self.assertRaises(OSError):
                    linux.broker_connection(address, port % 65535 + 1)
            thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_namespace_proxy_streams_bytes_bidirectionally(self):
        linux = self.module()
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0)); listener.listen()
            port = listener.getsockname()[1]
            def echo():
                conn, _ = listener.accept()
                with conn:
                    conn.sendall(conn.recv(100))
            threading.Thread(target=echo, daemon=True).start()
            with linux.connection_broker([port]) as address:
                # Another local port substitutes for the separate namespace.
                with linux.forward_port(address, port, listen_port=0) as proxy:
                    with socket.create_connection(proxy.server_address, timeout=3) as connection:
                        connection.sendall(b'proxy data')
                        self.assertEqual(connection.recv(100), b'proxy data')

    def test_protected_audit_parent_does_not_expand_read_scope(self):
        linux = self.module()
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder).resolve()/'source'; source.mkdir()
            selected = source/'selected.py'; selected.write_text('scoped')
            policy = {'readable': [str(selected)], 'writable': [],
                      'protected': [str(source)], 'private': []}
            plan = linux.mount_plan(policy)
            self.assertIn(('ro', str(selected), str(selected)), plan)
            self.assertFalse(any(source == Path(target) for _, _, target in plan))

    def test_git_protection_and_private_masks_follow_writable_mount(self):
        linux = self.module()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            git = root/'.git'; git.write_text('protected')
            private = root/'private'; private.mkdir()
            policy = {'readable': [], 'writable': [str(root)],
                      'protected': [str(git)], 'private': [str(private)]}
            plan = linux.mount_plan(policy)
            self.assertLess(plan.index(('rw', str(root), str(root))),
                            plan.index(('ro', str(git), str(git))))
            self.assertIn(('hide-dir', None, str(private)), plan)


@unittest.skipUnless(sys.platform == 'linux', 'Linux namespace integration')
class LinuxNamespaceTests(unittest.TestCase):
    def setUp(self):
        if not Path(__file__).with_name('sandbox.py').exists():
            return
        sandbox = importlib.import_module('sandbox')
        result = sandbox.capability(platform='linux')
        if not result['available']:
            if os.environ.get('TELOS_REQUIRE_LINUX_SANDBOX') == '1':
                raise AssertionError(result['reason'])
            self.skipTest('Host cannot run Linux isolation: ' + result['reason'])

    def test_network_forwards_only_authorized_live_loopback_ports(self):
        sandbox = backend(self)
        import linux_sandbox as linux
        import socketserver
        import telos
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.sendall(b'live')
        with tempfile.TemporaryDirectory() as folder, \
                linux.running(linux.TCPServer(('127.0.0.1', 0), Handler)) as allowed, \
                linux.running(linux.TCPServer(('127.0.0.1', 0), Handler)) as blocked:
            for server in (allowed, blocked):
                with socket.create_connection(server.server_address, timeout=2) as connection:
                    self.assertEqual(connection.recv(4), b'live')
            code = ('import socket\n'
                    f'with socket.create_connection({allowed.server_address!r}, timeout=3) as s:\n'
                    ' assert s.recv(4)==b"live"\n'
                    f'try: socket.create_connection({blocked.server_address!r}, timeout=2)\n'
                    'except OSError as e: assert e.errno in (1,13,101,111), e\n'
                    'else: raise AssertionError("unauthorized live port reachable")\n')
            policy = telos.sandbox_profile(writable=[folder], relay_port=allowed.server_address[1])
            result = subprocess.run(sandbox.command(policy, [sys.executable, '-I', '-c', code]),
                                    cwd=folder, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            # Sharing the host network is a distinct, explicit contract option.
            policy = telos.sandbox_profile(writable=[folder], network=True)
            code = (f'import socket; s=socket.create_connection({blocked.server_address!r}, timeout=3); '
                    'assert s.recv(4)==b"live"; s.close()')
            result = subprocess.run(sandbox.command(policy, [sys.executable, '-I', '-c', code]),
                                    cwd=folder, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_private_source_and_readonly_dependency_are_enforced(self):
        sandbox = backend(self)
        import telos
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            work, dependency, secret = root/'work', root/'dependency', root/'private'
            work.mkdir(); dependency.mkdir(); secret.write_text('private')
            (dependency/'data').write_text('declared')
            (work/'.git').write_text('protected')
            profile = telos.sandbox_profile(writable=[work], readable=[dependency],
                                            protected=[work/'.git'])
            code = ('import errno; from pathlib import Path\n'
                    f'assert Path({str(dependency / "data")!r}).read_text()=="declared"\n'
                    f'Path({str(work / "result")!r}).write_text("ok")\n'
                    f'for path,write in [({str(secret)!r},False),'
                    f'({str(dependency / "data")!r},True),({str(work / ".git")!r},True)]:\n'
                    ' try:\n'
                    '  p=Path(path); p.write_text("bad") if write else p.read_text()\n'
                    ' except OSError as e: assert e.errno in (errno.EACCES,errno.EPERM,errno.EROFS,errno.ENOENT)\n'
                    ' else: raise AssertionError("boundary escaped")\n')
            result = subprocess.run(sandbox.command(profile, [sys.executable, '-I', '-c', code]),
                                    cwd=work, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((work/'.git').read_text(), 'protected')


if __name__ == '__main__':
    unittest.main(verbosity=2)
