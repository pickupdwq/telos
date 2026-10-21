"""Bubblewrap launcher and fixed-loopback-port bridge for isolated Linux jobs.

The host broker opens only frozen 127.0.0.1 TCP destinations. A trusted proxy in
the new network namespace receives those connected sockets. Worker processes
inherit neither broker descriptors nor permission to create Unix sockets.
"""
import array
import contextlib
import json
import os
from pathlib import Path
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class TCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


@contextlib.contextmanager
def running(server):
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@contextlib.contextmanager
def connection_broker(ports):
    allowed = frozenset(ports)
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                self.request.settimeout(10)
                raw = b''
                while len(raw) < 2:
                    chunk = self.request.recv(2 - len(raw))
                    if not chunk:
                        return
                    raw += chunk
                port = struct.unpack('!H', raw)[0]
                if port not in allowed:
                    self.request.sendall(b'N')
                    return
                with socket.create_connection(('127.0.0.1', port), timeout=10) as upstream:
                    upstream.settimeout(None)
                    self.request.sendmsg([b'Y'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                                  array.array('i', [upstream.fileno()]))])
                    # Do not close a connected socket while its descriptor is
                    # still in transit. Darwin can otherwise expose early EOF
                    # to the receiver; the bounded acknowledgement fixes the
                    # ownership handoff without expanding destination access.
                    if self.request.recv(1) != b'A':
                        return
            except OSError:
                with contextlib.suppress(OSError):
                    self.request.sendall(b'N')
    # The task's TMPDIR may be deeply nested. Unix socket addresses have a
    # kernel length limit; a private short directory does not expose all /tmp.
    with tempfile.TemporaryDirectory(prefix='tlx-', dir='/tmp') as folder:
        address = str(Path(folder) / 'broker.sock')
        server = UnixServer(address, Handler)
        os.chmod(address, 0o600)
        with running(server):
            yield address


def broker_connection(address, port):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as control:
        control.settimeout(15)
        control.connect(address)
        control.sendall(struct.pack('!H', port))
        data, ancillary, flags, _ = control.recvmsg(1, socket.CMSG_SPACE(array.array('i').itemsize))
        received = []
        for level, kind, payload in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                fds = array.array('i')
                fds.frombytes(payload[:len(payload) - len(payload) % fds.itemsize])
                received.extend(fds)
        if data != b'Y' or flags & socket.MSG_CTRUNC or len(received) != 1:
            for fd in received:
                os.close(fd)
            raise OSError('The sandbox broker refused the connection')
        os.set_inheritable(received[0], False)
        result = socket.socket(fileno=received[0])
        try:
            result.settimeout(None)
            control.sendall(b'A')
        except OSError:
            result.close()
            raise
        return result


def pipe_bytes(source, destination):
    try:
        while data := source.recv(65536):
            destination.sendall(data)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            destination.shutdown(socket.SHUT_WR)


@contextlib.contextmanager
def forward_port(address, port, *, listen_port=None):
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                with broker_connection(address, port) as upstream:
                    thread = threading.Thread(target=pipe_bytes, args=(self.request, upstream), daemon=True)
                    thread.start()
                    pipe_bytes(upstream, self.request)
                    thread.join(1)
            except OSError:
                pass
    with running(TCPServer(('127.0.0.1', port if listen_port is None else listen_port), Handler)) as server:
        yield server


def within(path, parent):
    return path == parent or parent in path.parents


def mount_plan(policy):
    """Order grants first, then protected descendants and private masks.

    Never bind a protected ancestor merely to make it read-only: an audit may
    grant one file under it, and mounting the ancestor would disclose siblings.
    """
    reads = {Path(p) for p in policy['readable'] if Path(p).exists()}
    writes = {Path(p) for p in policy['writable']}
    private = {Path(p) for p in policy['private']}
    protected = {Path(p) for p in policy['protected']}
    if Path('/') in reads | writes:
        raise ValueError('The host root cannot be mounted into a worker')
    grants = reads | writes
    for p in grants:
        if not p.is_absolute() or p.resolve() != p or not p.exists():
            raise ValueError('Sandbox path disappeared or was redirected: ' + str(p))
        if any(within(p, secret) for secret in private):
            raise ValueError('Sandbox grant overlaps a private path: ' + str(p))
    plan = []
    for p in sorted(grants, key=lambda p: (len(p.parts), str(p))):
        if within(p, Path('/dev')):
            continue  # Bubblewrap supplies a minimal device filesystem.
        kind = 'rw' if p in writes and not any(within(p, root) for root in protected) else 'ro'
        plan.append((kind, str(p), str(p)))
    for p in sorted(protected, key=lambda p: (len(p.parts), str(p))):
        if p.exists() and any(within(p, root) for root in writes):
            if p.resolve() != p:
                raise ValueError('Protected path was redirected: ' + str(p))
            plan.append(('ro', str(p), str(p)))
    for p in sorted(private, key=lambda p: (len(p.parts), str(p))):
        if p.exists() and any(within(p, root) for root in grants):
            plan.append(('hide-dir' if p.is_dir() else 'hide-file', None, str(p)))
    return plan


def inner(policy, address, argv):
    with contextlib.ExitStack() as stack:
        for port in policy['ports'] if not policy['network'] else []:
            stack.enter_context(forward_port(address, port))
        security = str(Path(__file__).with_name('linux_security.py'))
        # A fresh isolated interpreter applies filters before exec. No preexec
        # hook runs in a fork of the multithreaded proxy, and no broker FD leaks.
        result = subprocess.run([sys.executable, '-I', security, '--', *argv], close_fds=True)
        return result.returncode


def launch(policy, argv):
    if sys.platform != 'linux':
        raise RuntimeError('The Linux backend can only execute on Linux')
    # Resolve the helper in the supervisor, before a worker's PATH is narrowed.
    bwrap = policy.get('bwrap')
    if (not isinstance(bwrap, str) or not Path(bwrap).is_absolute()
            or not Path(bwrap).is_file() or Path(bwrap).resolve() != Path(bwrap)
            or not os.access(bwrap, os.X_OK)):
        raise RuntimeError('The pinned Bubblewrap helper is unavailable; isolation cannot be disabled')
    if policy.get('backend') != 'linux' or type(policy.get('network')) is not bool:
        raise ValueError('Invalid Linux sandbox policy')
    ports = policy.get('ports')
    if not isinstance(ports, list) or any(type(p) is not int or not 1 <= p <= 65535 for p in ports):
        raise ValueError('Invalid authorized loopback ports')
    with contextlib.ExitStack() as stack:
        address = (stack.enter_context(connection_broker(ports))
                   if ports and not policy['network'] else '')
        args = [bwrap, '--unshare-all', '--unshare-user', '--die-with-parent', '--new-session',
                '--cap-drop', 'ALL', '--disable-userns']
        if policy['network']:
            args.append('--share-net')
        args += ['--proc', '/proc', '--dev', '/dev', '--dir', '/tmp']
        for alias, target in sorted(policy.get('aliases', {}).items(), key=lambda item: len(Path(item[0]).parts)):
            args += ['--symlink', target, alias]
        fds = []
        for kind, source, target in mount_plan(policy):
            if kind in ('ro', 'rw'):
                args += ['--ro-bind' if kind == 'ro' else '--bind', source, target]
            elif kind == 'hide-dir':
                args += ['--perms', '000', '--tmpfs', target, '--remount-ro', target]
            else:
                stream = stack.enter_context(open('/dev/null', 'rb'))
                fds.append(stream.fileno())
                args += ['--perms', '000', '--ro-bind-data', str(stream.fileno()), target]
        for file in (Path(__file__).resolve(), Path(__file__).with_name('linux_security.py').resolve()):
            args += ['--ro-bind', str(file), str(file)]
        if address:
            args += ['--ro-bind', address, address]
        # Synthetic parents of selected audit files must not accept new files.
        # This is non-recursive: explicit writable bind mounts stay writable.
        args += ['--remount-ro', '/']
        args += ['--chdir', os.getcwd(), '--', sys.executable, '-I', str(Path(__file__).resolve()),
                 '--inner', json.dumps(policy, separators=(',', ':')), address, '--', *argv]
        result = subprocess.run(args, pass_fds=tuple(fds))
        return result.returncode


def main():
    args = sys.argv[1:]
    if args and args[0] == '--inner':
        policy, address = json.loads(args[1]), args[2]
        if args[3] != '--' or not args[4:]:
            raise ValueError('Expected isolated worker command')
        return inner(policy, address, args[4:])
    if len(args) < 3 or args[1] != '--':
        raise ValueError('Expected POLICY -- COMMAND ARGS')
    return launch(json.loads(args[0]), args[2:])


if __name__ == '__main__':
    try:
        result = main()
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        print('telos Linux sandbox: ' + str(error), file=sys.stderr)
        result = 125
    sys.exit(result if result >= 0 else 128 - result)
