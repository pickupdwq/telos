# Frozen automatic checks and phase timing (3.11)

`auto_run_checks` is a boolean creation policy. The shipped default is **false**.
The operator may enable it in mutable local `config.json`; explicit creation input
overrides that default. New implementation sessions and all newly allocated project
children freeze it before work or queueing. Project additions inherit the frozen
project default. Existing contracts are never backfilled or rewritten.

## Flow and control

A valid `completed` delivery pins its commit/tree and materialized source snapshot.
The same session queues one local-check job identified by source job, delivery,
contract and attempt. The existing coordinator admits it under the same project and
provider capacity limits. Local stages are counted separately in
`local_checks_started`; no model request, token usage or project model job is invented.
One evaluation round is reserved for checks and reused by its first exam. A new
evaluation after a rejected grade consumes a new round even if checks remain reusable;
only bounded infrastructure retries retain an unfinished reservation.

The original delivery event remains available while checks run, allowing supervisor
preparation/code reading to overlap. No `receive` is fabricated. Normal state views
show the check phase. Completion uses the existing durable outbox/project batching;
automatic-stage callbacks wait while the original chat is active. Receipt and action
identity remain mandatory. Recovery uses the existing recovery loop, not new polling.

`telos_evaluate` returns `await_checks_completion` if the matching job is pending or
running. Successful evidence allows the **same executor's closed-book exam**, followed
by original-supervisor review and grade. No automatic stage can set `accepted=true`.
A project still requires its existing integrate/check/review/finish gates.

Checkpoint, clarification, environment-blocked and malformed replies never trigger
these checks. Pause/cancel, exhausted round/request budgets and no-progress controls
block new stages. A command is launched under the same session/control lock; an
already launched bounded command may finish, but changed control prevents its result
from advancing into exam/acceptance. Each command retains its frozen timeout.

A durable claim prevents duplicate workers. Recovery can complete the phase update
from a result whose proof was committed to state. Missing or ambiguous proof is
`uncertain`: retain artifacts and inspect; never automatically rerun that program.
This intentionally favors safe uncertainty over exactly-once claims that cannot be
proved after a process crash.

## Evidence identity and freshness

Evidence covers session/source delivery/check attempt, reserved round, commit/tree,
contract/check hashes, snapshot content, actual sandbox profile hash, runner module
and Python identities, platform, explicit dependency/tool/cache contents, environment
variables and permissions. Relevant input changes during execution invalidate the
result. Read-only source and separate scratch are enforced by the existing macOS
sandbox, not by the evidence label. Integrity scans are bounded and fail closed.

Each frozen check may declare:

```json
{"id":"unit", "argv":["{python}","-c","...private program..."],
 "freshness":{"mode":"snapshot"}}
```

- `snapshot`: an explicit assertion of deterministic behavior over the snapshot and
  all relevant declared dependencies/tools. No clock, undeclared mutable input,
  network or external service may affect validity. Do not select this for speed.
- `max_age`: include `max_age_seconds` (1–3600), an explicitly authorized validity
  window. External services/IPC require this or `never`; matching commit is insufficient.
- omitted / `never`: no cross-stage reuse. Evaluation performs one fresh check in
  the already reserved round. Expired evidence gets the same bounded fallback.

Only freshness failure permits that one explicit recheck. Modified, incomplete,
missing, failed or mismatched evidence blocks with diagnostics. A second freshness
recheck for the same delivery is refused; repair/new delivery remains subject to the
original limits. No-progress history survives, and reuse does not create a second
check observation. Closed-book retries keep infrastructure/budget gates.

Packets bind the exact check evidence hash and attempt. Review and grade validate
that binding, current source/environment and freshness where applicable. Accepted
history retains an integrity check, including after archival; it does not pretend an
external-service result remains a statement of current health indefinitely. Private
code, references and raw failures are never forwarded to the executor.

## Timing interpretation

`timing.elapsed_wall_seconds` is a task/project lifecycle, not the sum of children.
`requirement_elapsed_wall_seconds` starts at the earliest recorded linked meeting or
creation boundary. Time before the runtime saw the requirement is unavailable.

`timing.stages` contains independent intervals, cumulative category durations, their
union and overlap, ongoing intervals and milestones. Categories cover actual model
requests, environment preparation, scheduling/capacity, checks, closed-book work,
callback delivery, supervisor receipt-to-action and pause. Model requests and tokens
come only from the existing request ledger. Supervisor elapsed time includes human
or model thinking/reading; it is not a CPU measurement. Parallel and nested intervals
can overlap, so cumulative durations must not be added to infer wall time.

Delivery completion, queueing, check/exam start/end, notification queueing, receipt
and review completion are persisted at transitions. Old exclusive `phase_seconds`
remain for compatibility; use `stages` for concurrent analysis. Missing history and
interrupted observations remain incomplete; no model summary supplies timestamps.

## Validation

```sh
python3 runtime/verify_check_overlap.py .artifacts/phase2-comparison.json
python3 runtime/verify_all.py .artifacts/phase2-regression.json --jobs 4
python3 -m unittest discover -s tests -p test_install.py
```

The comparison uses the same task/check, real Git/sandbox and an explicitly controlled
supervisor delay with offline model boundaries. It asserts early check start, one
check execution and retained exam/grade gates. It makes no live-model speedup claim.

The 3.10.1 NO_PROXY validation and checkpoint scheduler timing remain in place.
Bootstrap caching still binds source content, capability identity, scratch identity
and intact outputs; broad source invalidation is conservative. This release does not
redesign bootstrap cache granularity or task dependency graphs.
