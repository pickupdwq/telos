# Testing / 测试

The default checks below use deterministic local model stubs. They require no provider keys and do not consume model quota. Git, subprocesses and macOS sandbox behavior are exercised for real where applicable.

## Source regression

```sh
python3 -m unittest discover -s tests -v
mkdir -p .artifacts
python3 runtime/verify_all.py .artifacts/regression.json
```

The installer tests use temporary directories and mocked prerequisites. They cover runtime/Skill packaging, legacy upgrades, exact configuration preservation, conflicts, active-task locks and publication rollback. The runtime suite requires macOS and runs each locally declared test once; avoid broad `unittest discover` over `runtime/`, which can repeat inherited tests. Coverage includes task gates, callbacks, project orchestration, isolation, providers, closed-book exams, experience, storage, audits and local endpoints. Use the generated report for the current test count.

Observer coverage (`test_watch`) uses actual subprocess pipes, a real isolated
Codex CLI with a local model fixture, and a separate tmux server when available.
No existing terminal panes are changed. It verifies streaming before process
completion, one shared panel, focus retention, task switching, close/reopen,
history and private-exam boundaries. See [observer behavior](watch.md).

Local endpoint regressions exercise both Responses and Chat against real HTTP servers and the real Codex CLI, including namespaced tools, work-history recall, zero-tool exams, supervisor grading, key/port isolation and selection locks. These are deterministic model fixtures; a real local gateway needs a separate small work → exam → supervisor acceptance run. See [local executor configuration](local-executors.md).

Usage/preflight regressions cover Chat and Responses JSON/SSE accounting, malformed and partial usage, duplicate terminal events, concurrent reservations, frozen budgets shared by work/exam/repair, exhaustion and unknown-usage stops. `test_preflight.py` also checks the no-model probe order and, when nested Seatbelt is available, runs the selected Python and Git inside the real profile while confirming the private path remains denied. A container or outer sandbox that denies `sandbox-exec` or loopback listeners can run the pure accounting/preflight/compact tests but must report those integration tests as blocked, not passed; run the full command on the supervisor host.

## Fresh-install smoke test

After installing the dedicated CLI as documented in the README:

```sh
python3 install.py --prefix "$PWD/.artifacts/installed" \
  --bin-dir "$PWD/.artifacts/bin" --skills-dir "$PWD/.artifacts/skills"
python3 runtime/verify.py --install-dir "$PWD/.artifacts/installed" \
  --report .artifacts/installation.json
```

The explicit Skill directory keeps this smoke test separate from your real global Skill. It does not register MCP or change your current installation. Use fresh targets each time: the installer deliberately refuses implicit overwrites. The verification includes a stdio MCP handshake and 25-tool catalog, rejection of untrusted ownership, callback/repair regressions, a real local Codex app-server with a stub model and a worker credential-environment probe.

A passing report verifies this local CLI/MCP path. It does **not** validate upstream keys, remote model behavior or automatic return to a real Desktop conversation. After a CLI/Desktop upgrade, repeat local tests and a small end-to-end task in the actual host you intend to use.

## Skill validation

Validate the Skill frontmatter/UI metadata and all linked references. Check actual discovery in the target Codex client; on the tested CLI 0.163.0, app-server `skills/list` with `forceReload=true` exposes the parsed Skill, enabled state, scope and MCP dependency without starting a model turn. For a global install, expect a single user-scoped `telos` at the installed path. This checks loading, not model behavior.

For substantial workflow changes, simulate realistic requests with an independent reviewer: ordinary advice, explicit development, audit-only findings, authorized repairs, inaccessible screenshots, duplicate callbacks, user pause, unsupported local effort and project child events. Inspect the concrete decisions rather than matching prose. Keep simulated evaluations distinct from real executor work → exam → supervisor acceptance tests; do not launch paid model tests merely to validate Skill metadata.

## Optional local browser audit verification

Use already installed Playwright and a Chromium headless shell. No package installation, real provider call or external site is involved:

```sh
python3 runtime/verify_audit_browser.py \
  --module /absolute/path/to/node_modules/playwright/index.js \
  --browser /absolute/path/to/chrome-headless-shell \
  --report .artifacts/audit-browser-verification.json
```

The fixture exercises desktop/mobile form interaction, deliberately broken mobile layout, off-scope request and redirect blocking, captured screenshots, and the supervisor review gate. Inspect exported screenshots as well as the JSON report. It uses a deterministic model/exam/supervisor boundary and does not establish model audit quality on real systems. Browser verification is opt-in; the default suite covers contracts, real sandbox boundaries, HTTP probes, a real executor CLI against a stub model, evidence tampering, project integration, repair handoffs and archival.

浏览器验证复用已有依赖，在本地测试页面验证交互、布局缺陷、地址与重定向限制和截图证据。默认回归不要求安装浏览器；两者都不消耗模型额度。

## Optional paid integration checks

The independent comparison runner is covered by `test_comparison.py`, included
in `verify_all.py`. It checks frozen inputs, variant ordering, serial leases,
configuration/experience isolation, explicit live opt-in, pause/cancel, budget
preservation, incomplete/unknown reports, seeded outcome oracles and a real CLI
work → exam → rejected review → repair → accepted review cycle for both variants.
An additional accepted-audit cycle checks same-thread tool-free exams, preserved
supervisor finding decisions and independently scored detection counts.
That regression uses a deterministic HTTP model fixture and synthetic review
scores. A real-model comparison must be run separately and reviewed by the
original supervisor; see [comparison](comparison.md). Reports must distinguish
that small live smoke run from the full public 20-case suite and a private
capability benchmark.

```sh
python3 "$HOME/.local/share/telos/verify_live_providers.py" \
  --live --report .artifacts/live-providers.json
```

Run from a directory containing `.artifacts/`. This explicit opt-in check uses the installed DeepSeek and Ark profiles/credentials and can incur API charges or consume Coding Plan quota. Read the script and confirm both providers are configured first. Live checks are excluded from CI. Do not commit generated reports: even non-secret usage logs can contain local paths and task details.

## Bailian contract simulation

The Bailian provider regression in `test_providers.py` runs the real isolated
Codex CLI against a deterministic local HTTP model. It checks shell/patch tool
round trips, explicit history, the same-thread zero-tool exam, usage accounting,
frozen model/effort, and credential isolation. The HTTP fixture enforces adjacent
tool results, including a historical parallel batch. Malformed history is rejected
before spending a request. This is not a live model test.

An additional local Postman fixture is based on the official Responses contract;
it is maintained in this repository, not an Alibaba-published mock. With the
Postman CLI already installed, run these in separate terminals:

```sh
postman simulation run postman/simulations/bailian.sim.yaml --port 4918 --output ndjson --no-history
python3 runtime/verify_bailian_simulation.py --base-url http://127.0.0.1:4918/bailian/compatible-mode/v1
```

The verifier sends only synthetic credentials through the real relay. It checks
a two-call historical batch across JSON and SSE responses, its ordered results,
the disabled parallel-call flag, and both total and reasoning token accounting.
Inspect the simulation's two request events, then stop it with
Ctrl-C. No Postman login, remote publication, API key or model quota is required.
Generated logs/reports belong under `.artifacts/`, outside Git.

For live evidence, use a small disposable repository with
`executor_profile=bailian_deepseek`, freeze checks/questions and a request/token
budget, and complete work → exam → original-supervisor review. A successful direct
API response alone does not prove that workflow, image understanding or long
context behavior. Keep real paid calls out of the default test suite.

## CI

GitHub Actions runs portable installer tests on Ubuntu and the runtime suite plus a fresh-install smoke test on macOS. The workflow uses Codex CLI 0.163.0 and a local stub model; it needs no repository secrets. Reports are retained as Actions artifacts, not committed source files. CI results are additional evidence, not a promise of compatibility with every machine/provider.

## 中文说明

默认测试不调用真实服务商、不消耗额度。安装器测试在临时目录验证启动器、Skill 打包、旧版升级、配置保留、冲突检查、活跃任务锁和失败回滚；运行时回归使用真实 Git、进程和 macOS 沙箱，模型边界使用本地替身，数量以验证报告为准。

完整安装验证还包括 MCP 握手、25 个工具、可信会话绑定、原生队列、回传和执行环境凭据隔离。临时安装命令使用独立 Skill 目录，不注册 MCP、不覆盖本机部署。已有验证目录请保留或换一个空目录，不要对生产目录强制覆盖。Skill 的加载发现、场景模拟和真实模型验收是三种不同证据，需分别说明。

离线通过不等于真实 Key 有效，也不等于桌面自动回传已通过。升级 CLI/桌面程序后，在目标宿主再做小规模真实任务验证。`--live` 是额外付费验证，会调用配置中的 DeepSeek 与火山服务商；CI 不运行它。报告可能含路径或任务信息，不要提交到仓库。
