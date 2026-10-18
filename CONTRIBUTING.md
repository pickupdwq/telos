# Contributing / 贡献指南

Use an issue to describe a reproducible bug or discuss a substantial design change, then open a focused pull request from a feature branch. Small documentation fixes can go straight to a PR. Please keep the English and Chinese READMEs consistent.

## Development

1. Fork/clone the repository and create a branch.
2. Use Python 3.12+; macOS is required for runtime isolation tests. Install the dedicated Codex CLI as described in the README for installation probes.
3. Make the smallest coherent change and add regression coverage when behavior changes.
4. Run installer tests and relevant runtime tests; run `runtime/verify_all.py` before submitting runtime changes. Do not run paid provider tests without intentionally opting in.
5. Explain the problem, resulting behavior, validation and remaining limitations in the PR.

Runtime modules use Python's standard library. Matplotlib is optional and used only to regenerate architecture assets:

```sh
python3 -m venv .venv
.venv/bin/pip install matplotlib
.venv/bin/python scripts/render_architecture.py
```

Use an available CJK font for the Chinese diagram. If MCP schemas change, regenerate `docs/tools.md` and `docs/tools.schema.json` with `python3 scripts/render_tools.py`.

## Invariants to preserve

- The original GPT supervisor owns acceptance. One frozen executor binding per task; work and closed-book exam share a thread.
- Private references, checks and grading evidence must not leak to the executor. Upstream credentials stay in the bridge.
- Acceptance remains tied to successful executable checks and unchanged source/evidence; retries cannot bypass gates.
- Task/project scopes, budgets, receipts and historical frozen contracts must retain their semantics.
- Changes must not silently merge, push, deploy, weaken isolation or alter existing user credentials/configuration.

Never include real keys, local runtime state, private reports, personal paths or unrelated project files. Use clearly fake fixtures. See [SECURITY.md](SECURITY.md) for sensitive reports. Contributions are provided under the repository's MIT license.

## 中文

较大变更先通过 issue 说明问题与设计，小型文档修正可直接提 PR。请在功能分支开发，保持中英文说明一致，提交前运行相关测试；运行时变更需完整回归。真实服务商测试需要主动选择，会消耗额度。

重点保持两角色、同执行者闭卷、私有材料隔离、凭据只在桥接层、固定源码与证据验收、预算和回执幂等，以及历史合同兼容。不要暗中放宽沙箱、合并推送代码或修改用户配置。不要提交真实密钥、本机任务数据、私人报告和个人路径。贡献适用 MIT 许可。
