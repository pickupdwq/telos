# Architecture / 架构

[English README](../README.md) · [中文 README](../README.zh-CN.md)

![Architecture](assets/architecture-en.png)

## Roles and state

The original Codex conversation is the GPT supervisor. It owns task planning, private references, source review, scores and final acceptance. Telos is a deterministic local coordinator, not another model. Each task has one executor binding, frozen when the task is opened.

Work and exam resume the same executor thread. In the exam phase, the relay enforces an empty tool list, no tool choice and the frozen model/effort. The sandbox excludes project source and dependencies. Only question IDs and prompts are newly supplied; earlier work messages and tool results remain in the conversation. Reference answers, rubric and check results are not newly disclosed. Exam compaction is disabled.

`send → completion event → receive → evaluate → completion event → receive → review → grade`

Executable checks precede the questionnaire. A check failure returns to repair. Acceptance requires successful checks, score threshold, critical minima, no blockers and unchanged pinned source/evidence. Infrastructure recovery cannot accept a task. Events are tied to the original thread; receipts and action IDs prevent duplicate follow-on work. Pausing prevents new stages, while requests already in flight may complete.

## Module boundaries

| Module | Responsibility |
| --- | --- |
| `runtime/telos.py` | MCP catalog, task lifecycle, events, snapshots and acceptance gate |
| `runtime/supervisor.py` | Original supervisor identity and GPT model allowlist |
| `runtime/providers.py` | Provider policy, frozen model bindings and host credential lookup |
| `runtime/executor.py` | Persistent Codex executor session; work/exam capability changes |
| `runtime/closed_book.py` | Tool-free exam request/response enforcement and answer parsing |
| `runtime/check_runtime.py`, `runtime/isolation.py` | Private executable checks and macOS Seatbelt boundaries |
| `runtime/projects.py` | Task graph, scheduling, write scopes, scratch integration and final gate |
| `runtime/codex_rpc.py`, `runtime/desktop_rpc.py` | CLI queue and versioned Desktop IPC adapters |
| `runtime/experience.py` | Provenance, verified lesson selection and disabling stale records |
| `runtime/storage.py`, `runtime/processes.py` | Archive/cleanup safeguards and process bookkeeping |
| `runtime/identity.py`, `runtime/exam_loop.py`, `runtime/legacy_exam.py` | Product identity and historical contract compatibility |

The flat runtime directory is intentional: it is copied as a self-contained standard-library application, with its regression and installation probes adjacent to the modules they verify. Packaging and documentation live outside this directory.

## Trust and storage boundaries

The bridge retains upstream credentials and forwards requests only to the frozen provider endpoint. Executor requests use an ephemeral local relay token; shell environments exclude upstream keys and relay tokens. Source worktrees, private contracts and other executors' caches have separate permissions. Explicit dependency grants are read-only. Normal command network access is disabled by default, while model traffic goes through the relay.

This is a local OS sandbox, not a VM. The original supervisor is trusted to create appropriate checks, keep references private and review untrusted model output as data. Project files and prompts sent to providers remain subject to the user's provider agreements. Host compromise, model reasoning errors and weak acceptance criteria are outside the guarantees of the gate.

## Projects and experience

The supervisor selects a strategy based on dependencies and shared files. Up to three jobs run at once. Unordered tasks with overlapping write scopes are rejected. Accepted dependency commits form the baseline for dependent tasks. Accepted children are integrated into a scratch worktree; whole-project checks and the supervisor's final score are required before project acceptance. The source branch is unchanged.

Failure metadata and public repair advice can be recorded as candidates. Only accepted standalone work or an accepted overall project supplies reusable lessons. Retrieval is repository- and path-scoped, capped at three entries / 6,000 characters. Private exam answers, check implementations and raw logs are excluded from experience. Provenance is checked; forgetting disables future retrieval while retaining audit history. No model weights are updated.

## 中文说明

![中文架构图](assets/architecture-zh.png)

大总管始终是原来的 GPT 旗舰会话，负责规划、私有验收材料、源码审核、评分与最终接受。Telos 本身是确定性的本地协调程序，不是第三个模型。每个任务只绑定一个执行者，创建时固定服务商、模型和强度。

工作和考试恢复同一个执行者会话。考试阶段由转发层强制禁用工具，沙箱不开放项目源码和依赖，只追加题号与题目；保留此前工作记忆，不追加私有参考、评分细则或检查结果，关闭考试压缩。先运行真实检查，检查通过后才答问卷。评分通过还要求源码、证据未改变，关键题达标且没有阻塞项。

项目模式由大总管决定单任务、并行或分阶段，最多三个作业同时运行。修改范围重叠的任务必须声明依赖；后续任务以已验收依赖提交为基线。子任务通过后在独立工作区集成，运行整体检查，再由大总管整体验收，不自动修改源分支。

凭据只由宿主桥接程序持有，执行者使用临时转发令牌，shell 不继承密钥与令牌。状态、私有检查与工作缓存分开存放，依赖授权只读。macOS Seatbelt 不等于虚拟机隔离；检查质量、原大总管的审核判断以及模型服务商的数据处理仍需用户理解和管理。

失败记录与公开返工建议可成为候选经验，只有独立任务或整个项目通过验收后才允许复用。按仓库与修改范围匹配，最多三条、6,000 字符，不包含私有答案、检查实现和原始日志。忘记经验会停用后续检索并保留来源，不训练模型权重。

## Diagram source / 图源

PNG previews: [English](assets/architecture-en.png), [中文](assets/architecture-zh.png). Vector sources: [English SVG](assets/architecture-en.svg), [中文 SVG](assets/architecture-zh.svg). Editable generation code: [render_architecture.py](../scripts/render_architecture.py).

The generator requires Matplotlib, a development-only dependency. For Chinese rendering, install a CJK font such as Noto Sans CJK SC or use the macOS Heiti SC font. Pass `--font` to select another font family.
