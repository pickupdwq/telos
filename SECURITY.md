# Security / 安全说明

Telos runs model-generated code on a local Mac. Its macOS Seatbelt profiles separate worktrees, private grading material, credentials and executor caches. This is not a VM boundary or protection against a compromised host. Review task scope and provider data handling before using sensitive repositories.

## Reporting a vulnerability

For credential disclosure, sandbox escape, cross-task access or an acceptance-gate bypass, use GitHub **Security → Report a vulnerability** if private reporting is available. Otherwise open an issue containing only a request for a private reporting channel; omit exploit details, secrets and sensitive logs until a private channel is arranged.

Include the affected commit/version, macOS/Python/Codex versions, a minimal reproduction using fake credentials, expected/actual behavior and the likely impact. Never submit working API keys. This project currently maintains the latest repository version; older local snapshots are not a supported security branch.

## Operational boundaries

- Keep credentials outside Git; use `telos auth` or provider environment variables. Do not attach private state directories or unredacted reports to issues.
- Model outputs and project content are untrusted input. The supervisor must independently review code and evidence.
- Command networking is disabled by default; explicit network/dependency grants broaden the task's capabilities.
- Source/context goes to the selected providers. Local contracts, evidence, session history and experience may contain sensitive repository information.
- A successful closed-book answer is not sufficient for acceptance. Real checks and unchanged commit/evidence gates must also pass.
- Upgrade Codex/Desktop deliberately and rerun probes; internal protocol compatibility must not be assumed.

## 中文

涉及密钥泄露、沙箱逃逸、跨任务访问或绕过验收门禁的问题，请优先使用 GitHub Security 的私密漏洞报告入口（如果仓库已启用）。若不可用，先开不含细节的 issue 请求私密联系方式，不公开利用步骤、密钥或敏感日志。

报告请附受影响版本、macOS/Python/Codex 版本，以及使用假凭据的最小复现。当前仅维护仓库最新版本。Seatbelt 不等于虚拟机隔离；执行者输出和项目内容仍须按不可信数据处理，大总管必须独立审核。源码和上下文会发送到所选模型服务商，本地合同、证据和会话历史也可能含敏感信息。
