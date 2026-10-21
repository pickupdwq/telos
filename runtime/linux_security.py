"""Mandatory Linux worker filters, applied before exec and inherited by children.

Bubblewrap supplies filesystem/network/PID isolation. Landlock additionally
blocks procfs process-file reads; seccomp blocks Unix IPC and process/namespace
escape interfaces. This module must never be used as a filesystem sandbox alone.
"""
import ctypes
import errno
import os
from pathlib import Path
import platform
import sys


def _libc():
    if sys.platform != 'linux' or platform.machine() not in ('x86_64', 'aarch64', 'arm64'):
        raise RuntimeError('Linux isolation currently requires x86_64 or aarch64')
    return ctypes.CDLL(None, use_errno=True)


def _check(result, operation):
    if result < 0:
        raise OSError(ctypes.get_errno(), operation)
    return result


def restrict_proc_reads():
    lib = _libc()
    abi = _check(lib.syscall(444, 0, 0, 1), 'Landlock is required')
    if abi < 1:
        raise RuntimeError('Landlock ABI 1+ is required')
    class Ruleset(ctypes.Structure):
        _fields_ = [('access', ctypes.c_uint64)]
    class PathRule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [('access', ctypes.c_uint64), ('parent_fd', ctypes.c_int32)]
    read_file = 1 << 2
    attr = Ruleset(read_file)
    fd = _check(lib.syscall(444, ctypes.byref(attr), ctypes.sizeof(attr), 0), 'create Landlock ruleset')
    try:
        # This is the namespace filesystem, not the host root. Only procfs needs
        # another restriction; Bubblewrap already enforces all mounted paths.
        roots = [p for p in Path('/').iterdir() if p.name != 'proc']
        roots += [Path('/proc') / p for p in ('cpuinfo', 'meminfo', 'stat', 'uptime', 'version',
                                             'sys/kernel/osrelease', 'sys/kernel/random/boot_id')]
        for path in roots:
            if not path.exists():
                continue
            parent = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = PathRule(read_file, parent)
                _check(lib.syscall(445, fd, 1, ctypes.byref(rule), 0), 'add Landlock path')
            finally:
                os.close(parent)
        _check(lib.prctl(38, 1, 0, 0, 0), 'set no_new_privs')
        _check(lib.syscall(446, fd, 0), 'enforce Landlock ruleset')
    finally:
        os.close(fd)


def restrict_syscalls():
    try:
        sec = ctypes.CDLL('libseccomp.so.2', use_errno=True)
    except OSError as error:
        raise RuntimeError('libseccomp2 is required; no unfiltered fallback') from error
    class Compare(ctypes.Structure):
        _fields_ = [('arg', ctypes.c_uint), ('op', ctypes.c_uint),
                    ('a', ctypes.c_uint64), ('b', ctypes.c_uint64)]
    sec.seccomp_init.argtypes = [ctypes.c_uint32]
    sec.seccomp_init.restype = ctypes.c_void_p
    sec.seccomp_release.argtypes = [ctypes.c_void_p]
    sec.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    sec.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
                                         ctypes.c_uint, ctypes.POINTER(Compare)]
    sec.seccomp_load.argtypes = [ctypes.c_void_p]
    ctx = sec.seccomp_init(0x7fff0000)  # SCMP_ACT_ALLOW
    if not ctx:
        raise RuntimeError('Could not allocate seccomp filter')
    def deny(name, *comparisons, code=errno.EPERM):
        number = sec.seccomp_syscall_resolve_name(name.encode())
        if number < 0:  # syscall does not exist on this architecture
            return
        args = (Compare * len(comparisons))(*(Compare(*c) for c in comparisons))
        result = sec.seccomp_rule_add_array(ctx, 0x00050000 | code, number, len(args), args)
        if result < 0:
            raise RuntimeError('Could not enforce seccomp rule for ' + name)
    try:
        for name in ('ptrace', 'process_vm_readv', 'process_vm_writev', 'pidfd_getfd',
                     'kcmp', 'open_by_handle_at', 'name_to_handle_at', 'perf_event_open',
                     'bpf', 'io_uring_setup', 'mount', 'umount2', 'pivot_root', 'chroot',
                     'unshare', 'setns', 'keyctl', 'add_key', 'request_key'):
            deny(name)
        for family in (1, 16, 17, 40):  # Unix, netlink, packet, vsock
            deny('socket', (0, 4, family, 0))  # SCMP_CMP_EQ
        for flag in (0x00020000, 0x02000000, 0x04000000, 0x08000000,
                     0x10000000, 0x20000000, 0x40000000):
            deny('clone', (0, 7, flag, flag))  # SCMP_CMP_MASKED_EQ
        # glibc/Rust fall back to clone; pointer-based clone3 flags cannot be
        # inspected by classic seccomp without a notification broker.
        deny('clone3', code=errno.ENOSYS)
        _check(_libc().prctl(38, 1, 0, 0, 0), 'set no_new_privs')
        if sec.seccomp_load(ctx) < 0:
            raise RuntimeError('Could not enforce seccomp filter')
    finally:
        sec.seccomp_release(ctx)


def main():
    argv = sys.argv[1:]
    if not argv or argv.pop(0) != '--' or not argv:
        raise RuntimeError('Expected -- COMMAND ARGS')
    restrict_proc_reads()
    restrict_syscalls()
    os.execvpe(argv[0], argv, os.environ)


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError) as error:
        print('telos Linux security: ' + str(error), file=sys.stderr)
        sys.exit(125)
