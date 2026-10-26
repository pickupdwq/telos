# Multi-executor planning meetings

New delegated requirements default to a meeting before implementation contracts. The original conversation remains the supervisor and makes the final technical decision. Optimize for elapsed time and quality; do not impose a token-saving debate cap.

When dynamic reasoning is enabled/authorized, choose each participant's initial effort and allowed range from its role's difficulty. Targeted follow-up messages can select a new `reasoning_effort` with a concrete `reasoning_reason`; the queued selection survives retries and resumes the original thread. Follow [executor guidance](executors.md#choose-effort-before-each-job). Meeting opinions and effort levels never substitute for implementation acceptance.

## Choose participants

Inspect the requirement and scoped repository, then choose a variable number of concrete perspectives from affected domains, uncertainty, dependencies and risk. Use at least two independent participants by default. Do not always create three or split identical prompts into redundant roles. A cross-domain change may need more participants than the concurrency limit. Record the allocation in `decision_reason` and reuse configured executor profiles unless the user requests overrides.

For a trivial, clear change or explicit direct-work instruction, provide a concrete `skip_meeting_reason` on implementation creation. Do not ask for confirmation just to record an obvious exception. A deliberately single-participant meeting requires `single_executor_reason`. `planning_mode=direct` is an explicit installation-level opt-out; it is not the default.

## Run and converge

1. Call `telos_meeting_open` with the clean repository, public requirement/context and participants (`id`, `role`, optional executor selection). Creation makes no model call. Retain `project_id` as the meeting ID.
2. Call `telos_project_start(project_id=meeting_id)`. The first opinions run independently and concurrently. Current execution capacity is at most three simultaneous jobs, also subject to the shared per-provider limit. Additional participants queue. Participant count, discussion rounds, implementation task count and simultaneous jobs are different quantities.
3. Receive original-owner callbacks with `telos_project_receive`, or use `telos_project_status` for a manual meeting. The `discussion` object contains per-job reports and stable question IDs. Reports contain summary, proposal, risks, questions and evidence; verify consequential claims against source or experiments.
4. If disagreement can change correctness or the plan, call `telos_meeting_round` with a stable `request_id`, focused messages and `decision_reason`. Follow-ups resume the targeted participants' original conversations. Share relevant peer evidence or user answers in the messages. Additional experts can join through `add_participants`; stopped participants can retire through `retire_participants`. Never overwrite queued follow-ups or repeat a successful round under a new ID after a timeout; retry the same arguments and request ID.
5. Resolve technical questions using evidence. Ask the user for missing business decisions and wait for those answers; continue independent investigation meanwhile. Do not manufacture consensus. Repeated unsupported disagreement should trigger a concrete experiment, a better-qualified reviewer or a user decision, rather than another identical debate.
6. Conclude as soon as requirements, implementation, work ownership and verification are concrete. `telos_meeting_conclude` takes nonempty lists for `plan.requirements`, `implementation`, `workstreams` and `verification`, plus a rationale and answers for **every** recorded question ID, including earlier rounds. All active participants must have valid returned reports; no running or queued rounds may remain. Record why the chosen approach resolves the material objections; unanimity is not required.

Meetings have no default token, turn or job cap, even when a configured implementation `task_budget` exists. Usage still records actual requests/tokens. Per-call timeouts, concurrency, original-owner controls and pause/cancel remain enforced. Use `telos_project_control` immediately for a pause or cancellation request. An executor returns its report instead of waiting inside its process.

## Source, context and acceptance

Participants inspect isolated worktrees pinned to the same source commit. Source and Git metadata are read-only; experiments may write only to `TELOS_DISCUSSION_SCRATCH`/the isolated executor home. Do not grant network or implementation writes through a meeting. Participants cannot create subordinate agents or call MCP. Keep private checks, reference answers and evaluation material out of meeting context.

Concluding freezes a public plan and evidence hashes, closes meeting reminders and returns `discussion_complete=true`, **`accepted=false`**. Then call `telos_open` or `telos_project_open` with `meeting_id`, newly chosen implementation tasks and freshly frozen private acceptance. Telos attaches the plan and provenance and checks that the source repository and HEAD still match. A changed baseline requires reviewing that baseline in a new meeting.

Choose implementation parallelism from the actual dependency graph and file ownership; meeting participants do not automatically become implementation workers. Implementation sessions are separate, with their own persistent work/exam conversations. Follow [planning](planning.md) and [acceptance](acceptance.md). Never evaluate, grade or integrate meeting opinions as delivered code.
