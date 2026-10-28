# Checkpoints and declared project environments

New implementation contracts use work reply protocol version 2. Existing contracts
without these fields retain protocol 1; reading/recovery never backfills a contract.

## Work replies and continuation

Every v2 reply is one complete JSON object with protocol_version=2, status,
summary, changed_files, checks_performed, remaining_issues and
clarification_questions. The four arrays contain strings.

- completed: ready for the original receive/evaluate/review/grade flow, not accepted.
- checkpoint: authorized work remains, with no supervisor decision needed.
  Also requires completed_work, remaining_work, next_step and evidence_paths.
- blocked_environment: a concrete environment blocker. Also requires blockers,
  repair_actions and evidence_paths.
- needs_clarification: missing requirements, business, scope or permission decision.
  Requires nonempty clarification_questions; all other statuses require an empty array.

Valid old completed/needs_clarification JSON objects remain readable. Invalid,
contradictory, duplicate-key, truncated or unsupported replies become protocol_error
on v2 contracts. Raw replies remain in delivery-<job>.json; no prose guessing or
JSON repair turns invalid data into completed work. One explicitly frozen format
correction resumes the same executor and consumes existing job/request/turn budgets.
If correction fails, the original supervisor receives the protocol diagnosis.

auto_continue_checkpoints is a boolean task/project input or local config default
(false in distributed config). New implementation tasks freeze the effective value;
project children freeze it when queued, not when eventually launched. Old contracts
without it do not auto-continue. Changing local defaults never changes old tasks.

An authorized continuation records its original job, delivery hash, contract hash,
executor thread, action ID and newly reserved job in private durable state.
It uses the coordinator/session lock order and normal launch/admission path.
There is no fabricated telos_receive receipt. A crash with an uncertain launch
retains diagnostics and does not resend the job. Valid completed replies continue
to notify the original supervisor and require the original acceptance steps.

Pause/cancel, business questions, environment blockers, request/job budgets,
unknown required usage and the no-progress guard stop automatic continuation.
Automatic continuation admission waits are recorded as scheduler_wait, separately
from work and supervisor time. Paused time is separate; launch or a stopped
continuation ends the wait. Older counters retain their recorded attribution,
with scheduler_wait_tracked_since indicating coverage of the new metric.
Actual source/check evidence drives progress; changing a checkpoint summary does
not reset it. Forced timeouts remain errors. Normal pending checkpoints wait for
existing capacity without repeatedly waking the supervisor. No model, concurrency
limit, token policy, acceptance criterion or dependency graph is changed.

## Declare only the project's environment needs

environment_requirements is optional and has version=1. Available fields:

| Field | Declaration |
| --- | --- |
| tools | id, absolute path, optional kind (python/node/package_manager/tool), version_args, exact expected_version |
| lockfiles / config_files | repository-relative files whose actual sandbox-visible bytes enter the cache identity |
| download_caches | explicit read-only cache/source directories; access must already be granted by dependency_paths |
| variables | allowlisted non-secret registry/proxy/offline settings and CI; NO_PROXY/no_proxy is a comma-separated bypass list, not a URL; no HOME/PATH override or credentials |
| services | id, loopback host, port, optional phases; grants access only to those local ports |
| registries | id, credential-free HTTP(S) URL, optional phases; HEAD probe, no redirects |
| browser | absolute node, module and executable paths, optional phases; actual sandbox launch, not host-only discovery |
| bootstrap | explicitly authorized argv, scratch-relative outputs, optional phases and timeout_seconds |
| local_ipc | explicit opt-in for Unix sockets beneath this workspace's TMPDIR |

phases defaults to work and checks. Runtime paths outside existing runtime reads
still require explicit dependency_paths. Registry access requires the existing
network permission for that phase. Failure never expands permissions, installs
software or discovers/executes repository scripts implicitly. Use credential
references outside public declarations; do not paste credentials into commands,
URLs, environment requirements, reports or configuration examples.

NO_PROXY/no_proxy supports host/domain/IP entries, optional ports, CIDR, wildcards
and an empty value; scoped IPv6 is excluded. Exact matching depends on the HTTP
client. The effective declaration is supplied in both cases to work and checks;
explicit lowercase wins if both are declared. Proxy bypass never expands sandbox
network access.

Bootstrap runs with scratch as cwd. Supported argv substitutions are {python},
{repo}, {scratch}. Commands are existing, explicitly authorized setup commands,
not package-manager guesses. Use TELOS_CHECK_OUTPUT for generated/build/dependency
outputs and TMPDIR for temporary files. Each workspace has its own writable
HOME, temporary, output and test-data directories. Source remains immutable in
formal checks; commands requiring writable source must prepare their own scratch
copy instead. Download sources are read-only. Shared writable node_modules,
.next and generated trees are not created by this facility.

## Verification and reuse

Base CLI/Python/Git preflight remains. Declared tools/version/ABI, lock/config
hashes, writable scratch permissions and local service reachability are checked
inside the actual sandbox on every invocation. Only relevant declared capabilities
are probed. Before the first executor call, public environment checks also run on
a clean committed snapshot under the formal-check sandbox, without executing or
revealing private acceptance programs. This cannot predict dependencies introduced
by later source changes; formal checks repeat preflight against their actual snapshot.

Shared facts are keyed by the Telos probe version, declarations, actual runtime/version/ABI/binary identity,
lock/config hashes, platform/architecture, browser identities, phase and sandbox
capabilities. Work/check capabilities have separate cache entries. Per-workspace
private paths and ephemeral relay ports are normalized; declared service ports
remain in the requirement identity. Browser launch and registry probes reuse
matching successful evidence for up to five minutes. Local services and workspace permissions stay live.

Cache writes are atomic and serialized across processes. Corrupt/incomplete entries
are misses; failed entries are never successes. Matching heavy-probe failures retain
a 30-second diagnostic to keep sibling executors from repeating the same expensive
failure; changed identities invalidate immediately. No preinstalled dependency tree
is shared between workspaces. Each workspace reuses its own successful bootstrap
only when source, environment key, commands and output-integrity hashes still match.
An interrupted/failed preparation cannot publish a successful marker. Output links
must stay inside their declared output tree; integrity scans have file-count,
byte and time bounds and fail closed when exceeded.

Work scratch uses short owner-only paths under the system temporary directory,
separate from persistent executor CODEX_HOME. Scratch is task-specific ephemeral working data and is not part of the persistent
evidence archive; keep reviewable reports in the worktree/task evidence. Reclaimed scratch is rebuilt from
authorized commands and read-only sources; it is never assumed to survive OS cleanup.
Formal-check scratch is freshly isolated and removed after checking. Reports retain
the phase, tool facts, cache identity, hit/miss, preparation results and failure
remediation; model/relay credentials are excluded.

Project-level requirements and the frozen continuation default are inherited by
initial and subsequently added implementation children. Explicit child values override
the default; existing child contracts are never rewritten.
Existing dependency_paths semantics remain: project-level dependency paths grant
final-check access; declare child dependencies explicitly as well.

## 验证与使用

本功能面向交付速度和质量，不增加 Token 限制，不改变动态推理配置。
可配置 auto_continue_checkpoints=true 供新建实施任务使用；历史合同不回填。
环境要求只声明项目实际需要的能力。无声明时保留原基础预检。
自动续做停止或环境阻塞时查看 status 中的 auto_continuations、environment、
delivery 和 next_action；原始回包及环境证据保留在任务目录。
最终 completed 仍由原监督者接收、检查、审查和评分。
