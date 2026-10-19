# Audit a system and hand confirmed findings to repairs

An audit accepts the quality of an investigation within its declared scope. A correctly demonstrated defect can be a successful audit result. Never use passing audit quality checks to claim that all business behavior is correct.

## Establish the basis and boundary

Identify the repository version, local/test/staging environment, requirements or contract, relevant roles/data and design references. A source commit alone does not prove which version a running service uses; gather deployment/version evidence when that affects the conclusion. The current probe implementation does not support production audits.

Freeze the public `audit` boundary using the connected schema: source read paths, correctness requirements, exclusions, stop conditions, allowed test-data effects, checklist, target origins/path prefixes/methods, probe limits and any browser capabilities. Audit source stays read-only; reports and evidence go into the separate artifact directory. Use the smallest useful operation scope. Account for endpoint side effects, including GET requests.

Checklist kinds cover code, APIs, interaction and visual presentation. Select those needed for the user's goal. For each item, specify expected behavior, evidence and whether it is required. Required checks must be investigated; optional blocked/untested items need reasons and supervisor acknowledgement. Keep the public checklist distinct from the private quality questions and checks.

## Page interaction and visual correctness

Inspect the relevant business flow, not just a screenshot or HTTP 200. As applicable, check permissions, navigation, validation, submission, state changes, error/loading/empty states and the agreed desktop/mobile viewports.

With a design reference, compare against it. Without one, check legibility, consistency, clipping, overlap, overflow and usability; classify aesthetic preferences as suggestions. Preserve viewport and reproduction details for visual defects.

Use explicitly supplied existing Playwright/Chromium dependencies and allowed browser actions. Each probe starts a fresh context, so login and the operation belong in the same declared sequence. Personal browser profiles are not reused. The current driver blocks redirects, WebSockets, service workers, downloads and arbitrary executor JavaScript; label dependent flows blocked/unverified. Do not silently install dependencies, use an unrestricted browser or widen network access to make a probe pass.

An executor without image understanding can operate the probe and collect screenshots. The supervisor must actually view every captured screenshot referenced in the review packet before recording `visual_evidence_reviewed`. Geometry measurements and screenshot filenames do not establish visual correctness.

## Evidence and supervisor decisions

Require checklist-linked expected/actual behavior, reproduction steps, source location where applicable and captured evidence. Distinguish defects, suspected issues and suggestions. Mark missing coverage explicitly instead of converting it to a pass. API/page evidence must match the runtime-captured probe records; preserve request IDs on retries to avoid repeating side effects.

Private checks assess coverage, evidence and reasoning through the audit report, not whether the target system is defect-free. The current verification snapshot exposes `TELOS_AUDIT_REPORT` and `TELOS_AUDIT_ARTIFACTS` (project checks use `TELOS_AUDIT_REPORTS`). Do not invent the eventual defects or private reference answers in advance.

After receiving the event, evaluate and review through the normal [acceptance loop](acceptance.md). Read the report, logs and screenshots; independently corroborate important findings. In `audit_review`, decide every finding with a rationale, acknowledge limitations and record only screenshots actually inspected. State that accepted investigation does not establish overall system health.

## Authorized repair

Use existing authorization: an explicit audit-and-fix request can authorize subsequent repairs without another routine confirmation; an audit-only request ends with findings. Scope expansion or production changes require their own applicable authorization.

For linked repair, first obtain accepted audit evidence and supervisor-confirmed finding IDs. Create an implementation task with `repair_of` referencing that audit and those IDs, explicit write paths, a reproduction and relevant regression checks. In a project, add the corresponding dependencies. Keep suspected findings out of automatic repair until confirmed. Recheck affected end-to-end behavior after fixes and preserve any remaining limitations in the final report.
