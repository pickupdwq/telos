# Reuse the installed executor configuration

The Telos runtime is the source of configuration truth. This skill contains no model defaults, endpoint addresses or API keys. Use the known installation launcher (normally `telos`) and `doctor` for version, default executor, capabilities and credential-availability diagnostics. `doctor` is not a live inference test. If necessary, inspect only non-secret configuration fields from the installed `config.json`; do not dump credentials or environment values into the conversation.

Use the existing `default_executor_profile` when the user does not choose a channel. Available configured profiles can include `deepseek_official`, `ark_coding_plan`, `openai_executor` and `local`; inspect the installed profiles instead of assuming all are present or authenticated. A profile name selects the runtime policy, model and default effort. A new task can use an explicit `reasoning_effort` supported by its upstream. A model list alone does not demonstrate tool-call, history or effort compatibility.

For local execution, `telos local list` shows saved endpoints and the single active entry. All new local tasks use its configured model and protocol. Multiple tasks may share this one entry within job limits. Use `telos local add` / `select` only when configuration changes are part of the user's request. Active jobs block switching; existing tasks retain their original endpoint/configuration and need that entry selected to resume. A new/reassigned task is required to replace its executor. Never work around the binding check by editing private task state.

Local `none` omits the upstream reasoning parameter; it does not prove that reasoning is disabled. Read supported effort values from the current tool schema. Inference endpoints may support different subsets; report unsupported explicit settings instead of silently downgrading or using another provider. The supervisor's own model and effort belong to its existing host session; Telos checks identity but does not set them through executor settings.

## Choose effort before each job

When the user authorizes dynamic reasoning or the installed configuration enables it, select an initial effort and freeze `reasoning_policy.allowed_efforts` when opening a task, project child or meeting participant. Inspect the returned policy for this exact executor. Unknown capabilities remain fixed; a tool enum is not evidence that a provider supports every listed level. Do not edit capability declarations merely to bypass a rejection.

Choose from the **canonical** allowed levels based on the next job's actual work:

- Use `high` for ordinary implementation with clear scope, known interfaces and concrete checks, when supported.
- Use `max` when unresolved design constraints, difficult root-cause reasoning or interacting changes justify the extra reasoning.
- Use `low` for simple, well-specified inspection or mechanical changes with direct verification. Use `medium` only as a distinct level when the frozen policy actually supports it; aliases can make it equivalent to `high`.

Pass `reasoning_effort` and an evidence-based `reasoning_reason` on `telos_send`, `telos_repair`, `telos_evaluate`, or a targeted meeting-round message. Omit both to retain the previous selection. Decisions apply to the next job, never an in-flight call; keep the same session/thread. Initial project jobs use each task's frozen initial effort. Explain a choice in terms of complexity, uncertainty and observed failures, not a claim that review compensates for all worker mistakes.

Before escalating after failure, distinguish incorrect reasoning from unclear requirements, missing context, transport errors and failing tools. Repair the cause; raising effort cannot fix all of them. Reduce effort only when the next scoped job warrants it. Do not oscillate without new evidence, silently change model/provider, weaken acceptance or add token caps. Review actual checks and code at every level. Assess optimization by total time to accepted delivery and rework, not a single response's speed.

Status shows requested/effective effort and the decision reason; full status retains history. The evaluation packet records the exam job's decision and transport binding. Treat documented alias mapping separately from real-model performance: parameter forwarding tests do not establish equal quality at lower effort.

Credentials are resolved by the runtime from configured environment references, its private credential store, or matching existing provider configuration. Use interactive `telos auth <provider>` when a key must be supplied. Never ask for a key in chat or place it in task arguments, prompts, skill files or the repository.

Keep this skill and its companion runtime at the same installed release. If tools or behavior do not match this workflow, report the versions and use the installation's supported upgrade procedure. Do not modify runtime configuration or install another tool simply because the skill was loaded.

## Checkpoints and project environments (3.10)

New implementation work uses protocol v2. completed means ready for the existing
receive/evaluate/review/grade flow. checkpoint means unfinished authorized work:
do not ask the user whether to continue routine work, run authorized checks or fix
in-scope defects. blocked_environment requires specific environment evidence and
repair actions. needs_clarification is reserved for genuine missing business,
scope, permission or requirement decisions; format errors are protocol_error.

When auto_continue_checkpoints is frozen true, the runtime resumes the same
executor through its auditable internal scheduler. Honor pause/cancel, budgets,
no-progress and uncertain-result diagnostics. Never fabricate a receive receipt,
create a replacement task to reset limits, automatically evaluate/grade, or
silently alter frozen contracts. A completed submission still requires the
original supervisor's review. Legacy contracts preserve their old behavior.

Declare only relevant environment_requirements before creating a task/project:
exact tool paths, lock/config files, read-only download sources, necessary local
services, browser capabilities and already-authorized bootstrap commands.
dependency_paths and network permissions must be explicit. Reuse public sandbox
preflight evidence; check environment diagnostics before asking the user to
troubleshoot. Use TELOS_CHECK_OUTPUT and TMPDIR and independent writable state;
formal checks read immutable source. No automatic new dependencies or secret
values in requirements/context. Do not change model, dynamic reasoning or Token
settings merely to use checkpoint/environment support.
