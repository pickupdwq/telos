# Configuration / 配置

[English](../README.md) · [中文](../README.zh-CN.md) · [Complete example](../config.example.json)

## Defaults and profiles

The installer copies `config.example.json` to `~/.local/share/telos/config.json`. Edit that installed file, then restart the MCP process. Running `runtime/telos.py` directly without installing/copying this configuration uses legacy defaults and is not equivalent to the quick start.

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

Executor model, provider and effort are frozen per task. New bindings default to `max` when no override is supplied. An explicit effort is sent unchanged; provider support is not inferred. The supervisor's allowlist only checks its OpenAI identity and model; launch the supervisor with `model_reasoning_effort="max"` to set its effort.

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

Lookup order: matching environment variable, Telos local credential store, then an eligible OpenCode configuration/auth entry with a matching endpoint. The built-in OpenCode provider IDs are `deepseek` and `huoshancodingplan`. Credentials are resolved at request time and can be rotated without changing frozen task contracts.

The interactive command accepts no key argument and writes `~/.config/telos/credentials.json` with mode `0600`. Keep keys out of task arguments, configuration examples, command history and Git. The original supervisor uses its own Codex authentication; Telos does not copy that login to executor sessions.

## Additional providers

Only a locally configured, fixed **HTTPS Responses API** endpoint is supported by this extension mechanism. An arbitrary Chat Completions endpoint is insufficient.

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

Profiles may override `http_timeout`, `job_timeout`, `exam_max_tokens`, `exam_format_attempts` and `credential_provider`. A task's `use_experience=false` disables experience injection for that task.

`dependency_paths` explicitly grants read-only access to required local dependency directories during work/checks. It does not permit writes or networking and is not granted during exams. `command_network=true` permits command networking during work; each private check has its own `network` field. Install dependencies through an appropriate workflow before launching isolated work; Telos does not automatically install a project's dependencies.

Project `allowed_paths` are literal relative files/directory prefixes, not globs. Avoid `.` unless the task really needs the whole repository. `.git` and parent traversal are forbidden. Whole-project checks inherit child dependency grants; project-level grants add read access only for final checks.

## 中文要点

- 安装器将根目录配置示例复制为 `~/.local/share/telos/config.json`，修改后重启 MCP。直接运行未配置的源码会使用历史默认值，不能视为已完成安装。
- 用 `executor_profile` 选择执行者，或明确指定 `provider` 与 `model`；只改模型名不会自动切换服务商。新任务不支持独立考试模型。
- 每项任务冻结执行者绑定，默认强度为 `max`，显式值不静默降级。大总管模型由允许列表约束，强度由原 Codex 对话设置。
- 大总管使用 `openai_http` 等本机 OpenAI 通道名时，核对实际地址和认证配置后，在 `supervisor.provider_aliases` 中明确授权该名称。默认不接受别名；模型允许列表、原会话绑定和验收前身份复查继续生效。
- 凭据依次从环境变量、Telos 凭据文件、地址匹配的 OpenCode 配置读取。使用 `telos auth deepseek`、`telos auth ark` 或 `telos auth openai` 隐藏输入，禁止把 Key 写进任务和仓库。
- OpenAI 执行者需要自己的 API 凭据，不复用大总管的 ChatGPT/Codex 登录。
- 新增服务商需要固定 HTTPS Responses 接口，在本机配置，不能通过任务参数传任意地址；仅支持 Chat Completions 的接口不满足条件。
- 默认及格线 90、最多五轮评估；项目默认 40 个作业预算、最多三个并发。它们不限制实际费用或 token。
- `dependency_paths` 只开放依赖目录读取；`command_network` 和检查的 `network` 分别控制工作/检查的网络权限，闭卷阶段不获得这些权限。
- 旧 `EXAM_LOOP_STATE_DIR` / `EXAM_LOOP_CACHE_DIR` 仍兼容，同名 `TELOS_*` 设置优先；历史合同按原来的绑定执行，不强行迁移。
