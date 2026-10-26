# Telos reasoning simulation

Created 2026-10-26 from the repository's Bailian Responses fixture and official API documentation.

## Purpose

`reasoning.sim.yaml` serves `../mocks/reasoning/default.js` in place of Bailian inference. It checks per-job effort forwarding through native CLI resume, private checks, closed-book evidence and final grading.

## Execution

1. `postman simulation run postman/simulations/reasoning.sim.yaml --port auto --no-history --output ndjson > reasoning.ndjson 2> reasoning.err`
2. Read the listening event's port, then run `python3 -B runtime/verify_reasoning_simulation.py --base-url http://127.0.0.1:<port>/reasoning/compatible-mode/v1`.
3. Inspect the verifier result and simulation request events; stop the simulation process with SIGINT.

## Assumptions

- The verifier redirects only the relay destination and supplies a synthetic credential; it isolates state/configuration and uses the native installed CLI. A source run therefore still needs that CLI and macOS sandbox support. (reasoned 2026-10-26, verifier and `test_effort_policy.py`)
- The mock models Responses envelopes and documented effort values, with fixed arithmetic answers. It cannot establish real provider availability, reasoning quality or speed improvements. (reasoned 2026-10-26, mock handler)

## Observations

<!-- begin:observations -->
2026-10-26: the reasoning member served native CLI work, resumed work and the same-thread closed-book exam. The verifier observed each selected effort upstream and completed actual checks and grading with accepted=true; no provider inference was called.
<!-- end:observations -->
