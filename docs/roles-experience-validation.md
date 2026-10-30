# Roles and shared experience validation

Validated on macOS on 2026-10-30, against the 3.12.0 feature branch based on
`01b618fdaa8791d6bc771c4dd77d839979fca175`.

- `python3 -B runtime/verify_all.py --jobs 2 .artifacts/roles-experience-full.json`:
  **824 tests passed**, zero failures/errors/skips, 97.36 seconds.
- `python3 -B -m unittest discover -s tests -p test_install.py -v`:
  **22 tests passed**. Includes installing into an isolated home, launchers,
  configuration preservation, upgrade refusal during active work, the 33-tool
  MCP catalog, installed role discovery and shared-search dispatch.
- The bundled skill passed `quick_validate.py`; generated tool documentation
  was refreshed with `scripts/render_tools.py`. `git diff --check` passed.

Runtime coverage includes frozen roles for queued work and meetings, model
override precedence, legacy task/experience compatibility, original-supervisor
authorization, final project acceptance, source revocation, concurrent duplicate
publication, multi-source receipts, applicability matching, bounded context and
the exact references supplied to an executor. Existing private-material, real
check, closed-book exam, budget, concurrency and no-progress tests remain active.

The native role/experience test runs the real Codex CLI inside the macOS sandbox
against a local deterministic model endpoint. Its captured request contains the
assigned job and reviewed shared lesson, and excludes private references and
check programs. No paid provider or autonomous coding job was started for this
validation. This does not measure model quality or guarantee task success.

## Small-fixture timing sample

One accepted repository record and one published shared lesson; each lookup
sample has 20 repetitions and each task-creation sample has 10. Task creation
creates an isolated worktree but does not dispatch a model request.

| Operation | Median | Maximum |
|---|---:|---:|
| Baseline repository context | 8.253 ms | 8.976 ms |
| New repository context | 7.941 ms | 9.340 ms |
| New shared context, including source verification | 7.992 ms | 8.718 ms |
| New task without role/shared matching | 26.804 ms | 29.717 ms |
| New task with role and one shared lesson | 35.991 ms | 40.523 ms |

The shared context contained one complete lesson, 637 characters, with matching
reference IDs. There were zero provider requests. These observations support
roughly 9 ms additional creation work in this fixture, not a speedup or a claim
about large experience libraries. Full-corpus scanning and source validation may
cost more as history grows; no large-corpus performance claim is made.

Production installation is a separate step: use the existing installer upgrade
path only after active work has finished or its owner has explicitly paused it
and running jobs/checks have settled. Preserve current configuration, credentials
and historical records. Reload the MCP client and skill after installation.
