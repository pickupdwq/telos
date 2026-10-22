# R004: Comparison wall-duration correctness

## Scope

This measurement-correctness change defines how `runtime/comparison.py` derives
wall durations for completed comparison trials. It does not change executor
speed, request/token accounting, provider measurements, outcome classification,
evidence hashing, or runtime-manifest verification.

## Timestamp contract

- A timestamp is usable only when it is an exact `int` (excluding `bool`) or a
  finite `float`. Strings and other numeric-looking values are not coerced.
- Negative epoch values are valid. A start after its finish is invalid and is
  reported as unknown before any rounding occurs.
- Two integer timestamps are subtracted as arbitrary-precision integers, so
  values beyond the IEEE-754 exact-integer range keep their exact difference.
- Mixed numeric or floating-point subtraction is accepted only while the
  duration remains finite. Overflow, infinity, NaN, missing fields, and
  reversed timestamps produce an unknown (`None`) duration.
- Known first and last decimal places are rounded to three places at the public
  boundary. Zero remains zero.

## Aggregate and report behavior

`trial_wall_seconds` is the rounded sum of the completed trials' raw durations.
If any completed trial has an unknown duration, the aggregate is unknown rather
than treating the missing value as zero. An empty completed set remains `0`.
Failed and cancelled terminal trials remain included exactly as before.

Each terminal JSON trial row uses the same duration helper, so `wall_seconds` is
`null` when the duration is unknown. The Markdown summary prints `unknown` for a
null aggregate. Historical records may omit `started_at`, `finished_at`, or
both; report generation still reads and hashes saved evidence and still relies
on the locked, manifest-hash-checked experiment state.

## Verification

Focused tests in `runtime/test_comparison.py` cover:

- exact `(2**53, 2**53 + 1)` subtraction;
- valid negative epochs and three-place fractional rounding;
- missing, reversed, boolean, string, infinity, and NaN timestamps;
- unknown aggregate propagation and the empty-set zero;
- report JSON/Markdown agreement while preserving the saved-evidence hash.

The worker check is limited to pure comparison unit tests. Native end-to-end,
listener, and full comparison workflow suites remain supervisor checks.
