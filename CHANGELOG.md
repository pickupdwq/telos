# Changelog / 更新记录

## 3.7.0 — 2026-10-20

- Add persistent, read-only executor activity logs and `telos watch` for live following, task selection and history. Show messages, tool calls, command results, file changes, errors and task state while excluding private exam content and reasoning.
- Automatically open or reuse one right-hand observer pane per originating tmux window, preserving focus in the supervisor conversation. Multiple executors share the pane; closing or reopening it does not interrupt work. Outside tmux, tasks continue normally and the viewer can be opened manually.
- Forward dynamic `TMUX` and `TMUX_PANE` values through MCP registration while preserving existing configuration. Keep activity recording and observer startup failures separate from task execution.
- Include the interrupted-request usage settlement and executor closeout budget reservation fixes previously landed on main.
- Verify observer behavior with real subprocesses, Codex CLI and isolated tmux sessions using deterministic local model fixtures. Native Ghostty/iTerm2 GUI acceptance and a complete interactive Codex-to-MCP task dispatch remain unverified.

新增执行者只读观察功能：持续记录执行过程，支持实时跟随、历史回看和任务切换。在 tmux 中自动打开或复用右侧面板，保留主对话焦点；关闭面板不影响任务，非 tmux 环境可手动查看。同步运行时与 Skill 版本，并包含中断用量结算及收尾预算预留修复。

## 3.6.0 — 2026-10-20

- Add the Beijing Bailian executor profile `bailian_deepseek`, pinned to `deepseek-v4.1-flash-1017`, with independent credentials, native Responses work/exams and configurable reasoning effort (default `max`).
- Supply the isolated Bailian model catalog and enforce serial tool requests at its relay boundary. Pair complete historical tool-call batches with their results; reject missing, duplicate or ambiguous results before forwarding. Preserve other providers and existing installation configuration.
- Add real-CLI provider regressions and a local Postman HTTP simulation for tool history, same-thread closed-book exams, credential isolation and usage accounting. A separate small real-model coding task completed the current-supervisor acceptance gate; long-context and visual capabilities remain unverified.

- Add an explicitly started `telos compare` CLI with frozen runtime/configuration snapshots, 20 public development tasks, paired/counterbalanced ordering and independent trial state.
- Reuse the existing original-supervisor work/exam/repair/acceptance gate in explicit manual experiments; preserve the ordinary MCP catalog and task workflow.
- Report outcome regressions, independent outcome checks, code-audit detection, requests/tokens including failed attempts, unknown usage and separately labelled provider/wall time. Add bounded trials, pause/cancel and a serial experiment lease. No automatic grading, promotion, installation or main-branch changes.
- Fix the isolated native verifier's outdated runtime copy list so newly imported modules are included in MCP startup verification.

新增阿里百炼北京入口与 `deepseek-v4.1-flash-1017` 执行者，默认请求 `max`，保留本地、火山和 DeepSeek 官方配置。修复百炼工具结果顺序兼容问题，并完成真实编码、同会话闭卷考试与原大总管验收的小型验证。

新增独立评测与版本对比入口，复用现有验收流程，隔离实验状态与经验。内置 20 个公开开发样例，输出质量、用量、耗时和成对退步记录；不代表私有盲测、真实页面走查或已经实现 RSI。

## 3.5.0 — 2026-10-19

- Check the actual CLI/Python/Git work environment before model calls, with isolated Python startup that cannot import task-repository modules.
- Default MCP results to compact summaries while retaining full evidence access. Preserve original full project receipts as read-only snapshots and keep reminder continuation actionable and idempotent.
- Persist normalized request/token usage across work and exams, enforce configured budgets between requests, and expose unknown counters and incomplete project aggregates.
- Parse streamed Responses incrementally with a documented 16 MiB event bound, preserving valid large terminal usage and original forwarded bytes.
- Redraw the bilingual README architecture overview around the supervisor, local MCP runtime and executor. Include Skill guidance, audits, local endpoints, preflight and usage control; keep detailed module mapping in the architecture guide.

新增环境预检、精简工具返回、持久用量统计与预算控制，并修复预检误执行仓库模块、大事件用量丢失、项目汇总未知状态遗漏及完整回执补查/提醒恢复问题。重绘中英文架构图，明确两类模型角色、任务边界、模型入口与证据验收闭环。

## 3.4.0 — 2026-10-19

- Ship the `telos` supervisor Skill with scoped explicit/implicit invocation and references for planning, audits, executor configuration and acceptance.
- Install runtime and user-scoped Skill together; add explicit managed upgrades that preserve configuration/credentials/state, reject local modifications and active work, retain backups and roll back publication failures.
- Keep the current conversation as supervisor, reuse existing executor settings, and clarify evidence, task boundaries, internal acceptance commits and event reconciliation.
- Add installer regressions and installation/upgrade documentation; keep CI smoke Skills isolated from the user Skill directory.
- Document local `deepseek-flash` with `max`, cover its real-CLI tool/history/exam path using a model fixture, and reject Chat replies that contain reasoning without an answer or tool call.

新增随工具一起安装的大总管 Skill，支持开发、系统走查、确认问题后的修复和回调验收指导。默认对当前用户全局可用，复用统一执行者配置；显式升级保留现有配置、凭据和任务记录。

## 3.3.0 — 2026-10-19

- Add named local executor endpoints with one active selection, preserving DeepSeek official and Ark Coding Plan profiles and credentials.
- Support loopback Responses and Chat Completions, including function/custom tools, namespaces and same-thread exams; use no new dependencies.
- Add `telos local list/add/select` and endpoint-bound `telos auth local`. Freeze endpoint/protocol/model per task, block switching during local jobs, and reject silent rerouting or cloud fallback.
- Remove history-based tool declarations during closed-book exams, including those emitted by current GPT Codex model catalogs.
- Add configuration, credential, relay, project and real CLI regressions for both protocols.
- Handle nullable reasoning history from Codex resumes, preserving explicit reasoning effort through Chat tool calls and closed-book exams.

新增本地执行者入口：多个配置只启用一个，兼容两种协议，火山与 DeepSeek 继续可选。保留原会话闭卷验收，补齐新工具声明格式的禁用，具体边界见本地执行者指南。

## 3.2.0 — 2026-10-19

- Add `task_type=audit`: frozen public scope/checklists, read-only source, separate evidence artifacts and supervisor-owned audit review.
- Add bounded HTTP/browser probes, explicit target/method/action/viewport permissions, idempotent request IDs and screenshot evidence using existing Playwright/Chromium installations.
- Separate investigation quality from target business failures; enforce required coverage, evidence hashes, finding decisions and screenshot review acknowledgement.
- Support overlapping read-only project auditors, integrated audit evidence, confirmed-finding repair handoffs and evidence-preserving archive/cleanup.
- Preserve existing implementation tasks, same-executor exams, callbacks and credentials; add the `telos_audit_probe` MCP tool (25 total).

新增系统走查、接口与页面取证、公开执行边界、专用报告验收和确认问题转修复。页面驱动复用已有依赖，不自动安装；只支持指定测试环境，具体范围见走查指南。走查通过不代表系统没有缺陷。

## 3.1.0 — 2026-10-18

First repository release of the existing local Telos runtime.

- Original GPT supervisor, configurable executor and same-thread closed-book evaluation.
- DeepSeek official default at `max`; retained Ark Coding Plan and configurable Responses providers.
- Frozen checks and scoring gates, project scheduling/integration, persistent callbacks and verified experience.
- Compatibility with historical `exam_loop` state, tool aliases and frozen contracts.
- Fresh-install script, key-free profile example, English/Chinese documentation, PNG/SVG architecture diagrams and MIT license.
- Installer regression tests and macOS CI using local model stubs.

This release packages the existing runtime; it does not migrate or overwrite active local installations. No private credentials, task history or benchmark artifacts are included.

首次仓库发布：整理已有本机运行时，补齐全新安装器、无密钥配置、中英文文档、架构图片、MIT 许可及 CI。保留两角色闭卷验收、服务商切换、项目协作、经验复用和历史兼容；不覆盖已有部署，不包含私人凭据或任务数据。
