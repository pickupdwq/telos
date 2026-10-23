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

Executor model, provider and effort are frozen per task. Cloud bindings default to `max`; local endpoints default to `none` (omit upstream reasoning parameters). An explicit non-none effort is sent unchanged; provider support is not inferred. The supervisor's allowlist only checks its OpenAI identity and model; launch the supervisor with `model_reasoning_effort="max"` to set its effort.

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
snapshot without requiring a named profile. Telos sends the selected effort
unchanged as `reasoning.effort`, including `none`; it does not translate it or
silently retry at a lower level. Available Telos values are `none`, `minimal`,
`low`, `medium`, `high`, `xhigh`, `max`. These are request values, not a claim of
seven distinct native reasoning levels: Bailian can map levels internally.
`max` and `none` were checked against this snapshot; other levels and their
quality differences need separate validation.

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
- Task `timing.phase_seconds` separates `idle`, `work`, `checks`, `exam`, `callback_wait`, `supervisor_wait`, and `paused`. Work/check/exam stages include their local setup. Callback wait ends at acknowledgement; supervisor wait includes review, repair decisions and the wait before the next action. It is elapsed waiting time, not a measurement of the supervisor's model inference. Pausing does not stop an already running job's clock; cancellation ends its clock when that job actually finishes.
- Project `timing.elapsed_wall_seconds` is time from creation to acceptance/cancellation (or now while active). `child_phase_seconds` sums retained executor assignments and can overlap across children; it must not be presented as project wall time. `integration_checks` separately reports completed final verification runtime, including failures, and identifies an in-flight verification. It does not include worker scheduling delay.
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
- 每项任务冻结执行者绑定，云端默认强度为 `max`，本地默认不发送推理参数，显式值不静默降级。大总管模型由允许列表约束，强度由原 Codex 对话设置。
- 大总管使用 `openai_http` 等本机 OpenAI 通道名时，核对实际地址和认证配置后，在 `supervisor.provider_aliases` 中明确授权该名称。默认不接受别名；模型允许列表、原会话绑定和验收前身份复查继续生效。
- 凭据依次从环境变量、Telos 凭据文件、地址匹配的 OpenCode 配置读取。使用 `telos auth deepseek`、`telos auth ark` 或 `telos auth openai` 隐藏输入，禁止把 Key 写进任务和仓库。
- OpenAI 执行者需要自己的 API 凭据，不复用大总管的 ChatGPT/Codex 登录。
- 本地入口使用 `local_executor`，兼容 Responses 和 Chat Completions，多地址只能选中一个；详见[本地执行者指南](local-executors.md)。其他新增服务商仍通过 `providers` 配置固定 HTTPS Responses 接口。
- 默认及格线 90、最多五轮评估；项目默认 40 个作业预算、最多三个并发。它们不限制实际费用或 token。
- 可为新任务或项目子任务冻结 `task_budget`（请求数与 token 上限）。请求数上限为硬预留；token 上限在已观测用量达到后阻止下一次请求。评测所需用量未知时停止后续付费请求，不重试、不降级。没有真实价格时不估算费用。
- `dependency_paths` 只开放依赖目录读取；`command_network` 和检查的 `network` 分别控制工作/检查的网络权限，闭卷阶段不获得这些权限。
- 旧 `EXAM_LOOP_STATE_DIR` / `EXAM_LOOP_CACHE_DIR` 仍兼容，同名 `TELOS_*` 设置优先；历史合同按原来的绑定执行，不强行迁移。
- `watch.auto_tmux` 默认开启；设为 `false` 可关闭自动分屏，保留日志和手动 `telos watch`。配置 MCP 环境转发后，在 Ghostty/iTerm2 的 tmux 中启动 Codex 即可使用[只读观察面板](watch.md)。
