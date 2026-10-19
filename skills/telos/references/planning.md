# Plan implementation and projects

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

The current check schema accepts `argv` as `['{python}', '-c', inline_code]`; that code may run appropriate project test commands in the verification snapshot. Verify the connected schema before constructing a contract. Resolve dependencies within the approved read-only paths. Command/check network access is off by default; a blocked dependency is not permission to relax isolation.

Create the contract with `telos_open` or `telos_project_open`; creation does not itself call the execution model. Start standalone work with `telos_send` or eligible project tasks with `telos_project_start`. Follow tool results and [acceptance](acceptance.md).

## Project completion

The supervisor reviews and accepts every child. Use project status/receive results to start eligible tasks; execution is bounded by the dependency graph and permissions. Retain accepted dependency commits as the baseline.

After child acceptance, use `telos_project_integrate` in the scratch worktree. Resolve conflicts only within authorized scope; `telos_project_resolve` records staged conflict resolution. Run `telos_project_check`, inspect `telos_project_review` and the exact integrated code, then call `telos_project_finish`. Child success is insufficient without final project `accepted=true`. The source branch is changed only by a separately authorized merge.
