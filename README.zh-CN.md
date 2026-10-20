# Telos

**让编码目标变成可测试、可审核的成果。**

[English](README.md) · [架构说明](docs/architecture.md) · [配置指南](docs/configuration.md) · [MCP 工具](docs/tools.md)

[![CI](https://github.com/pickupdwq/telos/actions/workflows/ci.yml/badge.svg)](https://github.com/pickupdwq/telos/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Platform: macOS](https://img.shields.io/badge/platform-macOS-111827)
![Python: 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB)

Telos 是运行在本机的 Codex MCP 桥接程序。原来的 GPT 旗舰对话担任**大总管**，负责理解需求、冻结验收标准、审核源码和最终评分；可切换的**执行者**负责实现功能，然后在同一个会话中闭卷回答问卷。

整个流程只有两类模型角色。每个任务只有一个执行者，审核与验收始终由原大总管负责。项目模式可以调度多个任务，每个任务有自己的执行者。

## 系统架构

[![Telos 架构：原始 GPT 大总管、本地 MCP 桥接程序和任务执行者；任务边界、用量控制、模型入口与证据验收](docs/assets/architecture-zh.png)](docs/assets/architecture-zh.png)

从左向右阅读：原大总管通过 Telos 分派任务，执行者交回的完成事件和证据返回同一聊天。下半部分展示运行时约束与验收门禁。只有大总管和执行者是模型角色。

| 组成 | 核心职责 |
| --- | --- |
| 原大总管 + Skill | 理解需求，冻结范围与检查，审核证据并评分 |
| 本地 Telos 运行时 | 环境预检、调度、隔离、模型转发、用量与预算、持久回传和验收门禁 |
| 任务执行者 | 在指定边界内工作，再于同一会话中完成闭卷问答 |

[查看高清 PNG](docs/assets/architecture-zh.png) · [矢量 SVG](docs/assets/architecture-zh.svg) · [模块映射与协议详解](docs/architecture.md#中文说明)

## 功能

- **执行者可切换。** 默认使用 DeepSeek 官方 API，同时支持火山 Ark Coding Plan、OpenAI API 和本地端口。可保存多个本地入口，同一时间只启用一个，兼容 Responses 与 Chat Completions。
- **凭证据验收。** 在固定 Git 提交上运行预先冻结的真实检查，大总管再审核源码、给问卷评分。默认 90 分及格，还必须满足关键题最低分、所有检查通过、无阻塞问题。
- **同一执行者闭卷答题。** 保留原工作会话记忆，不提供新的源码、私有答案或检查结果。转发层禁用工具，并拒绝考试中的工具调用。
- **隔离工作。** 使用 Git worktree、macOS Seatbelt、私有验收目录和本地凭据转发，划分实现与评估的权限。
- **项目协作。** 大总管选择单任务、并行或分阶段执行；程序约束依赖关系和可修改路径。最多同时运行三个作业，集成后还需整体验收。
- **可恢复执行。** 持久化事件、回执、有限重试、暂停/恢复/取消，以及默认只预览的归档与清理。
- **预检与用量控制。** 调用模型前检查工作环境，工具默认返回精简摘要，完整证据可补查；工作与考试的请求数和 token 用量持久记录。配置预算后约束后续请求，缺失用量保持未知状态。见[预算语义](docs/configuration.md)。
- **经验复用。** 从已通过验收的工作中提炼公开修复经验，用于同仓库的后续任务；保留来源，可停用过时经验。这是检索式积累，不是训练模型权重。
- **独立版本对比。** 通过 `telos compare` 冻结两个运行时和固定任务，复用当前大总管的执行、考试、审查流程，对比任务结果、用量和耗时。实验状态单独保存，不自动替换正式版本。见[评测与对比指南](docs/comparison.md)。
- **系统走查。** 大总管冻结公开清单和执行边界，执行者只读检查源码，通过受限探针验证接口、页面交互与布局，提交报告、日志和截图。大总管审核覆盖与证据，已确认问题可转成修复任务。走查通过表示检查质量合格，不表示系统没有 Bug。见[走查指南](docs/audits.md)。

Telos 在隔离工作区中产出已验收的提交。合并、推送和部署是后续独立操作。

## 环境要求与兼容性

| 组件 | 要求 |
| --- | --- |
| 操作系统 | 带 `/usr/bin/sandbox-exec` 的 macOS；暂不支持在 Linux、Windows 上执行任务 |
| Python | 3.12+；运行时代码只依赖标准库 |
| Git | 已安装并配置；目标仓库至少有一个提交，工作区必须干净 |
| Codex CLI | **已验证基线为 0.163.0**；安装器允许更高版本，但升级后需重新验证 |
| Node.js / npm | 用于安装、运行专用 Codex CLI；CI 使用 Node.js 24 |
| 大总管 | 已登录 Codex，并使用允许列表中的 OpenAI GPT 旗舰模型 |
| 执行者 | 所选服务商的有效凭据/套餐，以及对应模型的使用权限 |

本地已在 Apple Silicon、macOS 27.0.1、Python 3.13、Codex CLI 0.163.0 上验证，不代表所有 Mac 或后续 CLI 版本均已实测。桌面端自动回传适配依赖内部 IPC 协议，推荐先使用下方专用 CLI 路径；升级后重新验证。

## 快速开始

安装器会一起安装运行时和配套的**大总管 Skill**。全新安装仍拒绝覆盖已有文件；受管理的已有安装使用下方明确的 `--upgrade` 路径。仅克隆仓库不会升级正在使用的版本。

### 让 Codex 帮你安装

可以把下面这段话复制到另一个 Codex CLI 会话中。请附上仓库地址，方便 Codex 找到正确项目和安装说明：

> 请按照 https://github.com/pickupdwq/telos 的中文 README 安装 Telos 和用户全局 Skill。检查依赖，使用已验证的专用 Codex CLI 0.163.0，注册 MCP，并运行离线安装验证。已有安装先复用，除非我要求升级；保留现有配置和凭据。缺少的服务商 Key 由我在本地终端隐藏输入。完成后汇报验证结果，并提醒我在注册后重新打开 Codex。

- **同一台 Mac、同一份 Codex 配置下的另一个 CLI 会话**：复用已有安装即可。重新打开 Codex，用 `/mcp` 检查 Telos 是否加载。
- **另一台机器**：需要能够访问本仓库，并满足上面的 macOS、Python 3.12+ 和专用 CLI 要求。配置自己的模型凭据，并在该机器上运行安装验证。

安装器回归覆盖全新安装、受管理升级、配置保留、冲突检查和失败回滚。离线运行时检查使用本地模型替身；真实 API Key、其他 CLI 版本及真实桌面会话自动回传仍需单独验证。复现命令见[测试指南](docs/testing.md)。

### 1. 克隆仓库，安装专用 CLI

```sh
git clone https://github.com/pickupdwq/telos.git
cd telos
npm install --prefix "$HOME/.local/share/telos-cli" @openai/codex@0.163.0
```

该专用目录属于沙箱配置的一部分，不能仅用任意全局 `codex` 命令代替。

### 2. 安装运行时、Skill 并注册 MCP

```sh
python3 install.py --register
```

程序会复制运行时与[无密钥配置示例](config.example.json)，创建 `~/.local/bin/telos`，将 Skill 安装到 `~/.agents/skills/telos`，并向 Codex 注册 `telos` 服务。其他 MCP 与模型设置保留。Skill 对当前系统用户的各个项目可用，安装本身不会启动执行者。省略 `--register` 则只安装文件；加上 `--dry-run` 可检查计划而不写入。详见 [Skill 使用与安装](docs/skills.md)。

已有受管理安装，可在检出的新版源码中执行：

```sh
python3 install.py --upgrade --dry-run
python3 install.py --upgrade
```

升级保留现有配置、凭据、任务记录及启动入口，并保留运行时与 Skill 备份。检测到受管理文件被修改、Skill 冲突或任务正在运行时会拒绝升级。先暂停或完成活跃项目，等待作业及检查结束，升级后重新加载 MCP 客户端。自定义安装需沿用原来的 `--prefix`、`--bin-dir`，详见[升级说明](docs/skills.md#中文说明)。

### 3. 配置执行者凭据

```sh
"$HOME/.local/bin/telos" auth deepseek
"$HOME/.local/bin/telos" doctor
```

在终端隐藏输入 Key；凭据写入仓库之外的本地文件，权限为 `0600`。也可使用已有且地址匹配的环境变量或 OpenCode 凭据。`doctor` 检查配置和凭据是否存在，不验证远端 Key 是否有效。

### 4. 验证安装

```sh
python3 "$HOME/.local/share/telos/verify.py" --report telos-verification.json
```

使用本地模型替身验证 MCP、原生回传和执行环境隔离，不调用付费模型，也不验证真实凭据。完整回归及可选的真实服务商验证见[测试指南](docs/testing.md)。

### 5. 在项目中启动大总管

```sh
# 当前 Codex 账号尚未登录时执行。
"$HOME/.local/share/telos/commands/codex" login
cd /你的/干净Git项目/绝对路径
"$HOME/.local/share/telos/commands/codex" --model gpt-6-astra \
  -c 'model_reasoning_effort="max"'
```

注册后重新打开已运行的 Codex 客户端，确认加载了 `telos_*` 工具。Telos 通过可信 MCP 元数据绑定原始 Codex 会话；没有这类元数据的通用 MCP 客户端不能直接替代大总管宿主。

### 6. 用自然语言发起任务

> $telos 实现这个功能：[需求]。沿用已配置的默认执行者。开始前冻结有针对性的真实检查和私有问卷，完成后由你检查源码与测试证据，在预算内返工直到验收通过。最后告诉我工作区位置和提交。

较大项目可以这样描述：

> 使用 Telos 完成[目标]。由你根据依赖判断单任务、并行或分阶段执行，为每项任务指定可修改路径，复用仓库中已验证的经验，集成后做整体验收。

可显式使用 `$telos`，也可用自然语言要求使用 Telos。Skill 指导大总管规划、划定边界和审核，MCP 执行流程并落实验收门禁；普通咨询不会启动执行者。执行者回复完成、收到回调都不等于验收通过，只有 `accepted=true` 才算通过。

系统走查可以这样发起：

> 使用 Telos 走查这个系统：[需求、仓库、测试环境和设计依据]。由你拆分任务，制定走查问卷和执行边界，检查代码、接口及关键页面交互与展示，审核实际证据，输出问题清单和未验证范围。本次仅走查。

页面走查需要指定本机已有的 Playwright 1.48+ 模块、Chromium 可执行文件、测试地址、允许动作和视口；Telos 不会自动安装依赖或使用个人浏览器资料。推荐独立 Chromium headless shell，配置与修复衔接见[走查指南](docs/audits.md)。

## 默认模型与推理强度

安装配置示例选择：

| 角色 / 配置名 | 渠道 | 模型 | 推理强度 |
| --- | --- | --- | --- |
| 大总管 | OpenAI / 原 Codex 对话 | `gpt-6-astra` | 上述启动命令设置为 `max` |
| 默认：`deepseek_official` | DeepSeek 官方 | `deepseek-flash` | `max` |
| 可选：`ark_coding_plan` | 火山 Ark Coding Plan | `glm-5.3-flash` | `max` |
| 可选：`openai_executor` | OpenAI API | `gpt-6.1-sol` | `xhigh`，示例中显式覆盖 |
| 可选：`local` | 当前选中的本地入口 | 本机配置的模型 ID | 默认 `none`，不向上游发送推理参数 |

云端执行者绑定未显式指定时默认 `max`；本地入口默认不发送推理参数，可以显式配置。大总管的推理强度继承原 Codex 对话，Telos 不会暗中修改。模型权限和可用强度以服务商支持为准，不兼容时明确失败，不静默降级。

截至 **2026-10-18**，DeepSeek 官方文档将 `deepseek-flash` 对应到 DeepSeek V4.1 Flash 1017。它是滚动别名，不是永远固定的日期快照，见[官方集成指南](https://api-docs.deepseek.com/quick_start/agent_integrations/)。

切换执行者时告诉大总管：

> 这个任务使用 Telos，执行者选 `executor_profile=ark_coding_plan`。

配置文件位于 `~/.local/share/telos/config.json`。任务创建时冻结服务商、模型和强度，修改默认配置只影响新任务。更换执行者需要新会话，或使用受约束的项目重新分配流程。详见[配置指南](docs/configuration.md)。

本地入口通过 `telos local add` 保存、`telos local select` 切换，任务选择 `executor_profile=local`。火山与 DeepSeek 配置继续保留。已创建的本地任务绑定原地址和模型，切换后需选回原入口才能续跑。[本地执行者配置与示例](docs/local-executors.md)

大总管可以使用现有 Codex 登录；OpenAI **执行者**需要单独的 API 凭据，本发布版没有将执行者请求转接到 ChatGPT/Codex 套餐。Telos 本身免费开源，模型 API 与套餐按各服务商规则计费；作业数量上限不等于费用上限。

## 验收流程

[![Telos 执行时序：冻结合同、实现、真实检查、同会话闭卷、大总管评分及通过或返工](docs/assets/lifecycle-zh.png)](docs/assets/lifecycle-zh.png)

[查看高清 PNG](docs/assets/lifecycle-zh.png) · [矢量 SVG](docs/assets/lifecycle-zh.svg)

1. 大总管冻结需求、私有参考答案、题目分值和可执行检查。
2. 所选执行者在独立 Git worktree 中实现任务。
3. Telos 固定提交并运行检查，失败时先返工，不进入问卷。
4. 检查通过后，恢复**同一个执行者会话**闭卷答题，禁止工具；此前工作记忆仍保留。
5. 原大总管查看固定源码和私有证据，逐题评分、指出阻塞项。
6. 程序强制检查及格线、关键题最低分、真实检查结果与证据一致性。不通过可在轮数预算内返工；已通过工作的经验才有资格被复用。

默认最多五轮评估，包含首次。基础设施失败另有有限重试策略。闭卷回答补充真实测试与源码审核，单靠问卷不能证明代码正确。

## 日常操作与数据

```sh
"$HOME/.local/bin/telos" status <session_id>
"$HOME/.local/bin/telos" pause <session_id>
"$HOME/.local/bin/telos" resume <session_id>
"$HOME/.local/bin/telos" cancel <session_id>
"$HOME/.local/bin/telos" recover
"$HOME/.local/bin/telos" cache --action inventory
```

暂停/取消会阻止后续阶段，已经发出的模型调用可能完成；取消后不能恢复。也可直接向原大总管说“暂停/继续/取消这个 Telos 任务或项目”。

| 位置 | 内容 |
| --- | --- |
| `~/.local/share/telos/` | 程序与无密钥 `config.json` |
| `~/.local/share/telos-cli/` | 专用 Codex CLI 依赖 |
| `~/.agents/skills/telos/` | 当前用户跨项目可用的大总管 Skill |
| `~/.config/telos/credentials.json` | 本地模型凭据 |
| `~/.local/state/telos/` | 私有合同、证据、事件和经验 |
| `~/.cache/telos/` | 工作区及隔离执行者会话 |

`TELOS_STATE_DIR` / `TELOS_CACHE_DIR` 可覆盖数据路径；旧 `exam_loop` 路径和环境变量保留读取兼容。删除工作区前使用归档/清理工具，运行中不要删除任务数据。停用 MCP 可在同一 Codex 配置下执行 `codex mcp remove telos` 并重启客户端，不会删除任务数据。

## 已知边界

- 当前执行隔离依赖 macOS Seatbelt，不是虚拟机，也不能防御已被控制的宿主机。
- 命令默认不能访问网络；`dependency_paths` 只授予指定依赖目录的读取权限，需要时显式开启命令/检查网络权限。
- 含 `.codex/config.toml`、符号链接等特殊快照条目或特殊依赖布局的仓库可能需要兼容处理，程序不会自动绕过检查。
- 任务上下文与源码会发送给选定的模型服务商。私有评估资料不提供给执行者，但大总管可以读取；本地证据和日志也可能包含敏感信息。
- 走查探针仅支持明确指定的本地、测试或预发布环境；页面驱动支持 Chromium，阻止重定向、WebSocket、Service Worker 和下载。依赖这些能力的流程须标为未验证。组合模型不保证必然优于单模型。
- 桌面自动回传、服务商接口兼容需要单独端到端验证，不能仅凭 socket 存在或 `doctor` 成功判断。

## 开发与贡献

```sh
python3 -m unittest discover -s tests -v
mkdir -p .artifacts
python3 runtime/verify_all.py .artifacts/regression.json
```

第二项测试在 macOS 上使用本地模型替身运行完整运行时回归。参见[贡献指南](CONTRIBUTING.md)、[测试指南](docs/testing.md)、[安全说明](SECURITY.md)和[更新记录](CHANGELOG.md)。架构图以 PNG/SVG 提供，由 [scripts/render_architecture.py](scripts/render_architecture.py) 生成。

## 开源许可

采用 [MIT License](LICENSE)，覆盖本仓库代码与文档。Codex、模型服务及其他外部依赖仍适用各自许可与服务条款。Telos 是独立项目，并非 OpenAI、DeepSeek 或火山引擎的官方产品。
