# Telos

**Turn coding goals into reviewed, testable results.**

[简体中文](README.zh-CN.md) · [Architecture](docs/architecture.md) · [Configuration](docs/configuration.md) · [MCP tools](docs/tools.md)

[![CI](https://github.com/pickupdwq/telos/actions/workflows/ci.yml/badge.svg)](https://github.com/pickupdwq/telos/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Platform: macOS](https://img.shields.io/badge/platform-macOS-111827)
![Python: 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB)

Telos is a local MCP bridge for Codex. Your original GPT flagship conversation acts as the **supervisor**: it defines the task, freezes acceptance criteria, reviews code and decides whether the result passes. A configurable **executor** implements the change, then answers a closed-book questionnaire in the same conversation.

There are two model roles. Each task has one executor; the original supervisor owns review and acceptance. Project mode can schedule several tasks with separate executors.

## System architecture

[![Telos layered architecture: control surface, orchestration, isolated execution, evidence gates and verified experience](docs/assets/architecture-en.png)](docs/assets/architecture-en.png)

The diagram maps logical capabilities within the local runtime. Only the supervisor and executor are model roles; the other boxes are program modules, storage or external inference interfaces.

| Layer | Main responsibilities |
| --- | --- |
| Control surface | MCP tool gateway, original-thread ownership, durable completion events and recovery |
| Orchestration plane | Immutable contracts, a finite-state machine (FSM), dependency DAG scheduling and bounded retries |
| Execution & verification plane | Persistent executor sessions, authenticated inference relay, filesystem isolation and evidence-bound acceptance |
| Artifact & experience plane | Git snapshots, private audit records and provenance-checked lesson retrieval |

[Full-size PNG](docs/assets/architecture-en.png) · [Vector SVG](docs/assets/architecture-en.svg) · [Module map and protocol details](docs/architecture.md)

## What it does

- **Configurable executor.** DeepSeek official API by default; Volcengine Ark Coding Plan, OpenAI API and locally configured Responses-compatible providers are supported.
- **Evidence before acceptance.** Frozen executable checks run against a pinned Git commit. The supervisor then reviews the code and grades the questionnaire. Default passing score: 90/100, with critical-question minima and no unresolved blockers.
- **Same-executor closed-book exam.** The executor keeps its work conversation, but receives no new source, private answers or test results. The relay disables tools and rejects tool calls during the exam.
- **Isolated work.** Git worktrees, macOS Seatbelt restrictions, private evaluation materials and a local credential relay separate implementation from grading.
- **Project coordination.** The supervisor chooses single, parallel or phased execution; dependencies and allowed write paths are enforced. Up to three jobs can run concurrently. Integrated code receives a separate final evaluation.
- **Recoverable operations.** Persistent events, receipts, bounded retries, pause/resume/cancel, and dry-run-first archive/cleanup tools.
- **Experience reuse.** Public repair lessons from accepted work can inform later tasks in the same repository. Provenance is preserved; stale lessons can be disabled. This is retrieval, not model-weight training.
- **System audits.** The supervisor freezes a public checklist and execution boundaries. Executors inspect read-only source, use constrained API/browser probes, and deliver reports, logs and screenshots. The supervisor reviews coverage/evidence; confirmed findings can become repair tasks. Audit acceptance concerns investigation quality, not absence of bugs. See the [audit guide](docs/audits.md).

Telos produces accepted commits in isolated worktrees. Merging, pushing and deploying remain separate actions.

## Requirements and compatibility

| Component | Requirement |
| --- | --- |
| OS | macOS with `/usr/bin/sandbox-exec`; Linux and Windows execution are not supported |
| Python | 3.12+; runtime uses the standard library only |
| Git | Installed and configured; target repository must have a commit and a clean working tree |
| Codex CLI | **0.163.0 is the tested baseline**; installer accepts newer versions, which need revalidation |
| Node.js / npm | Required to install/run the dedicated Codex CLI; Node.js 24 is used in CI |
| Supervisor | Authenticated Codex session using an allowed OpenAI GPT flagship model |
| Executor | A valid key/subscription for the selected provider and access to the chosen model |

Local verification has been run on Apple Silicon, macOS 27.0.1, Python 3.13 and Codex CLI 0.163.0. This does not establish compatibility with every Mac or later CLI release. The Desktop callback adapter depends on a versioned internal IPC protocol; use the dedicated CLI for the documented path and revalidate after upgrades.

## Quick start

The installer creates a **fresh installation**. It refuses to overwrite an existing Telos directory, launcher or MCP registration. Existing local deployments should keep their current installation until a migration is planned; cloning this repository does not upgrade them.

### Ask Codex to install

You can paste this into another Codex CLI session. Include the repository URL so Codex can find the correct project and installation instructions:

> Install Telos from https://github.com/pickupdwq/telos by following its README. Check dependencies, use the tested dedicated Codex CLI 0.163.0, register MCP, and run the offline installation verification. Preserve existing configuration and do not overwrite an existing installation. I will enter the DeepSeek key myself in the hidden local terminal prompt described in the README. Report the verification results and remind me to reopen Codex after registration.

- **Another CLI session on the same Mac, using the same Codex configuration:** reuse the existing installation. Reopen Codex and use `/mcp` to confirm that Telos is loaded.
- **Another machine:** it must be able to access this repository and meet the macOS, Python 3.12+ and dedicated CLI requirements above. Configure your own provider credentials and run the installation verification on that machine.

On **2026-10-18**, all six installer tests and a fresh-directory installation with offline verification passed on the Mac environment listed above. Checks covered the MCP handshake, 24 tools, trusted session binding, acceptance gates, native CLI callbacks and executor credential isolation. These checks use local model stubs; real API credentials, network access, other CLI versions and automatic return to a real Desktop conversation require separate validation. See [testing](docs/testing.md) for reproducible commands.

### 1. Clone and install the dedicated CLI

```sh
git clone https://github.com/pickupdwq/telos.git
cd telos
npm install --prefix "$HOME/.local/share/telos-cli" @openai/codex@0.163.0
```

This dedicated location is part of the sandbox configuration. A global `codex` command alone is not the documented installation path.

### 2. Install and register the MCP server

```sh
python3 install.py --register
```

This copies the runtime and [key-free example configuration](config.example.json), creates `~/.local/bin/telos` and registers `telos` with Codex. It preserves other MCP entries and model settings. Omit `--register` to install files only; `--dry-run` checks the installation plan without writing.

### 3. Add the executor credential

```sh
"$HOME/.local/bin/telos" auth deepseek
"$HOME/.local/bin/telos" doctor
```

Enter the key in the hidden terminal prompt. Credentials are stored locally with file mode `0600`, outside this repository. An existing matching environment/OpenCode credential can also be used. `doctor` checks configuration and credential availability, not whether a remote API key is valid.

### 4. Verify the local installation

```sh
python3 "$HOME/.local/share/telos/verify.py" --report telos-verification.json
```

This exercises MCP, native callbacks and executor isolation with a local model stub. It does not call paid models or validate real credentials. See [testing](docs/testing.md) for the full regression suite and optional live checks.

### 5. Start the supervisor in your project

```sh
# Run login if this Codex account is not already authenticated.
"$HOME/.local/share/telos/commands/codex" login
cd /absolute/path/to/your-clean-git-project
"$HOME/.local/share/telos/commands/codex" --model gpt-6-astra \
  -c 'model_reasoning_effort="max"'
```

Restart an already-open Codex client after MCP registration. Confirm that the `telos_*` tools are loaded. Telos binds tasks to the original Codex thread using trusted MCP metadata; arbitrary MCP clients without that metadata cannot act as an equivalent supervisor.

### 6. Ask for a task

> Use Telos to implement this feature: [requirements]. Use the default DeepSeek executor. Freeze relevant executable checks and the private questionnaire before sending work. Review the resulting code and test evidence, and continue until accepted or the configured budget is exhausted. Report the worktree and commit.

For a larger project:

> Use Telos to complete [goal]. Decide whether single, parallel or phased execution fits the dependencies. Define allowed paths for each task, reuse verified repository experience, and perform final integration checks before acceptance.

You do not need to manually sequence MCP calls. The supervisor uses the workflow described by the tools. A completed model reply or notification is **not** a passing result; only `accepted=true` means the gate passed.

For system audits:

> Use Telos to audit [requirements, repository, test environment and design references]. Choose the task decomposition, freeze the public checklist and execution boundaries, inspect code/APIs and key page interactions/layouts, review actual evidence, and report findings and unverified scope. This task is audit-only.

Page audits require an existing Playwright 1.48+ module, Chromium executable, explicit test targets, permitted actions and viewports. Telos does not install dependencies or use personal browser profiles. A dedicated Chromium headless shell is recommended. See the [audit guide](docs/audits.md) for configuration and repair handoffs.

## Models and reasoning effort

The installed example selects:

| Role / profile | Provider | Model | Reasoning effort |
| --- | --- | --- | --- |
| Supervisor | OpenAI / original Codex session | `gpt-6-astra` | `max` in the launch command above |
| Default: `deepseek_official` | DeepSeek official | `deepseek-flash` | `max` |
| Optional: `ark_coding_plan` | Volcengine Ark Coding Plan | `glm-5.3-flash` | `max` |
| Optional: `openai_executor` | OpenAI API | `gpt-6.1-sol` | `xhigh` (explicit example override) |

New executor bindings default to `max` unless explicitly overridden. The supervisor's effort comes from the original Codex session; Telos does not silently change it. Model availability and supported reasoning levels depend on the provider; unsupported settings fail rather than silently downgrade.

As of **2026-10-18**, DeepSeek documents `deepseek-flash` as the alias for DeepSeek V4.1 Flash 1017. It is a rolling alias, not an immutable dated pin. See the [official integration guide](https://api-docs.deepseek.com/quick_start/agent_integrations/).

To select another executor, tell the supervisor:

> Use Telos with `executor_profile=ark_coding_plan` for this task.

Profiles live in `~/.local/share/telos/config.json`. Each task freezes its provider, model and effort; changing defaults affects new tasks. Replacing an executor uses a new session or the constrained project reassignment flow. [Configuration details →](docs/configuration.md)

The supervisor may use your Codex login. The OpenAI **executor** uses a separate API credential; this release does not route executor requests through your ChatGPT/Codex subscription. Telos itself is free software, while provider inference and subscriptions may incur charges. Job limits are not monetary spending caps.

## How acceptance works

[![Telos execution protocol: frozen contract, work, executable checks, same-thread exam, supervisor grading and acceptance or repair](docs/assets/lifecycle-en.png)](docs/assets/lifecycle-en.png)

[Full-size PNG](docs/assets/lifecycle-en.png) · [Vector SVG](docs/assets/lifecycle-en.svg)

1. The supervisor freezes requirements, private references, question scores and executable checks.
2. The selected executor implements the task in its own Git worktree.
3. Telos snapshots a commit and runs the frozen checks. Failures return for repair before the questionnaire.
4. On passing checks, the **same executor thread** answers the questionnaire with tools disabled. Its earlier work history remains available.
5. The original supervisor inspects the pinned source and private evidence, scores answers and identifies blockers.
6. Telos enforces the threshold, critical minima, successful checks and unchanged evidence. Rejected work may be repaired within the round budget; accepted experience becomes eligible for reuse.

Default: at most five evaluation rounds, including the first. Infrastructure failures have a separate bounded retry policy. Closed-book answers supplement executable tests and source review; they do not prove correctness on their own.

## Operations and data

```sh
"$HOME/.local/bin/telos" status <session_id>
"$HOME/.local/bin/telos" pause <session_id>
"$HOME/.local/bin/telos" resume <session_id>
"$HOME/.local/bin/telos" cancel <session_id>
"$HOME/.local/bin/telos" recover
"$HOME/.local/bin/telos" cache --action inventory
```

Pause/cancel prevents follow-on stages; an already-running provider request may finish. Cancellation cannot be resumed. You can also ask the original supervisor to pause, resume or cancel a task/project.

| Location | Contents |
| --- | --- |
| `~/.local/share/telos/` | Runtime and key-free `config.json` |
| `~/.local/share/telos-cli/` | Dedicated Codex CLI dependency |
| `~/.config/telos/credentials.json` | Local provider credentials |
| `~/.local/state/telos/` | Private contracts, evidence, events and experience |
| `~/.cache/telos/` | Worktrees and isolated executor sessions |

`TELOS_STATE_DIR` / `TELOS_CACHE_DIR` override data locations. Legacy `exam_loop` paths and environment variables remain readable for compatibility. Use archive/cleanup tools before removing worktrees; do not delete task data while work is running. To disable MCP without deleting data, run `codex mcp remove telos` using the same Codex configuration, then restart the client.

## Boundaries

- Execution isolation currently requires macOS. It is a Seatbelt boundary, not a VM or protection against a compromised host.
- Command network access is off by default. Explicit `dependency_paths` grant read access; command/check network access must be deliberately enabled when needed.
- Repositories with `.codex/config.toml`, symlinked/special snapshot entries or unusual dependency layouts may need compatibility work. Telos does not silently bypass these checks.
- Source and task context are sent to your selected model providers. Private grading materials stay out of the executor conversation but are available to the supervisor. Treat local evidence and logs as potentially sensitive.
- Audit probes target explicitly designated local/test/staging environments. The Chromium driver blocks redirects, WebSockets, service workers and downloads; affected flows must be reported unverified. Model combinations do not necessarily outperform a single model.
- Desktop automatic return and provider compatibility need their own end-to-end verification; an available socket or successful `doctor` check is insufficient.

## Development

```sh
python3 -m unittest discover -s tests -v
mkdir -p .artifacts
python3 runtime/verify_all.py .artifacts/regression.json
```

The runtime verification command runs the macOS regression suite with local model mocks. See [CONTRIBUTING.md](CONTRIBUTING.md), [testing](docs/testing.md), [security](SECURITY.md) and the [changelog](CHANGELOG.md). Architecture assets are PNG/SVG files generated by [scripts/render_architecture.py](scripts/render_architecture.py).

## License

[MIT](LICENSE). The license covers this repository's code and documentation. Codex, model services and other external dependencies retain their own licenses and service terms. Telos is an independent project, not an official product of OpenAI, DeepSeek or Volcengine.
