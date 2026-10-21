# Linux compatibility without changing macOS isolation

Base: `feature/rsi-20-rounds-20261020` at `8e0679a`.
Development branch: `telos-linux`.

## Contract

Retain the existing macOS Seatbelt policy and command invocation. Add a Linux
backend behind the same policy/command boundary for executors, closed-book
exams, executable checks, preflight and browser probes. Never fall back to
unsandboxed execution. Configuration, credentials, acceptance, original-owner
identity and historical tasks retain their existing semantics.

## Isolation

Linux uses Bubblewrap user/mount/PID/IPC/network namespaces, explicit read-only
and writable bind mounts, protected Git/dependency mounts and hidden private
paths. It requires a kernel with Landlock and libseccomp. The worker cannot
connect to host Unix sockets, inspect another process's environment, gain new
namespaces/capabilities, or read undeclared source, credentials or sibling jobs.

With command networking disabled, the worker has a separate network namespace.
Only explicitly authorized loopback relay/audit ports are forwarded. A trusted
namespace-local proxy obtains connections from a host broker that accepts only
those frozen ports. The worker never inherits broker descriptors, and its
seccomp policy prevents access to Unix sockets. Explicit command networking
retains the host network, but not host Unix-socket access. Provider keys remain
in the existing fixed-destination relay outside the worker.

The alternatives considered were replacing Seatbelt with one cross-platform
container layer (unnecessary macOS regression risk), and a Landlock-only Linux
port (insufficient subtraction of protected subpaths and fixed-destination
network isolation). Platform-specific backends preserve the existing contract.

## Integration and diagnostics

The installer accepts supported Linux/macOS hosts and runs an actual isolation
probe before publication. Doctor reports isolation capabilities and callback
socket availability separately. A Linux host that blocks namespace/mount
operations is unsupported until its host policy permits them; no automatic
sysctl, AppArmor or privilege changes are made.

The existing Unix-socket Codex CLI return transport is portable. Desktop IPC
remains the existing macOS adapter. Linux support does not make an arbitrary
hosted workshop expose a compatible original-thread control service. Missing
control service or trusted metadata remains an explicit blocker; do not invent
a thread identity, change supervisors or silently set `notify=false`.

## Verification and delivery

1. Record the original 527-test macOS baseline and 21 installer tests.
2. Add failing policy, platform dispatch, Linux security/proxy and prerequisite
   tests; implement the Linux backend without rewriting the Seatbelt body.
3. Run actual filesystem/network/process denials and the same runtime suite on
   a Linux host with namespaces enabled. CI must fail if isolation is unavailable.
4. Run the complete updated suite plus fresh-install/native callback verification
   on the user's Mac, in isolated test state. Do not upgrade its active runtime.
5. Preserve clean source on the new branch, push, and compare remote/local SHA.

Local cloud evidence must distinguish portable/unit/security tests from Linux
namespace integration: this workshop currently rejects mount operations even
inside an unprivileged user namespace. Successful CI on a supported Linux host
does not prove that this restriction has disappeared.
