# Changelog / 更新记录

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
