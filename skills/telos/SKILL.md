---
name: telos
description: "Supervise Telos delegated development, scoped system audits (code, APIs, page interactions and visual evidence), and confirmed-bug repairs through MCP. Use for requests to use Telos, coordinate independent executors with evidence-based acceptance, or continue an existing TELOS_EVENT. Ordinary coding, general review and questions about Telos do not by themselves authorize starting an executor."
metadata:
  version: "3.7.7"
---

# Telos

The current conversation is the supervisor. Understand the user's goal, freeze useful acceptance criteria, delegate bounded work, and personally review the result. Telos manages isolated executor conversations and enforces its gates. The executor supplies work and evidence; the supervisor owns allocation, questions, review and acceptance.

## Route the request

- For advice, explanation or a proposed plan, answer at that level. Loading this skill does not start a task or authorize model calls.
- For delegated implementation, plan the scope using [planning](references/planning.md).
- For system walkthroughs, code/API/page audits or audit-to-repair work, also read [audits](references/audits.md).
- Before dispatching a new task, read [executors](references/executors.md) to reuse the installed configuration.
- For any completion event, repair loop or final acceptance, read [acceptance](references/acceptance.md). Resume the existing task rather than opening a replacement.

Read only the references relevant to this stage. Use the connected `telos_*` tool schemas for current arguments; this skill is workflow guidance, not another tool catalog. Explicit user instructions and already granted authorization take precedence over skill preferences; runtime constraints still apply.

## Establish the task

1. Determine whether the user wants implementation, investigation, repair, or a combination. Reuse known requirements, environment, repository and authorization. Ask only about missing facts that materially affect scope or correctness; continue independent preparation while waiting.
2. Verify that the Telos MCP tools are available to this conversation. If unavailable, report the missing integration and the concrete loading/configuration step. Do not substitute an unrelated subagent, start a different supervisor, invent a callback identity, or silently switch to manual delivery.
3. Inspect the target repository and relevant running environment before choosing checks. Telos requires a committed, clean source repository. Preserve existing edits: use an appropriate isolated clean checkout or explain the required preparation; never reset or commit unrelated changes to pass the gate. Telos also creates internal commits in its isolated worktree for acceptance. If the user prohibits commits, clarify this conflict before dispatch rather than assuming they meant only merge or push.
4. Choose single-task or project execution from dependencies and reviewable scope, respecting the user's choice and runtime limits. Record why. Define public task instructions separately from private questions, reference answers and executable checks before sending work.
5. Honor configured defaults unless the user requests an override. Announce the selected execution channel/model/effort and scope briefly. Do not infer that an installed profile has working credentials or that every advertised model supports tools.

## Keep the boundary visible

Give each executor its goal, correctness basis, relevant source/environment, allowed paths/actions, exclusions, evidence requirements and stopping conditions. Use only the scope needed for the task. A read-only audit does not authorize source repairs or unrestricted network access. Route findings outside scope back to the supervisor while continuing unaffected in-scope work.

Preserve these distinctions throughout the workflow:

- **Public checklist:** what the executor must investigate or implement, including expected business behavior and required evidence.
- **Private acceptance:** the supervisor's check implementations, reference answers, rubric and review packet. Never forward these to the executor; give requirement-level repair feedback instead.
- **Work completion:** an executor response or event. This is a request for review.
- **Acceptance:** actual checks and supervisor review satisfy the frozen contract, and the tool returns `accepted=true`. For a project, require project-level acceptance after integration.

Use real behavioral checks and task-specific questions. An audit is accepted for evidence-backed investigation, including valid defect findings; it does not certify a bug-free system. A closed-book answer supplements tests and source/evidence review.

## Close the loop

Follow the returned state and `next_action`. Receive events before acting, use the exact returned identifiers, respect `proceed=false`, and honor pause/cancel requests. Inspect pinned code, checks and artifacts yourself; view referenced screenshots for visual audits. Request bounded repairs when evidence or behavior is inadequate. Do not weaken criteria to finish, duplicate work on retries, or claim success after the budget is exhausted.

`telos_status`, `telos_receive`, `telos_review`, `telos_project_status`, `telos_project_receive` and `telos_project_review` return a compact summary by default. Pass `detail="full"` only when you need complete successful logs, raw receipts/attempts or static task prose. Summaries retain action identifiers, pinned commit/worktree/packet identity, all review questions/references/answers, every check outcome and blockers, so grade from the summary; full detail is not required for routine acceptance. Failed log snippets are bounded.

When a task freezes `task_budget`, all work, repair and exam attempts share it. `max_requests` is hard; token limits stop the next request after observed spend reaches them, so an in-flight response can overshoot. If required usage is unknown, stop further paid requests and report the unknown-budget state. Do not retry an exhausted budget as an infrastructure failure, reduce review rigor, or invent a currency estimate.

Use structured MCP errors (`error.code`, `retryable`, `retry_after_seconds`, `next_action`, optional `usage`) to diagnose failures. Retry metadata never overrides event ownership, pause/cancel, budgets or acceptance gates. Timing summaries distinguish cumulative request durations from lifecycle phases and project wall time; preserve incomplete/unknown markers rather than inventing missing history.

Report the inspected version/environment, accepted result or remaining blockers, evidence/worktree locations and unverified boundaries. Link confirmed audit findings to authorized repair tasks. Reuse verified repository experience when relevant. Treat merge, push and deployment as separate actions governed by the user's authorization; acceptance alone grants none.
