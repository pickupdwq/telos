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
| Project `max_jobs` | 40 | Job budget, not a token/dollar cap |
| `task_budget` | unset | Optional default frozen usage budget for new tasks |

Profiles may override `http_timeout`, `job_timeout`, `exam_max_tokens`, `exam_format_attempts` and `credential_provider`. A task's `use_experience=false` disables experience injection for that task.

`task_budget` is an optional object with positive integer limits: `max_requests`, `max_input_tokens`, `max_uncached_input_tokens`, `max_output_tokens` and `max_total_tokens`. `telos_open` and each project task may override the configured default; an absent budget preserves legacy unlimited behavior. The budget is frozen into the task contract and all work, repair and closed-book exam attempts share one durable ledger outside the worker cache. `max_requests` is a hard reservation limit. Token limits are observed-spend thresholds: they stop the next request once spend reaches the limit, so an already in-flight response may overshoot. If usage needed to evaluate a configured token limit is unavailable or invalid, further paid requests stop and the task reports an unknown-budget error; no retry, effort reduction or gate weakening is allowed. These are token/request limits, not currency estimates.

Optional `exam_reserve_requests` and `exam_reserve_tokens` set aside capacity from `max_requests` and `max_total_tokens`. Each reserve requires its corresponding total limit and must be smaller than it. For example, `{"max_requests":12,"exam_reserve_requests":2}` permits at most ten work/repair requests and twelve requests overall. Both reserves are shared across attempts, persist on reassignment, and cannot be changed after the contract is frozen. Token reserves use observed usage and can be overshot by an in-flight response. A reserve does not make an incomplete work turn eligible for examination; completed work, checks and the same-thread exam remain required.

Optional `closeout_reserve_seconds` stops new work requests that many seconds before the frozen executor `job_timeout`; it must be smaller than `job_timeout`. Choose it from observed request latency and the task's stopping policy, before dispatch. Historical contracts without this field retain their original admission deadline. The deadline starts when the executor launches, after local setup and preflight. The executor receives the limit and its remaining work allowance, together with verified Python/cwd guidance. A completion window is not a guarantee that the model finishes. `job_timeout` still limits the executor process. On process exit or client disconnect, the relay blocks new calls and allows existing responses up to five seconds to finish accounting, then interrupts remaining reservations with unknown usage. Usage is parsed before forwarding response bytes; an unavailable final receipt is never replaced with zero. Errors preserve the local timeout alongside any unknown-budget failure. This cleanup cannot reconstruct usage lost in a historical run.

When available, a bounded `x-request-id` response header is saved as `upstream_request_id` for later reconciliation; it is distinct from Telos's internal request ID. Raw provider response bodies and credentials are not logged. An interrupted CLI-issued thread identity is retained for an authorized same-thread continuation, while the task remains unaccepted and all budget/turn gates still apply.

`dependency_paths` explicitly grants read-only access to required local dependency directories during work/checks. It does not permit writes or networking and is not granted during exams. `command_network=true` permits command networking during work; each private check has its own `network` field. Install dependencies through an appropriate workflow before launching isolated work; Telos does not automatically install a project's dependencies.

Project `allowed_paths` are literal relative files/directory prefixes, not globs. Avoid `.` unless the task really needs the whole repository. `.git` and parent traversal are forbidden. Whole-project checks inherit child dependency grants; project-level grants add read access only for final checks.

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
