# Plan implementation and projects

## Discuss before freezing implementation

For a new requirement, follow [multi-executor meetings](meetings.md) first. The supervisor selects a variable participant count and concrete roles, receives parallel independent opinions, resolves material disagreements, then freezes the public plan. Pass its `meeting_id` to the implementation open call. Meetings have no default token, round or job cap; existing implementation budgets and acceptance gates remain independent. A concrete `skip_meeting_reason` records a trivial or explicitly requested direct-work exception.

## Choose a reviewable unit

Use `telos_open` for a concentrated change whose behavior and code are best reviewed together. Use `telos_project_open` when the goal has separately reviewable parts or needs integrated acceptance. Choose single, parallel or phased project execution after examining dependencies and shared files; explain the decision instead of asking the user to select a mode without context. Respect explicit user choices and the connected runtime's concurrency limits.

Concurrent writers need disjoint literal `allowed_paths`. Add dependencies where tasks share writable files or rely on one another's output. Read-only audit tasks may overlap source scope. Splitting by file count alone is insufficient: cross-cutting behavior still needs an owner and final integration checks.

## Freeze the contract before sending work

Prepare two separate bodies of material:

| Public task brief | Private acceptance material |
| --- | --- |
| Goal and observable expected behavior | Questions, reference answers and scoring rationale |
| Allowed paths, dependencies and exclusions | Executable checks against the resulting pinned snapshot |
| Necessary architecture/context and evidence deliverables | Critical minima, blockers and task/project acceptance criteria |
| Environment/actions, budgets and stop conditions | No executor access to this material |

Base questions and checks on the user's requirements and inspected system. Ask about decisions and boundaries that matter to the result. Prefer a reproduction, relevant existing regression suite or observable output over checking whether files merely exist. Do not invent facts about an implementation that has not been written. Do not add irrelevant trivia or an exhaustive test suite for a small change.

Settle known business choices with the user before freezing the contract. The executor should check material ambiguity before changing affected code and return a structured question if it cannot resolve a missing decision from the public brief or scoped code. For a trivial, clear task, record the direct-work exception rather than requiring a ceremonial discussion. Later clarification must stay within the frozen scope and acceptance criteria.

For a small repair, include the relevant symbols/files, the reproduced symptom and a verified **public** test command with its cwd and interpreter. Keep private check implementations separate. Request bounded excerpts instead of repeated whole-file/diff reads; after a meaningful edit, run the targeted command and report the result. Investigate a concrete environment failure narrowly using the preflight interpreter rather than replacing PATH or exploring the host. Split unrelated changes into separate reviewable tasks before dispatch.

Plan for the entire work → final reply → checks → exam sequence. Use measured successful tasks when available; label provisional caps and headroom as supervisor choices. Where supported, freeze `exam_reserve_requests` / `exam_reserve_tokens` inside the task budget, and leave at least one work request for a factual final reply. Work and repairs cannot spend the exam reserve, and neither phase can exceed the original request cap. Token thresholds may overshoot; reservations do not guarantee success. Check the per-execution `job_timeout` as well as any whole-experiment time limit. A stopped/incomplete work turn still needs proper completion and cannot skip into grading.

When the operator sets `task_token_limits_enabled=false`, new task budgets omit cumulative token limits and `exam_reserve_tokens`, including limits supplied in tool arguments. Honor that preference; use the effective budget returned by the tool. Usage remains recorded, request/time limits remain available, and existing frozen contracts are unchanged.

Where supported, freeze `closeout_reserve_seconds` from observed request latency so work stops launching calls before `job_timeout`. Record the evidence and any chosen headroom; do not insert a universal token or time threshold into existing contracts.

The current check schema accepts `argv` as `['{python}', '-c', inline_code]`; that code may run appropriate project test commands in the verification snapshot. Verify the connected schema before constructing a contract. Resolve dependencies within the approved read-only paths. Command/check network access is off by default; a blocked dependency is not permission to relax isolation.

The entire verification snapshot is read-only, including new paths and parent directories. Checks cannot patch source, replace directories or create modules that shadow the submitted code. Put build outputs under `os.environ['TELOS_CHECK_OUTPUT']` and temporary files under `TMPDIR`; both are separate from the snapshot and are removed after the check sequence. Checks in one sequence may share these outputs. Configure tools that write into the source tree (for example coverage output or build directories) to use these paths. This also applies to project final checks and historical contracts; a frozen check that requires source mutation must fail and be replaced through a new reviewed contract, not silently loosened.

Initial task/context, public audit boundaries and retrieved experience use the same private-material guard as later messages. Project creation checks child briefs before saving the project; additions and reassignment are checked too. This guard detects known reference/check text and private-field labels, not all possible paraphrases. Keep public requirements and private answers separate when drafting them.

Create the contract with `telos_open` or `telos_project_open`, passing the concluded `meeting_id` (or explicit `skip_meeting_reason`); creation does not itself call the execution model. Start standalone work with `telos_send` or eligible project tasks with `telos_project_start`. Follow tool results and [acceptance](acceptance.md).

For enabled/authorized dynamic reasoning, choose the initial effort and allowed range before creation; project children can start at different levels. Follow [effort selection](executors.md#choose-effort-before-each-job). Freeze supported capabilities with the contract; later jobs can change effort inside that range without changing requirements, model, thread or acceptance criteria.

## Project completion

The supervisor reviews and accepts every child. Use project status/receive results to start eligible tasks; execution is bounded by the dependency graph and permissions. Retain accepted dependency commits as the baseline.

After child acceptance, use `telos_project_integrate` in the scratch worktree. Resolve conflicts only within authorized scope; `telos_project_resolve` records staged conflict resolution. Run `telos_project_check`, inspect `telos_project_review` and the exact integrated code, then call `telos_project_finish`. Child success is insufficient without final project `accepted=true`. The source branch is changed only by a separately authorized merge.

## Checkpoints and project environments (3.10)

New implementation work uses protocol v2. completed means ready for the existing
receive/evaluate/review/grade flow. checkpoint means unfinished authorized work:
do not ask the user whether to continue routine work, run authorized checks or fix
in-scope defects. blocked_environment requires specific environment evidence and
repair actions. needs_clarification is reserved for genuine missing business,
scope, permission or requirement decisions; format errors are protocol_error.

When auto_continue_checkpoints is frozen true, the runtime resumes the same
executor through its auditable internal scheduler. Honor pause/cancel, budgets,
no-progress and uncertain-result diagnostics. Never fabricate a receive receipt,
create a replacement task to reset limits, automatically evaluate/grade, or
silently alter frozen contracts. A completed submission still requires the
original supervisor's review. Legacy contracts preserve their old behavior.

Declare only relevant environment_requirements before creating a task/project:
exact tool paths, lock/config files, read-only download sources, necessary local
services, browser capabilities and already-authorized bootstrap commands.
dependency_paths and network permissions must be explicit. Reuse public sandbox
preflight evidence; check environment diagnostics before asking the user to
troubleshoot. Use TELOS_CHECK_OUTPUT and TMPDIR and independent writable state;
formal checks read immutable source. No automatic new dependencies or secret
values in requirements/context. Do not change model, dynamic reasoning or Token
settings merely to use checkpoint/environment support.
