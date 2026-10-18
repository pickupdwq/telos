# Changelog / 更新记录

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
