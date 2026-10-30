# Acceptance configuration preflight and revision

These operations help the original supervisor detect and correct a defective
check configuration. They do not repair product code, waive failed behavior,
change sandbox access or provide additional work/evaluation budget.

## Preflight

Call `telos_preflight_checks` with either `session_id` or proposed `contract` /
`checks`. `root` is an optional base for missing-path diagnostics. Preflight parses
and compiles the inline Python form; it never executes a check, dispatches a job
or writes/quarantines state. Existing sessions still require their original owner.

New standalone tasks, project children, added children and project final checks
use the same preflight before freezing. Old contracts are not retroactively
rejected merely because they fail the new preflight.

Syntax/compile errors and a small set of provable report-schema conflicts block
creation. Report analysis recognizes standard `json.load(s)` chains originating
from `TELOS_AUDIT_REPORT`, including simple imported aliases and intermediate
path/text variables. A path or a string containing that environment key is not
itself a report. Rebinding, mutation, optional lookups, defaults, branches and
dynamic keys are not treated as proof of an invalid required field. The analysis
is intentionally incomplete; arbitrary Python semantics need supervisor review.
Missing paths, including literal pytest file arguments, are advisory because an
implementation may create them later. No missing file is automatically renamed.

## Preview, review, apply

1. Receive the current completed submission event. Use the same session and
   original supervisor. Active work/probes, pending exams, clarification,
   paused/cancelled/archived or accepted work cannot be revised.
2. Supply one existing `check_id`, its exact `before_program`, proposed
   `after_program`, stable `request_id`, actual `user_authorization`, `reason`,
   `preservation_reason`, evidence references and received `event_id`.
3. Review the complete private before/after/diff and `proposal_hash` returned by
   preview. Review business assertions and negative cases, not just assertion
   counts. Never forward private programs or reference answers to the executor.
4. Apply with the same parameters plus `apply=true` and that proposal hash.
   The tool recomputes under the coordinator/session locks. The proposal binds
   the original and effective contracts, source HEAD/tree/cleanliness, audit
   artifact content hashes, retained check evidence, event and previous revision.
   A changed source, evidence or parameter invalidates the proposal. Running audit
   probes are excluded with their independent lock.
5. Run fresh checks, the same executor's closed-book exam and supervisor grading.
   Projects still need integrated checks and project-level acceptance.

Only the selected check's `argv[2]` changes. Its prefix, ID, network access,
timeout, dependencies, environment, scores and all other contract fields stay
unchanged. Programs must parse; assertion-node reduction is rejected as a
conservative guard, **not a proof of semantic equivalence**. A supervisor must
reject changes that weaken the promised behavior even when the count is equal.

## Retained history and compatibility

Original `contract.json` bytes, counters, usage, work/evaluation limits and prior
evidence files remain unchanged. The operation appends a versioned record and
event action in one atomic state write; only current packet/acceptance/automatic
check pointers are invalidated. It never starts, resumes or accepts a task.

All semantic request parameters are canonically bound. Identical replays of any
recorded request are read-only; reuse with different programs, check, event,
reason or evidence fails. Public summaries contain hashes and authorization
metadata, not check programs. Session status/review and project evidence retain
these summaries. Multiple revisions may target different checks; their contract
and record hashes form one ordered chain.

The two historical fixed correction types remain mutually exclusive. Their
original validation runs first; a subsequent generic revision binds the existing
fixed record hash. Once generic revisions exist, the historical correction tools
cannot rewrite that base. Absent generic state retains legacy behavior; null,
damaged or inconsistent revision records are rejected.

Trust comes from the original owner, received events, protected state and
cross-checked hashes. This does not defend against a machine administrator able
to rewrite every private record and trust anchor. No new secrets or dependencies
are introduced.

## Verification boundary

`test_check_preflight` and `test_check_revision.RevisionStateTests` can run with
real temporary Git/state fixtures and an explicit mocked model boundary. The
real loader/lifecycle tests and full `runtime/verify_all.py` run on the host so
they exercise macOS sandboxes without nesting `sandbox-exec`. Installer tests
run separately. Synthetic lifecycle acceptance is test evidence only, never
acceptance of a real task or proof of an external model call.
