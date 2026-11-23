# Refactor an existing project / 老项目重构

“请使用 Telos 对这个项目进行重构” starts discovery, not permission to dispatch a
code change. Route natural-language equivalents here as well. For a question
about feasibility, answer it; do not create a project. Use this same project
flow with a single batch for a small refactor.

1. **Find the user's points.** Inspect the requested repository and its actual
   entry points and callers. Use user priorities, change history and concrete
   maintenance pain to rank candidates, not file size alone. If the repository
   or a consequential business choice is ambiguous, ask a focused question.
   Do this initial investigation in the original supervisor conversation.
2. **Capture old behavior.** On the original revision, run the useful existing
   checks and capture representative inputs/results, old stored data, failure
   paths and important public interfaces. Record pre-existing failures or
   unavailable evidence separately. Don't freeze an accidental bug as a
   requirement: ask before changing visible behavior. Use an isolated clean
   checkout if the original contains ongoing edits.
3. **Prepare the proposal.** Use `telos_project_open(refactor_plan=...)`. Supply
   concrete public points under all four keys: `scope` (priorities, included and
   excluded areas), `preserve` (behaviors the user relies on), `improve` (the
   maintenance problem and observable improvement), `verify` (how to compare
   the old and new outcomes). Split by reviewable behavior, with literal paths
   and real dependencies. Freeze appropriate checks for each batch and for
   the complete project. The public brief goes to each executor; keep private
   test programs, reference answers and credentials out of it.
4. **Confirm before dispatch.** Present those points and the batches in plain
   language, with the exact `refactor.confirm_text` in the final question. For
   example: “这次只收拢同步状态判断；旧设备的状态和接口不变。用同一批旧数据前后各跑
   一次，再验证新增状态只需改一处。这些点对吗？确认请回复：确认重构 …” Use the
   returned code, never the ellipsis. Wait for the actual user. Where the
   client exposes the immediately preceding assistant message, an unqualified
   “好” or “确认” in reply to this one card also works. Otherwise use the displayed
   reply. Silence, the original request, a tool result, an executor opinion or
   an answer with amendments is not confirmation. If they correct scope,
   revise and show the corrected proposal; retire a superseded pending plan.
   Query `telos_project_status` to recover the same pending proposal after a
   reconnect. Only then call `telos_project_start(project_id, confirm_refactor=
   <full revision>)`. No need to reconfirm unchanged batches or routine retries.
   Substantive additions or a changed handoff require the new revision.
5. **Check both promises and finish.** Run each batch against the captured
   behavior and the stated maintainability goal. Resolve an introduced
   regression in its batch. Integrate and rerun the critical paths with all
   changes together; review the final diff and actual outputs before project
   acceptance. Report scope covered, what stayed compatible, which maintenance
   problem improved, and any unverified cases. Separate schema migrations,
   breaking public changes and deployment from the approved refactor.
