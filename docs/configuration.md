# Configuration / 配置

[English](../README.md) · [中文](../README.zh-CN.md) · [Complete example](../config.example.json)

## Defaults and profiles

Fresh installation copies `config.example.json` to `~/.local/share/telos/config.json`; `--upgrade` preserves the installed file instead. Edit that installed file, then restart the MCP process. Running `runtime/telos.py` directly without installing/copying this configuration uses legacy defaults and is not equivalent to the quick start.

The bundled [supervisor Skill](skills.md) reuses this configuration. It contains no separate executor defaults, endpoints or keys. It does not change the original supervisor's model/effort, and loading it does not change the active local endpoint.

```json
{
  "default_executor_profile": "deepseek_official",
  "supervisor": {"provider": "openai", "models": ["gpt-6-astra"]},
  "profiles": {
    "deepseek_official": {
      "provider": "deepseek",
      "model": "deepseek-flash",
      "reasoning_effort": "max"
    },
    "ark_coding_plan": {
      "provider": "ark",
      "model": "glm-5.3-flash",
      "reasoning_effort": "max"
    }
  }
}
```

`executor_profile` can be selected on `telos_open`, each project task or `telos_project_reassign`. Alternatively specify `provider` and `model` explicitly. An explicit provider must agree with a selected profile. Passing only `model` keeps the default profile's provider; use both fields when crossing providers. No separate examiner profile is accepted for new tasks.

Executor model and provider are frozen per task. Effort stays fixed unless the new task freezes a supervisor-adjustable reasoning policy (below). Cloud bindings default to `max`; local endpoints default to `none` (omit upstream reasoning parameters). Fixed tasks preserve explicit effort values unchanged. The supervisor's allowlist only checks its OpenAI identity and model; launch the supervisor with `model_reasoning_effort="max"` to set its effort.

## Supervisor-selected reasoning effort

Set `dynamic_reasoning_enabled: true` in the installed configuration to enable dynamic reasoning for **new tasks whose exact provider/model/protocol capability is known**. The default is `false`; existing task contracts and queued project policies never change when this setting changes. No token limit is needed, and `task_token_limits_enabled` is independent. Global model/profile/initial-effort defaults are preserved.

The supervisor may explicitly opt a new task in with `reasoning_policy: {"allowed_efforts": ["low", "high", "max"]}` on `telos_open`, a project task or a meeting participant. Its initial `reasoning_effort` must be inside that range. For routine scoped implementation, the supervisor should normally choose `high` when supported; choose `max` for difficult reasoning and `low` for simple, well-specified work with direct checks. These are scheduling guidelines, not measured guarantees of speed or correctness.

Before a work, repair or exam job, supply both `reasoning_effort` and a concise `reasoning_reason` (1–1000 characters) to `telos_send`, `telos_repair` or `telos_evaluate`. Meeting follow-up messages accept the same pair. Omit both to inherit the last selection, or the initial effort for the first job. Running jobs cannot be changed. A project starts with each task's frozen initial effort; queued meeting follow-ups retain their recorded choice.

Policies freeze canonical levels, aliases, source and allowed range. Status exposes `reasoning_policy` and the current `reasoning_decision`; full task status includes `reasoning_history`. Every job records requested/effective/previous effort, reason, source, job identity and contract hash. The relay enforces that selection independently of worker-supplied parameters, and usage entries record the effective effort. Dynamic jobs disable remote compaction so every model request passes the same selection path. Local `none` retains its existing omit-parameter meaning.

Exams resume the same model and conversation using the selected **evaluation job** effort; the review packet binds its job/decision and transport evidence. The supervisor still reviews actual source/checks/answers; thresholds, ownership, pause/cancel, idempotency and acceptance gates remain unchanged. Reusing an event/request ID with a different choice is rejected. Changing providers requires reassignment/new contract, not editing task state.

The built-in dynamic range for `bailian:deepseek-v4.1-flash-1017:responses` is `low`, `high`, `max`. Aliases are `minimal → low`, `medium → high`, `xhigh → high`; `medium` is **not** a fourth native level. The [Responses API table](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-responses) documents the family mapping, and the [official Codex catalog](https://help.aliyun.com/zh/model-studio/codex) lists this exact snapshot. This opt-in range intentionally excludes disabling reasoning with `none`. Local simulation verifies forwarding and acceptance, not real inference quality at each level.

Unknown capability keys stay fixed. Explicitly requesting a dynamic policy for one fails before task creation. An operator can declare a separately verified capability locally (never through a task's prompt/arguments):

```json
"reasoning_capabilities": {
  "custom_provider:verified-model:responses": {
    "supported_efforts": ["low", "medium", "high"],
    "aliases": {},
    "source": "Provider documentation URL or dated local compatibility report"
  }
}
```

The key must match the resolved binding exactly, including `chat` for a local Chat adapter. A declaration must include its source, nonempty unique supported levels and direct aliases; it is an operator assertion, not an automatic capability probe. Never assume that a CLI enum or a model name proves upstream support.

If the original Codex conversation uses a locally named OpenAI provider such as `openai_http`, explicitly trust that provider ID in the installed configuration:

```json
"supervisor": {
  "provider": "openai",
  "provider_aliases": ["openai_http"],
  "models": ["gpt-6-astra"]
}
```

Only add an alias after checking that its effective Codex configuration uses the intended OpenAI endpoint and authentication. Aliases are local trust decisions, not inferred from a GPT model name or a provider-name prefix. Unlisted providers and models remain rejected, and Telos preserves and rechecks the original conversation's actual identity before grading. Omit `provider_aliases` to accept only `openai`.

## Planning meetings

`planning_mode` defaults to `multi_executor`, including upgrades whose preserved config omits this key. New MCP implementation/project creation requires a concluded `meeting_id`, or a concrete `skip_meeting_reason` for a trivial or explicitly directed exception. Set `planning_mode: "direct"` only to opt out globally. Existing frozen tasks continue unchanged.

The supervisor chooses the participant count and roles. `telos_meeting_open` defaults to multiple independent participants; `max_parallel` defaults to 3 and may be 1–3. Additional participants queue (up to 30 registered roles per meeting); the shared provider concurrency limit still applies. Follow-ups preserve the original conversations and can add or retire roles.

Meetings do not inherit `task_budget` and have no default token, turn or job limit. They retain request usage, per-call `job_timeout`, pause/cancel and original-owner callbacks. Source and Git are read-only; temporary experiments use the isolated scratch directory. A hashed public conclusion enters the later implementation contract, whose normal budgets, checks and acceptance remain separate. See the [supervisor meeting workflow](../skills/telos/references/meetings.md).

### Stop repeated attempts without progress

The durable convergence guard stops a stream after **3 consecutive attempts without new observable evidence**. Work compares Git-visible file contents (or the canonical audit report), evaluation compares newly passing check IDs, and meetings compare proposals, risks, questions and evidence across completed rounds and participants. Revisited content, new job/request IDs, changed summaries, timing/log noise and reasoning-effort changes do not reset the counter. Explicit requirement-clarification waits do not count as failed work. Existing sessions initialize observations lazily; historical attempts are not retroactively counted.

Project startup failures use a 10/20/40-second backoff and the same three-failure stop, including failures that refund the job reservation. Recovery scans do not restart blocked work. Status, compact receipts and meeting views retain `progress_guard` and the required next action. No artifacts are discarded and no task is marked accepted, paused or cancelled by this guard.

The original supervisor reports the blocker, then calls `telos_replan` with `session_id`, stable `request_id`, `blocker`, a different `approach`, supporting `evidence`, and `success_criterion`. A meeting-wide replan uses its meeting ID as `session_id`. Replanning permits one trial and retains the history; successful dispatch alone is not substantive work. After two adjusted trials without new evidence, automatic retries remain blocked and the supervisor must report the blocker for a user decision. Concluding a meeting and grading an already available review packet remain available. No token cap is introduced.

These are observable convergence signals, not a semantic proof of useful work: code churn or genuinely new but incorrect opinions still require supervisor review and the frozen acceptance checks. File evidence scans are bounded (256 MiB and 5 seconds after the Git listing); unavailable evidence is reported rather than treated as progress. Browser, executor and check timeout cleanup bounds process reaping and pipe draining, including retained pipes from detached/orphaned descendants. Unattributable orphan processes cannot be safely killed by ancestry alone; incomplete drain is reported instead of waiting indefinitely.

## Credentials

| Provider | Endpoint | Environment variable | Interactive setup |
| --- | --- | --- | --- |
| `deepseek` | `https://api.deepseek.com` | `DEEPSEEK_API_KEY` | `telos auth deepseek` |
| `ark` | `https://ark.cn-beijing.volces.com/api/coding/v3` | `ARK_API_KEY` | `telos auth ark` |
| `openai` | `https://api.openai.com/v1` | `OPENAI_API_KEY` | `telos auth openai` |
| `bailian` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `DASHSCOPE_API_KEY` | `telos auth bailian` |
| `local` | One active loopback endpoint | Dedicated `--key-env`, or none | `telos auth local` |

Lookup order: matching environment variable, Telos local credential store, then an eligible OpenCode configuration/auth entry with a matching endpoint. The built-in OpenCode provider IDs are `deepseek` and `huoshancodingplan`. Credentials are resolved at request time and can be rotated without changing frozen task contracts.

The interactive command accepts no key argument and writes `~/.config/telos/credentials.json` with mode `0600`. Keep keys out of task arguments, configuration examples, command history and Git. The original supervisor uses its own Codex authentication; Telos does not copy that login to executor sessions.

## Alibaba Cloud Bailian

The built-in `bailian` provider pins `deepseek-v4.1-flash-1017` at the **Beijing**
pay-as-you-go endpoint `https://dashscope.aliyuncs.com/compatible-mode/v1`.
It uses the native Responses API for work and the same-thread closed-book exam.
It has separate credentials; it never reuses the official DeepSeek or Ark key.

Fresh installs include `bailian_deepseek`. For an existing installation, upgrade
the runtime and merge this entry into `profiles` in the installed `config.json`,
preserving your default and other profiles:

```json
"bailian_deepseek": {
  "provider": "bailian",
  "model": "deepseek-v4.1-flash-1017",
  "reasoning_effort": "max"
}
```

Run `telos auth bailian` to enter the key privately, reload MCP, then select
`executor_profile=bailian_deepseek`. Explicit `provider=bailian` also selects the
snapshot without requiring a named profile. Fixed tasks send the selected effort
unchanged as `reasoning.effort`, including `none`; it does not translate it or
silently retry at a lower level. Available Telos values are `none`, `minimal`,
`low`, `medium`, `high`, `xhigh`, `max`. These are request values, not a claim of
seven distinct native reasoning levels: Bailian can map levels internally.
`max` and `none` were checked against this snapshot; other levels and their
quality differences need separate validation. Dynamic tasks instead use the explicit canonical range and alias mapping above.

The isolated executor includes a dedicated model catalog for the one-million-token
window and its tested shell/patch tools. Its effective input window is 80%, with
local history compaction starting at 700,000 tokens, to leave room before the
Responses API's documented truncation boundary. Remote `/responses/compact`
is disabled. The catalog is copied once per executor conversation and does not
alter the original supervisor's settings. Long-context compaction and visual
tasks are not established by a small coding smoke test.

For Bailian work requests, the relay explicitly sends `parallel_tool_calls=false`.
It also pairs complete historical tool-call batches with their matching results,
because Bailian requires each result immediately after its call. Missing,
duplicate or ambiguous results are rejected locally without a paid request;
other providers' history is unchanged. Closed-book exams still disable all tools.

API keys and model availability are region-specific. The shared Beijing domain
continues to accept keys from Beijing workspaces. Other regions or a dedicated
workspace URL require a separately named HTTPS provider under `providers`, a
matching key and independent executor compatibility validation; do not replace
the frozen built-in endpoint or send this key to another region.

百炼使用独立的北京入口与凭据，保留本地、火山及 DeepSeek 官方配置。通过
`executor_profile=bailian_deepseek` 选择，默认请求 `max`；工作、考试、用量统计、
预算和原大总管验收仍走原流程。Key 只保存在仓库外的私有凭据文件。

Official references (checked 2026-10-20):
[API keys](https://docs.bailian.console.aliyun.com/zh/model-studio/get-api-key),
[regional endpoints](https://docs.bailian.console.aliyun.com/zh/model-studio/base-url),
[snapshot capabilities](https://docs.bailian.console.aliyun.com/zh/model-studio/deepseek-v4-1-flash),
[Responses contract](https://docs.bailian.console.aliyun.com/zh/model-studio/qwen-api-via-openai-responses),
[Codex configuration](https://docs.bailian.console.aliyun.com/zh/model-studio/codex).
The live snapshot also accepted the CLI's freeform `custom` patch tool; this
compatibility was measured separately from the documented function-tool API.

## Local executor endpoints

Use `telos local add NAME --url URL --model MODEL --protocol responses|chat`, then `telos local select NAME`. Append `--default` to select local execution for new tasks by default. Endpoints and keys are separate from Ark/DeepSeek. See [local executor setup](local-executors.md) for multiple ports, credentials, frozen tasks and protocol limitations.

## Additional HTTPS providers

The `providers` extension mechanism accepts fixed **HTTPS Responses API** endpoints. Loopback Responses and Chat Completions endpoints use the separate `local_executor` configuration above.

```json
{
  "providers": {
    "other_vendor": {
      "base_url": "https://api.example.com/v1",
      "env": "OTHER_VENDOR_API_KEY",
      "wire_api": "responses",
      "models": ["your-model-id"]
    }
  },
  "profiles": {
    "other_executor": {
      "provider": "other_vendor",
      "model": "your-model-id",
      "reasoning_effort": "max"
    }
  }
}
```

Merge these entries into the existing configuration; replace the example endpoint/model with real values. Built-in provider endpoints cannot be overridden. No URL credentials, query strings or task-supplied endpoints are accepted. Test tool calling, thread resumption and closed-book behavior before using an additional provider for real work.

## Budgets and permissions

| Setting | Default | Meaning |
| --- | --- | --- |
| `threshold` | 90 | Frozen passing score, out of 100 |
| `max_rounds` | 5 | Evaluation rounds including the first |
| `max_turns` | 20 | Task execution-turn limit |
| `job_timeout` | 600 seconds | Worker job time limit |
| `http_timeout` | 180 seconds | Upstream request timeout |
| `exam_max_tokens` | 8192 | Exam output limit |
| `exam_format_attempts` | 3 | Bounded answer-format attempts; no private feedback |
| `experience_enabled` | true | Enable verified experience injection |
| Project `max_parallel` | up to 3 | Concurrent model jobs, including exams |
| Implementation project `max_jobs` | 40 | Job budget, not a token/dollar cap |
| `task_budget` | unset | Optional default frozen usage budget for new tasks |
| `task_token_limits_enabled` | true | Allow token limits when freezing new task budgets; false removes them |

Set `"task_token_limits_enabled": false` in the runtime configuration to disable cumulative token limits for new standalone tasks, project tasks and comparison trials, including explicitly supplied limits. This removes `max_input_tokens`, `max_uncached_input_tokens`, `max_output_tokens`, `max_total_tokens` and `exam_reserve_tokens` before freezing the budget. Request counts, request reserves and time limits remain; usage is still recorded. A budget containing only token fields becomes unlimited (`null`). Existing frozen contracts and ledgers retain their original limits, including queued project tasks and reassignments. Model context windows and per-response output limits such as `exam_max_tokens` are separate and unchanged.

本机配置设为 `"task_token_limits_enabled": false` 后，新任务不设置累计 Token 上限；即使创建任务时显式传入这些上限，也会在冻结合同前移除。仍记录 Token 用量并保留请求次数、超时与验收规则。历史任务的冻结预算保持原样。

Profiles may override `http_timeout`, `job_timeout`, `exam_max_tokens`, `exam_format_attempts` and `credential_provider`. A task's `use_experience=false` disables experience injection for that task.

`task_budget` is an optional object with positive integer limits: `max_requests`, `max_input_tokens`, `max_uncached_input_tokens`, `max_output_tokens` and `max_total_tokens`. `telos_open` and each project task may override the configured default; an absent budget preserves legacy unlimited behavior. The budget is frozen into the task contract and all work, repair and closed-book exam attempts share one durable ledger outside the worker cache. `max_requests` is a hard reservation limit. Token limits are observed-spend thresholds: they stop the next request once spend reaches the limit, so an already in-flight response may overshoot. If usage needed to evaluate a configured token limit is unavailable or invalid, further paid requests stop and the task reports an unknown-budget error; no retry, effort reduction or gate weakening is allowed. These are token/request limits, not currency estimates.

Optional `exam_reserve_requests` and `exam_reserve_tokens` set aside capacity from `max_requests` and `max_total_tokens`. Each reserve requires its corresponding total limit and must be smaller than it. For example, `{"max_requests":12,"exam_reserve_requests":2}` permits at most ten work/repair requests and twelve requests overall. Both reserves are shared across attempts, persist on reassignment, and cannot be changed after the contract is frozen. Token reserves use observed usage and can be overshot by an in-flight response. A reserve does not make an incomplete work turn eligible for examination; completed work, checks and the same-thread exam remain required.

Optional `closeout_reserve_seconds` stops new work requests that many seconds before the frozen executor `job_timeout`; it must be smaller than `job_timeout`. Choose it from observed request latency and the task's stopping policy, before dispatch. Historical contracts without this field retain their original admission deadline. The deadline starts when the executor launches, after local setup and preflight. The executor receives the limit and its remaining work allowance, together with verified Python/cwd guidance. A completion window is not a guarantee that the model finishes. `job_timeout` still limits the executor process. On process exit or client disconnect, the relay blocks new calls and allows existing responses up to five seconds to finish accounting, then interrupts remaining reservations with unknown usage. Usage is parsed before forwarding response bytes; an unavailable final receipt is never replaced with zero. Errors preserve the local timeout alongside any unknown-budget failure. This cleanup cannot reconstruct usage lost in a historical run.

When available, a bounded `x-request-id` response header is saved as `upstream_request_id` for later reconciliation; it is distinct from Telos's internal request ID. Raw provider response bodies and credentials are not logged. An interrupted CLI-issued thread identity is retained for an authorized same-thread continuation, while the task remains unaccepted and all budget/turn gates still apply.

`dependency_paths` explicitly grants read-only access to required local dependency directories during work/checks. It does not permit writes or networking and is not granted during exams. `command_network=true` permits command networking during work; each private check has its own `network` field. Install dependencies through an appropriate workflow before launching isolated work; Telos does not automatically install a project's dependencies.

Project `allowed_paths` are literal relative files/directory prefixes, not globs. Avoid `.` unless the task really needs the whole repository. `.git` and parent traversal are forbidden. Whole-project checks inherit child dependency grants; project-level grants add read access only for final checks.

## Timing and structured errors / 耗时与结构化错误

Status and review summaries expose timing without enabling any additional model calls:

- `usage.request_latency` reports cumulative `observed_seconds`, `measured_requests`, `missing_requests`, `in_flight_requests`, `complete`, and separate `work`/`exam` phases. Failed requests with measured duration are included; repairs count as work. Project usage aggregates the shared task ledgers, including prior executor assignments. `unavailable_sources` and `complete=false` preserve unreadable or incomplete child data. A zero observed total with missing requests does **not** mean zero actual time.
- Task `timing.phase_seconds` separates `idle`, `work`, `checks`, `exam`, `callback_wait`, `supervisor_wait`, `scheduler_wait`, and `paused`. Work/check/exam stages include their local setup. Scheduler wait measures authorized automatic continuation waiting for admission after a checkpoint or bounded protocol correction; it ends at job launch and excludes paused time. These waits do not count as executor work or supervisor review. Callback wait ends at acknowledgement; supervisor wait includes review, repair decisions and the wait before the next action. It is elapsed waiting time, not a measurement of the supervisor's model inference. Pausing does not stop an already running job's clock; cancellation ends its clock when that job actually finishes.
- Project `timing.elapsed_wall_seconds` is time from creation to acceptance/cancellation (or now while active). `child_phase_seconds` sums retained executor assignments and can overlap across children; it must not be presented as project wall time. `integration_checks` separately reports completed final verification runtime, including failures, and identifies an in-flight verification. It does not include worker scheduling delay.
- Timing schema v2 adds scheduler_wait. Legacy v1 counters remain readable and migrate on the next timing transition without resetting prior totals. scheduler_wait_tracked_since identifies the new metric's start; null means historical scheduling wait was not separately measured. Old work totals retain their original attribution.
- These metrics are observational, never budget or acceptance inputs. Lifecycle tracking starts with new tasks or the first observed transition of a historical task; missing history, process-loss intervals and clock regressions remain `complete=false`. Legacy terminal records without a finish timestamp have unknown wall time. Request latency measures relay elapsed time, not pure provider compute or first-token latency.

Project `usage` remains `null` when all children are on their first pending assignment with no session artifacts or ledger. Such children do not make a partially started project's aggregate incomplete. Missing ledgers for prior assignments or partially initialized tasks remain unknown rather than being reported as zero usage.

MCP failures retain `isError=true` and return the same JSON object in `structuredContent` and text content:

```json
{"error":{"code":"retry_backoff","message":"Infrastructure backoff active","retryable":true,"retry_after_seconds":12,"next_action":"retry_after_backoff"}}
```

Stable codes include `invalid_arguments`, `unknown_tool`, `owner_mismatch`, `source_dirty`, `continuation_stopped`, `event_receipt_required`, `event_not_actionable`, `job_running`, `review_required`, `retry_backoff`, `budget_exhausted`, `budget_unknown`, `budget_unavailable`, `execution_timeout`, `exam_format_error`, `infrastructure_error`, `gate_rejected`, and `internal_error`. Budget errors include the available usage snapshot, never the raw ledger. Unknown exceptions disclose their type, not arbitrary exception bodies. Retry metadata is guidance only: ownership, event idempotency, user control and frozen budgets remain authoritative; an uncertain failed operation must be reconciled before any retry.

Supplied argument types and enum values are checked before tool dispatch, including nested fields. An `invalid_arguments` message identifies paths such as `arguments.tasks[0].id`, without echoing submitted values or arbitrary map keys. Existing defaults, optional null handling and domain validation remain authoritative.

Project task views retain a `start_error` when automatic setup or dispatch fails before the child records an execution error. It includes the stable error code and recovery guidance; the task's `error` and `next_action` expose the reason directly. The diagnostic survives refresh, clears when execution advances or the task is reassigned, and cannot change eligibility or budgets. Paused/cancelled projects expose no task continuation action. A `ready` child can therefore have a blocked start; it does not establish that an executor is running.

Malformed initialization parameters return a JSON-RPC invalid-params response (`-32602`) while the MCP request loop stays available. Missing required arguments in experience/audit handlers return `invalid_arguments`; internal handler exceptions remain `internal_error`. Omitted project strategy, omitted cache inventory action and supported null options retain their defaults.

中文：请求累计耗时、任务阶段耗时、项目实际经过时间分开显示，失败请求及保留的重分派历史计入统计。缺失、损坏、执行中和历史未记录的数据明确标记，不把缺失值当作零耗时，也不把并行子任务耗时之和当成项目总时间。错误返回提供稳定错误码、是否可重试、等待秒数、下一步动作与可用预算快照；这些提示不能绕过原有权限、事件、暂停或预算限制。

尚未首次启动且没有执行记录的子任务不再误报用量缺失，重分派和不完整初始化的缺失账本仍保留警告。参数类型错误会给出具体字段路径，不回显提交的值或任意映射键。

项目子任务的 `start_error` 保留启动拦截原因和处理提示，状态刷新不会清空；执行推进或重分派后清除旧提示，暂停/取消时不提供继续动作。错误初始化请求会收到参数错误响应，不会终止 MCP；缺参错误与处理函数内部异常保持区分。

## Read-only executor observer

Activity recording is enabled for new execution attempts. `watch.auto_tmux`
defaults to `true`: a task launched from Codex CLI inside tmux opens or reuses
one right-side observer pane without changing focus. Set it to `false` to
retain logs and manual `telos watch` access without automatic panes.
This setting does not change model bindings, budgets, or acceptance gates.
See [observer setup](watch.md) for MCP environment forwarding, history,
task switching, and Ghostty/iTerm2 usage.

## 中文要点

- 全新安装将根目录配置示例复制为 `~/.local/share/telos/config.json`；`--upgrade` 保留现有配置。修改后重启 MCP。直接运行未配置的源码会使用历史默认值，不能视为已完成安装。
- 配套大总管 Skill 复用同一配置，不另存模型、入口或 Key，不改变当前大总管模型、强度或活动本地入口。
- 用 `executor_profile` 选择执行者，或明确指定 `provider` 与 `model`；只改模型名不会自动切换服务商。新任务不支持独立考试模型。
- 每项任务冻结执行者模型和供应商。启用 `dynamic_reasoning_enabled` 后，新任务可冻结受支持的强度范围，由大总管在每次执行前选择档位并记录理由；运行中不能改档，旧任务保持固定。百炼当前支持的动态档位为 `low/high/max`，`medium` 会明确映射为 `high`。全局初始档位、Token 设置和验收要求不变。
- 大总管使用 `openai_http` 等本机 OpenAI 通道名时，核对实际地址和认证配置后，在 `supervisor.provider_aliases` 中明确授权该名称。默认不接受别名；模型允许列表、原会话绑定和验收前身份复查继续生效。
- 凭据依次从环境变量、Telos 凭据文件、地址匹配的 OpenCode 配置读取。使用 `telos auth deepseek`、`telos auth ark` 或 `telos auth openai` 隐藏输入，禁止把 Key 写进任务和仓库。
- OpenAI 执行者需要自己的 API 凭据，不复用大总管的 ChatGPT/Codex 登录。
- 本地入口使用 `local_executor`，兼容 Responses 和 Chat Completions，多地址只能选中一个；详见[本地执行者指南](local-executors.md)。其他新增服务商仍通过 `providers` 配置固定 HTTPS Responses 接口。
- 默认及格线 90、最多五轮评估；项目默认 40 个作业预算、最多三个并发。它们不限制实际费用或 token。
- 可为新任务或项目子任务冻结 `task_budget`（请求数与 token 上限）。请求数上限为硬预留；token 上限在已观测用量达到后阻止下一次请求。评测所需用量未知时停止后续付费请求，不重试、不降级。没有真实价格时不估算费用。
- `dependency_paths` 只开放依赖目录读取；`command_network` 和检查的 `network` 分别控制工作/检查的网络权限，闭卷阶段不获得这些权限。
- 旧 `EXAM_LOOP_STATE_DIR` / `EXAM_LOOP_CACHE_DIR` 仍兼容，同名 `TELOS_*` 设置优先；历史合同按原来的绑定执行，不强行迁移。
- `watch.auto_tmux` 默认开启；设为 `false` 可关闭自动分屏，保留日志和手动 `telos watch`。配置 MCP 环境转发后，在 Ghostty/iTerm2 的 tmux 中启动 Codex 即可使用[只读观察面板](watch.md)。

## Checkpoints and project environments / 续做与项目环境

See [protocol, frozen policy, sandbox preflight and cache behavior](checkpoints-environments.md). New implementation tasks support checkpoint and blocked_environment; malformed replies are separate protocol errors. Optional auto_continue_checkpoints resumes authorized work, never acceptance. Environment requirements are explicit and optional; credentials and existing model/token settings remain unchanged.

### Proxy bypass declarations

environment_requirements.variables.NO_PROXY and no_proxy accept comma-separated
hosts, domain suffixes, IPv4/IPv6, optional ports, CIDR, wildcards and the empty
string. Example: localhost,127.0.0.1,::1,.example.com. Scoped IPv6 is not supported.
They are not HTTP URLs; URL syntax, credentials and control characters are rejected.
CIDR/wildcard/port matching still depends on the selected HTTP client.

Work and check environments receive the same effective value in both cases.
An explicit lowercase declaration wins if both cases are supplied, including an
empty string. Proxy endpoints retain their existing HTTP(S), credential-free URL
validation. These settings do not widen the task's frozen sandbox network access.


## Automatic frozen checks (3.11)

New implementation tasks may freeze `auto_run_checks=true`; shipped default false.
Queued project children freeze it at creation. Only valid completed work triggers
local checks; no model/exam/receive/grade is automatic. The original supervisor may
prepare/read source during checks, then receives the completion and calls evaluate.
Valid evidence is reused in the same round. Declare check `freshness` as `snapshot`
only for deterministic declared inputs, or `max_age` with `max_age_seconds` (1–3600).
Omitted/`never` or expired freshness permits one explicit same-round recheck; changed
or tampered evidence blocks. Old contracts retain their original workflow.
Inspect `automatic_checks` and `timing.stages`; distinguish cumulative/overlap time
from lifecycle wall time. Follow existing pause/cancel, capacity, budgets and
no-progress gates; never forward private check programs or raw failures.

## Project job-budget policy / 项目作业预算策略

`telos_project_budget` is the only audited entry point for changing a project's
effective job-admission ceiling. It is owner-only and currently supports only
`strategy="unlimited"`. It requires a stable `request_id`, `reason` and
`user_authorization`, and writes one versioned `job_budget_policy` plus an
append-only authorization history into the single atomic `project.json`. The
original `max_jobs`, `jobs_started`, frozen contract/hash, task identities and
history are preserved. Identical replays are idempotent; the same `request_id`
with different arguments is rejected. Projects without this policy keep their
original cap. A paused project stays paused; the call never starts, resumes,
accepts or deploys work. Accepted, cancelled, discussion and damaged-policy
projects are rejected. Status and compact/final-review views expose the
initial/effective ceiling, used jobs and authorization history.

An explicit `include_work_turns=true` call records only the separate versioned
`work_turn_budget_policy` scope and requires an already valid unlimited
`job_budget_policy` (the v1 object is preserved byte-for-byte). It binds
project, owner, contract, the original `max_turns` snapshot, current job count
and the prior v1-policy digest, with a scope-aware canonical argument digest
and append-only history. Request IDs are unique across both scopes; identical
replays write nothing, while changed scope/arguments or damaged records fail
closed. It exempts only the `max_turns` admission gate for current bound
implementation children of that project (explicit sends/repairs and automatic
checkpoint continuations, decided under the coordinator lock); exam rounds,
request/token budgets, no-progress, concurrency, timeouts, model settings,
pause/cancel and acceptance gates are unchanged. Counts keep incrementing
(20→21, jobs 62→63) with no reset. It may be recorded while paused but never
resumes or dispatches; standalone, foreign-project and discussion
participants are never exempt. Only an absent sibling key is legacy; an
explicit null is damaged.

中文：`telos_project_budget` 是唯一可审计的项目作业准入上限变更入口，仅原 owner
可用且当前只支持 `unlimited`。必须提供稳定的 `request_id`、`reason` 和
`user_authorization`；策略与只追加授权历史写入同一个原子 `project.json`，保留原
`max_jobs`、已用次数、冻结合同/哈希、任务身份与历史。相同参数重放幂等，异参重放
拒绝；未调用入口的项目保持原上限。暂停项目保持暂停，绝不自动启动、恢复、接受或部署。
已接受、已取消、讨论项目或损坏策略一律拒绝。状态及最终审阅展示初始/有效额度、
已用次数与授权历史。

Hardening: only an absent `job_budget_policy` key preserves legacy behavior.
An explicit null, unknown kind/version, missing or extra fields, mismatched
project identity, owner, contract hash, `max_jobs`/`jobs_started` snapshot or a
recomputed canonical argument digest is rejected as damaged. The owner is
re-verified from trusted MCP metadata while holding the coordinator lock;
`caller` is not an MCP argument. `request_id` with surrounding whitespace is
rejected. Status summaries report the current `jobs_started`; authorization
history retains the count captured at authorization time.
