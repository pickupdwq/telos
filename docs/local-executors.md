# Local executors / 本地执行者

Telos supports OpenAI-compatible loopback endpoints alongside DeepSeek official and Ark Coding Plan. Multiple endpoints can be saved; one is active at a time. The selected endpoint supplies one model ID to all new `local` tasks. Several tasks may use that same endpoint concurrently, subject to existing job limits. A local gateway may forward requests elsewhere; a loopback URL does not imply offline inference.

## Configure / 配置

```sh
# Chat Completions endpoint with authentication
telos local add gateway --url http://127.0.0.1:14000/v1 \
  --model your-model-id --protocol chat --key-env TELOS_LOCAL_API_KEY
telos local select gateway
telos auth local

# Another saved endpoint; omit --key-env only if it needs no authentication
telos local add workstation --url http://127.0.0.1:1234/v1 \
  --model your-other-model-id --protocol responses
telos local list

# Only workstation is now active; new tasks default to local execution
telos local select workstation --default
```

Use the actual model ID exposed by your service. IDs are configured rather than hardcoded; a changing model catalog does not rewrite existing tasks. `none` is the default effort and omits the upstream reasoning parameter. Set `--reasoning-effort high` or another supported value when needed. Unsupported explicit settings fail without a cloud fallback.

Acceptable hosts are `127.0.0.0/8`, `localhost` (normalized to `127.0.0.1`) and `[::1]`, with an explicit port. HTTP and HTTPS are supported; URL credentials, queries, fragments and traversal are rejected. Local requests bypass system HTTP proxies and never follow redirects. Remote/LAN URLs are outside this local-entry feature.

`telos auth local` prompts invisibly and saves a credential under `local:<endpoint-name>` in `~/.config/telos/credentials.json` with mode `0600`. Each saved key must match the endpoint's exact base URL. The explicitly configured environment variable takes precedence. Use a dedicated variable per endpoint if their keys differ. Cloud key variables and OpenCode credentials are never local fallbacks. Omitting `--key-env` explicitly selects unauthenticated requests, even if a saved key exists.

本地入口和火山、DeepSeek 是并列的执行渠道。`select` 只切换当前本地入口，`--default` 才将新任务默认执行者改为本地；原云端配置和 Key 不变。已有名称不能覆盖，改端口、协议或模型请新增名称后选择，避免改写旧任务。Key 通过终端隐藏输入，不能写进任务、配置示例或仓库。

### Local DeepSeek / 本地 DeepSeek

A gateway exposing `deepseek-flash` through Chat Completions can use the existing local adapter with `max` reasoning. Save a separate configuration to keep other model bindings intact, even when both use the same address:

```sh
# For a gateway that permits unauthenticated loopback requests
telos local add local-deepseek --url http://127.0.0.1:14000/v1 \
  --model deepseek-flash --protocol chat --reasoning-effort max

# Activate when choosing this model for new local tasks
telos local select local-deepseek
```

If the gateway requires a key, add a dedicated `--key-env` when saving the entry and use `telos auth local` after selecting it. Adding an entry alone does not select it. This remains `provider=local`; the `deepseek_official` profile continues to use its separate official endpoint and credential.

`max` is forwarded as Chat `reasoning_effort`; API acceptance does not prove a particular internal compute budget. Verify final text and tool calls, not HTTP status alone. A reply containing only `reasoning_content` without an answer/tool call is rejected; reasoning is never substituted for the final answer.

本地服务新增模型后，无需扩展硬编码模型列表。将 `deepseek-flash` 保存为独立名称，指定 `chat` 与 `max` 即可，仍然只启用一个本地配置。添加不会自动切换；DeepSeek 官方渠道和原模型配置继续保留。连通性检查必须包含实际输出，HTTP 200 但最终答案为空不能算调用成功。

## Choose an executor / 选择执行者

Tell the supervisor which profile to use:

| Request / 需求 | Profile / 配置 |
| --- | --- |
| 当前本地入口 | `executor_profile=local` |
| 火山 Coding Plan | `executor_profile=ark_coding_plan` |
| DeepSeek 官方 | `executor_profile=deepseek_official` |

This applies to implementation tasks, system audits and individual project tasks. Task parameters cannot supply an arbitrary URL or select a second local endpoint. To change a model, save/select an endpoint configuration with that model first.

Selection is blocked while a local job is scheduled or executing. Idle tasks retain their frozen endpoint, protocol and model. After a switch, they are blocked until you select their original configuration again. To replace an executor, create a new task or use `telos_project_reassign` with `executor_profile=local`; previous evidence remains preserved. Manual edits that change the active endpoint also fail validation before model requests.

同一时间只启用一个本地入口，不等于只能运行一个任务。原有并发限制继续生效。切换入口不会将旧任务或旧会话偷偷转发到另一个端口；续跑旧任务需要选回原配置，换执行者则新建或重新分配任务。

## Protocol boundaries / 协议边界

- `responses`: forwards to `<base_url>/responses`; remote compaction is disabled for local executors.
- `chat`: translates Responses history into `<base_url>/chat/completions`, preserves call IDs, function/custom tool calls and namespaces, then converts completed JSON replies into Responses events. Custom tools use a function whose argument is one `input` string. Upstream Chat requests are non-streaming.
- Text and inline/URL image content are supported when the selected model supports them. Hosted tools, encrypted reasoning and unsupported history types fail explicitly. Each model/server must support the tools used by the installed Codex CLI; a successful `/models` response alone is insufficient.
- Closed-book exams resume the same executor conversation. Top-level tools and history-based tool declarations are removed; returned tool calls and incomplete replies are rejected before reaching the CLI. The original supervisor still grades actual evidence. No automatic merge or deployment is added.

两种协议共享原来的任务边界、走查取证、闭卷考核和验收门禁。Chat 转换支持普通函数、自定义工具和命名空间；不声称兼容所有 OpenAI 扩展或所有模型。真实服务端必须单独验证工具调用和会话续接。

Protocol references: [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling), [Responses migration](https://developers.openai.com/api/docs/guides/migrate-to-responses).
