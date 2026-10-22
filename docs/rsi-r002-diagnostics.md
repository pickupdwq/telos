# R002 relay diagnostic preservation

## Scope

This repair is limited to diagnostics emitted by `peer_turn` when budget accounting
fails closed. It does not change admission, retry, recovery, provider, model, schema,
configuration, or sandbox policy.

## Behavior

When the relay exposes a bounded `diagnostics["failure"]`, `peer_turn` now appends it
to the fail-closed error for:

- unknown usage reported by `UsageLedger.ensure_known()`;
- an accounting `OSError` or `ValueError`;
- an existing relay budget failure.

The original executor or admission failure remains in the message. The complete relay
detail is first redacted with both the upstream credential and the relay token, then
capped at 600 characters. This prevents a credential prefix from surviving truncation
and prevents repeated replacement of a short credential from exceeding the cap. An
empty upstream credential is skipped rather than replacing every character boundary.
Without a relay diagnostic, the previous message remains unchanged. `BudgetExceeded`
snapshots, `thread_id`, and `execution_timeout` origin marking are preserved.

## Test evidence

Command:

```text
PYTHONPATH=runtime python3 -B -m unittest test_usage.PeerBudgetTests
```

Initial red before implementation: 10 tests ran, 5 failed. The failures showed that a
synthetic relay diagnostic disappeared behind the unknown-usage stop, was absent for
both accounting exception types, and was not available for secret-redaction checks.

Initial green after implementation: 10 tests ran, 0 failed.

Boundary-review red before the ordering repair: 12 tests ran, 2 failed. The cutoff test
found a credential prefix after truncation, and the expansion test found a sanitized
detail longer than 600 characters. Green after the ordering repair: 12 tests ran,
0 failed.

The focused tests are socket-free and cover synthetic relay diagnostics with unknown
usage, upstream and relay secret redaction, an empty upstream credential, both
credential-boundary cases, accounting `OSError` and `ValueError`, and unchanged legacy
output when no diagnostic exists.

## Limitations

This is diagnostic preservation only. It does not repair the upstream transport
outage, make unknown usage retryable, establish usage that the ledger does not have,
or authorize any recovery/admission change. The full native suite remains for
supervisor verification.
