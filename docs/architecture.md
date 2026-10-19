# Architecture / 架构

[English README](../README.md) · [中文 README](../README.zh-CN.md)

[![Layered system architecture](assets/architecture-en.png)](assets/architecture-en.png)

## Roles and deployment model

The original Codex conversation is the GPT supervisor. It owns task planning, private references, source review, scores and final acceptance. Telos is a deterministic local coordinator, not another model. Each task has one executor binding, frozen when the task is opened.

The architecture diagram is a **logical capability map**, not a deployment of independent microservices. One local Python application coordinates Codex subprocesses, a per-job inference relay, filesystem state and Git worktrees. Model providers are external HTTPS dependencies. The lifecycle diagram below shows the execution order; the module boxes do not represent additional model participants.

## Architecture layers

| Layer | Responsibilities | Contracts and boundaries |
| --- | --- | --- |
| **Control surface** | Expose 25 tools over MCP stdio/JSON-RPC; bind the original supervisor; route completion events | Trusted `threadId` and supervisor identity; CLI queue or versioned Desktop IPC |
| **Orchestration plane** | Freeze acceptance criteria and provider policy; enforce a finite-state machine (FSM); schedule the project dependency DAG | Immutable contract hash; frozen provider/model/effort; allowed write paths; round and concurrency budgets |
| **Execution & verification plane** | Run work and closed-book phases in one executor thread; relay inference; run private checks; enforce acceptance | Phase-specific capabilities; fixed upstream endpoint; pinned source commit and evidence packet |
| **Artifact & experience plane** | Store isolated worktrees, snapshots, private evidence, receipts and reusable lessons | JSON state with filesystem locks; Git identity; provenance checks; verified archival before cleanup |

The FSM constrains legal task transitions. A DAG is a directed acyclic graph of project dependencies. The acceptance gate is deterministic code that enforces the supervisor's frozen criteria. Provenance links a reusable lesson to the accepted work that supports it. These terms describe implemented behavior; they do not imply a database server, external message broker or model-weight training.

## Execution protocol

[![Execution lifecycle and acceptance protocol](assets/lifecycle-en.png)](assets/lifecycle-en.png)

The sequence condenses supervisor MCP calls and their responses. In particular, the original supervisor acknowledges completion events with `receive`; the executor does not grade or acknowledge its own acceptance.

`open → send → completion event → receive → evaluate → completion event → receive → review → grade`

`evaluate` pins the submitted commit and runs executable checks before the questionnaire. Failed checks return to repair. Once checks pass, work and exam resume the **same executor thread** with different capabilities:

| Capability | Work | Closed-book exam |
| --- | --- | --- |
| Conversation | Persistent executor thread | Same thread and prior work history |
| Tools | Scoped execution tools | `tools=[]`, `tool_choice=none`; tool-call responses rejected |
| Filesystem | Task worktree; explicitly granted read-only dependencies | Project source and dependencies excluded |
| Newly supplied context | Public task brief, public repair guidance, eligible lessons | Question IDs and prompts only |
| Private grading material | References and private checks withheld | No new references, rubric, check results or source |
| Model policy | Frozen provider/model/effort | Same binding; exam compaction disabled |

The supervisor reviews the private evidence packet and pinned source, then supplies question scores, blockers and optional public lessons. Telos enforces all acceptance invariants together:

- Every frozen executable check passed.
- The total score reaches the configured threshold, default **90/100**, and each critical question reaches its minimum.
- No unresolved blockers remain.
- The pinned commit, contract hash and evidence packet still match the reviewed state.

Failure returns public repair guidance to the same executor within the round budget, default five evaluations including the first. Infrastructure retries have separate bounds and cannot accept a task. Passing produces an accepted commit and evidence; merging, pushing and deploying remain separate actions.

## Durable events and recovery

Completion is recorded in a durable outbox and tied to the original supervisor thread. `event_id` identifies the event, `receive` records its receipt, and action IDs deduplicate follow-on operations. Recovery reconciles persisted state, process bookkeeping and delivery evidence. An uncertain dispatch is not blindly resent; these safeguards do not claim exactly-once model execution.

The CLI queue and Desktop IPC are distinct callback adapters. Desktop IPC depends on an internal, versioned client protocol, and real Desktop automatic return still needs end-to-end verification. A reachable socket or successful configuration check does not establish that result. Pause/cancel blocks new stages while already-running provider requests may finish.

## Module boundaries

| Module | Responsibility |
| --- | --- |
| [telos.py](../runtime/telos.py) | MCP catalog, task FSM, durable events, snapshots and acceptance gate |
| [supervisor.py](../runtime/supervisor.py) | Original supervisor identity and GPT model allowlist |
| [providers.py](../runtime/providers.py) | Provider policy, frozen model bindings and host credential lookup |
| [executor.py](../runtime/executor.py) | Persistent Codex executor session; work/exam capability changes |
| [closed_book.py](../runtime/closed_book.py) | Tool-free exam request/response enforcement and answer parsing |
| [check_runtime.py](../runtime/check_runtime.py) | Private executable checks against pinned snapshots |
| [isolation.py](../runtime/isolation.py) | Read allowlists and per-job authenticated inference relay |
| [projects.py](../runtime/projects.py) | Dependency DAG, write scopes, scratch integration and final project gate |
| [codex_rpc.py](../runtime/codex_rpc.py), [desktop_rpc.py](../runtime/desktop_rpc.py) | CLI queue and versioned Desktop IPC adapters |
| [experience.py](../runtime/experience.py) | Provenance, verified lesson selection and disabling stale records |
| [storage.py](../runtime/storage.py), [processes.py](../runtime/processes.py) | Archive/cleanup safeguards and process bookkeeping |
| [identity.py](../runtime/identity.py), [exam_loop.py](../runtime/exam_loop.py), [legacy_exam.py](../runtime/legacy_exam.py) | Product identity and historical contract compatibility |

The flat runtime directory is intentional: it is copied as a self-contained standard-library application, with its regression and installation probes adjacent to the modules they verify. Packaging and documentation live outside this directory.

## Trust and storage boundaries

The bridge retains upstream credentials and forwards requests only to the frozen HTTPS Responses endpoint. Executor requests use an ephemeral local relay token; shell environments exclude upstream keys and relay tokens. Source worktrees, private contracts and other executors' caches have separate permissions. Explicit dependency grants are read-only. Normal command network access is disabled by default, while model traffic goes through the relay.

SSE accounting is incremental and byte-exact: only the current event is retained, capped at 16 MiB (the same bound as other accepted response bodies). Events split across arbitrary chunks, including CRLF and multiline `data:` fields, are parsed safely. An event over the cap is discarded; if that was the terminal event, usage remains unknown, the request is recorded as failed, and a configured token budget stops further paid requests. Total stream length is never buffered.

Private contracts, evidence, events and lessons live under `~/.local/state/telos/`. Worktrees and isolated executor homes live under `~/.cache/telos/`. JSON files and filesystem locks persist state; no database or message broker is required. Cleanup requires verified archives and eligible task state rather than deleting directories solely by age.

This is a local OS sandbox, not a VM. The original supervisor is trusted to create appropriate checks, keep references private and review untrusted model output as data. Project files and prompts sent to providers remain subject to the user's provider agreements. Host compromise, model reasoning errors and weak acceptance criteria are outside the guarantees of the gate.

## Projects and experience

The supervisor selects a strategy based on dependencies and shared files. Up to three jobs run at once. Unordered tasks with overlapping write scopes are rejected. Accepted dependency commits form the baseline for dependent tasks. Accepted children are integrated into a scratch worktree; whole-project checks and the supervisor's final score are required before project acceptance. The source branch is unchanged.

Failure metadata and public repair advice can be recorded as candidates. Only accepted standalone work or an accepted overall project supplies reusable lessons. Retrieval is repository- and path-scoped, capped at three entries / 6,000 characters. Private exam answers, check implementations and raw logs are excluded from experience. Provenance is checked; forgetting disables future retrieval while retaining audit history. No model weights are updated.

## 中文说明

[![中文分层架构图](assets/architecture-zh.png)](assets/architecture-zh.png)

大总管始终是原来的 GPT 旗舰会话，负责规划、私有验收材料、源码审核、评分与最终验收。每个任务只绑定一个执行者，创建时固定服务商、模型和强度。图中展示本地 Python 程序的逻辑模块：Telos 协调 Codex 子进程、每作业推理转发、文件状态与 Git 工作区，远端模型服务商通过 HTTPS 提供推理。框图中的模块不代表额外模型，也不是独立部署的微服务。

### 分层与术语

| 层次 | 核心职责 | 对应约束 |
| --- | --- | --- |
| **交互与控制入口** | 通过 MCP stdio/JSON-RPC 暴露 25 个工具；绑定原始大总管；回传完成事件 | 可信 `threadId`、原会话身份、CLI 队列或版本化 Desktop IPC |
| **任务编排平面** | 冻结合同、有限状态机（FSM）、依赖有向无环图（DAG）调度 | 合同哈希、服务商/模型/强度绑定、可修改路径、轮数与并发上限 |
| **执行与验证平面** | 持久化执行者会话、鉴权推理转发、真实检查、验收策略门禁 | 按阶段控制能力、固定上游地址、固定源码提交与证据包 |
| **产物与经验平面** | 工作区、快照、私有证据、回执与经验存储 | JSON 状态和文件锁、Git 提交标识、来源校验、归档后清理 |

FSM 约束任务能够进入哪些状态；DAG 表达项目任务之间的依赖；策略门禁由程序强制执行冻结的验收条件；来源追溯（Provenance）把经验关联到支持它的已验收工作。模块与源码的对应关系见上方 [Module boundaries](#module-boundaries) 表。

### 执行时序与验收不变量

[![中文执行时序图](assets/lifecycle-zh.png)](assets/lifecycle-zh.png)

时序图合并展示 MCP 请求与响应。完成事件的 `receive` 回执由原大总管提交；执行者不负责给自己的成果评分或确认验收。

`open → send → 完成事件 → receive → evaluate → 完成事件 → receive → review → grade`

`evaluate` 先固定源码提交并运行真实检查，通过后才进入问卷。工作和考试恢复**同一个执行者会话**，但能力边界不同：

| 能力 | 工作阶段 | 闭卷阶段 |
| --- | --- | --- |
| 会话 | 持久化执行者线程 | 同一线程，保留此前工作记忆 |
| 工具 | 受权限约束的执行工具 | `tools=[]`、`tool_choice=none`，拒绝工具调用响应 |
| 文件系统 | 本任务工作区；显式授权的只读依赖 | 不开放项目源码和依赖 |
| 新增上下文 | 公开需求、返工建议、符合条件的经验 | 仅题号与题目 |
| 私有验收材料 | 不提供参考答案与私有检查 | 不追加参考答案、评分细则、检查结果或源码 |
| 模型策略 | 固定服务商、模型、推理强度 | 继续使用同一绑定，关闭考试压缩 |

原大总管审核固定源码和私有证据后，提交逐题分数、阻塞项和可选公开经验。程序要求以下条件同时成立：真实检查全部通过、总分达到及格线（默认 **90/100**）、关键题达到最低分、没有阻塞项、源码提交及合同和验收包哈希仍与被审核状态一致。

不通过时，在预算内向原执行者提供公开返工建议，冻结标准保持不变。默认最多五轮评估，包含首次；基础设施重试另有限额，不能绕过验收。通过后产出验收提交及证据，合并、推送、部署另行执行。

### 完成事件与故障恢复

完成事件写入持久化发件箱，绑定原大总管会话。`event_id` 标识事件，`receive` 保存接收回执，操作 ID 用于避免重复推进。恢复时核对持久化状态、进程记录和投递证据；无法确定是否已经发送的请求不会盲目重发。这些机制不承诺模型调用“恰好执行一次”。

CLI 队列与 Desktop IPC 是不同回传适配器。Desktop IPC 依赖客户端内部协议版本，真实桌面自动回传仍待端到端验证；仅有 socket 或配置检查成功不能证明完成回传。暂停或取消阻止后续阶段，已发出的模型请求可能继续完成。

### 项目集成、隔离与经验

项目模式由大总管决定单任务、并行或分阶段，最多三个作业同时运行。修改范围重叠的任务必须声明依赖；后续任务以已验收依赖提交为基线。子任务通过后在独立工作区集成，运行整体检查，再由大总管整体验收，不自动修改源分支。

凭据只由宿主桥接程序持有，请求转发至冻结的 HTTPS Responses 地址。执行者使用临时转发令牌，shell 不继承密钥与令牌；命令默认禁止联网，显式授予的依赖权限为只读。私有状态与证据存于 `~/.local/state/telos/`，工作区和隔离会话位于 `~/.cache/telos/`，通过 JSON 文件与文件锁持久化。清理需要验证归档与任务状态，不单按目录年龄删除。

macOS Seatbelt 属于本地操作系统沙箱，不能提供虚拟机隔离或抵御宿主机失陷。大总管负责检查设计、私有材料保密及审核判断；验收门禁无法保证弱检查足以发现所有问题。发送给服务商的源码和任务上下文仍受相应服务条款约束。

失败记录与公开返工建议可成为候选经验，只有独立任务或整个项目通过验收后才允许复用。按仓库与修改范围匹配，最多三条、6,000 字符，不包含私有答案、检查实现和原始日志。忘记经验会停用后续检索并保留来源，不训练模型权重。

## Diagram source / 图源

Version 3.2 adds system audits to the core workflow illustrated above: `audit.py` validates public boundaries and pins report evidence; `audit_probe.py` brokers bounded HTTP/browser actions; `audit_browser.cjs` drives an explicitly supplied existing Playwright/Chromium installation. Audit artifacts live separately from read-only source and private exam materials. Final acceptance judges investigation quality, with findings and limitations preserved. See [audit workflow and boundaries](audits.md). The static core diagrams retain the original 3.1 catalog; the current generated MCP catalog has 25 tools.

3.2 版本增加只读走查合同、受限接口/浏览器探针、独立证据产物和大总管逐项审核，复用原调度、回传与闭卷问答。走查验收与系统是否存在 Bug 分开；详见[系统走查](audits.md)。上方静态核心架构图保留 3.1 的工具数量，当前工具目录为 25 项。

| Diagram / 图 | English | 中文 |
| --- | --- | --- |
| System architecture / 系统架构 | [PNG](assets/architecture-en.png) · [SVG](assets/architecture-en.svg) | [PNG](assets/architecture-zh.png) · [SVG](assets/architecture-zh.svg) |
| Execution lifecycle / 执行时序 | [PNG](assets/lifecycle-en.png) · [SVG](assets/lifecycle-en.svg) | [PNG](assets/lifecycle-zh.png) · [SVG](assets/lifecycle-zh.svg) |

Editable layout, labels and colors live in [render_architecture.py](../scripts/render_architecture.py). PNG previews are exported at 3,240 pixels wide; SVG text is outlined to preserve typography without requiring the reader to install fonts. The Python source remains editable.

The generator requires Matplotlib, a development-only dependency. For Chinese rendering, install a CJK font such as Noto Sans CJK SC or use the macOS Heiti SC font. Pass `--font` to select another font family.

```sh
python scripts/render_architecture.py
# With another installed Chinese font:
python scripts/render_architecture.py --font "Noto Sans CJK SC"
```

The generator checks text widths and canvas bounds before export. Preview all four PNGs after layout or wording changes; geometry checks cannot replace visual inspection.

图中文字、布局和颜色统一维护在 Python 图源中；PNG 宽度为 3,240 像素，SVG 将文字转为轮廓以保持字体效果。中文默认使用 macOS 黑体，可用 `--font` 指定其他已安装字体。修改后重新生成并目视检查四张预览图，确认换行、间距和连接关系。
