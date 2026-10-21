# Linux host support

This branch adds a Linux isolation backend. The existing macOS Seatbelt policy
is retained. Linux execution requires all of:

- Python 3.12+, Git, Node.js, and the dedicated Codex CLI 0.163.0.
- Linux x86_64 or aarch64 with Landlock enabled (kernel 5.13+).
- Bubblewrap 0.8+ and `libseccomp.so.2` (Debian/Ubuntu: `bubblewrap libseccomp2`).
- Permission to create user, mount, PID, IPC and network namespaces.

Until this branch is merged, clone with `--branch telos-linux` before following
the README installation commands; the default branch does not include this port.
After installing the OS packages and dedicated CLI, use the same installer.
`python3 install.py --dry-run` runs a real isolated Python process before
installing files. `telos doctor` reports the backend and its failure reason.
Package presence alone is not sufficient: an outer container, AppArmor policy
or host setting may still deny mounts. Telos does not disable isolation, modify
host security settings or request a privileged container automatically.

Check the actual Bubblewrap version: Ubuntu 22.04's original 0.6 package is too
old for this backend. CI builds the pinned upstream 0.9.0 release without setuid
on an Ubuntu 22.04 runner. The tested Ubuntu 24.04 hosted image rejected even a
minimal unprivileged Bubblewrap UID/network setup with its existing AppArmor
policy; this is a host prerequisite failure, not permission to turn that policy
off. Other images and container policies must pass the same real probe.

## What the backend enforces

The namespace filesystem exposes explicit read-only toolchain/dependency paths
and per-task writable directories. Git metadata and protected dependencies are
mounted read-only; private materials are hidden. Closed-book exams retain their
isolated home and receive no worktree/dependency grants.
Synthetic parent directories are also read-only, so granting one audit source
file cannot expose its siblings or allow new files beside it. Bubblewrap's
absolute executable path is frozen before the worker environment is narrowed.

By default each worker has a separate network namespace. A trusted local proxy
can reach only the task's frozen loopback model-relay and audit-broker ports.
The host broker opens those destinations itself and passes connected sockets;
the worker receives no broker descriptor. Seccomp denies host Unix-socket access,
ptrace/process-memory interfaces and new namespaces. Landlock denies reads of
procfs process files such as another process's environment. Explicit
`command_network=true` retains the host network, while the other restrictions
remain. Upstream provider credentials stay outside the worker as before.

Neither backend protects against a compromised host or resource exhaustion.
Bubblewrap mount-boundary failures may return `EROFS` or `ENOENT` where macOS
returns `EPERM`; both must actually deny the operation.

## Original-session return is a separate capability

The CLI Unix-socket transport works independently of the OS backend. The
original supervisor must expose the compatible local Codex control service and
trusted MCP `threadId` metadata. Doctor reports CLI control-socket availability
separately from isolation. The existing Desktop IPC adapter is not a Linux
workshop integration.

Registering MCP in a hosted workshop does not create that workshop's missing
control socket. Automatic return still fails explicitly when the original
session is unavailable. Telos never substitutes a different supervisor or
silently disables notifications. The documented explicit manual mode remains
available only when the caller intentionally chooses it.

## Verification

```sh
python3 -m unittest discover -s tests -v
mkdir -p .artifacts
TELOS_REQUIRE_LINUX_SANDBOX=1 python3 runtime/verify_all.py .artifacts/linux.json --jobs 4
python3 install.py --prefix "$PWD/.artifacts/installed" --bin-dir "$PWD/.artifacts/bin" --skills-dir "$PWD/.artifacts/skills"
python3 runtime/verify.py --install-dir "$PWD/.artifacts/installed" --report .artifacts/installation.json
```

CI runs the same runtime inventory on macOS and Ubuntu, with local model
fixtures. Linux CI requires a working sandbox; it cannot pass by skipping all
namespace tests. On restricted hosts, portable tests and the independent
Landlock/seccomp/broker tests are useful partial evidence, not a completed
Linux execution verification. Real credentials, browser binaries and hosted
workshop callbacks each need their own evidence.
