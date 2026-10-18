# Testing / 测试

The default checks below use deterministic local model stubs. They require no provider keys and do not consume model quota. Git, subprocesses and macOS sandbox behavior are exercised for real where applicable.

## Source regression

```sh
python3 -m unittest discover -s tests -v
mkdir -p .artifacts
python3 runtime/verify_all.py .artifacts/regression.json
```

The installer tests use temporary directories and mocked prerequisites. The runtime suite requires macOS and runs each locally declared test once; avoid broad `unittest discover` over `runtime/`, which can repeat inherited tests. The initial release contains 197 runtime regression cases covering task gates, callbacks, project orchestration, isolation, providers, closed-book exams, experience, storage and identity compatibility.

## Fresh-install smoke test

After installing the dedicated CLI as documented in the README:

```sh
python3 install.py --prefix "$PWD/.artifacts/installed" \
  --bin-dir "$PWD/.artifacts/bin"
python3 runtime/verify.py --install-dir "$PWD/.artifacts/installed" \
  --report .artifacts/installation.json
```

This does not register MCP or change your current installation. Use a fresh target each time: the installer deliberately refuses overwrites. The verification includes a stdio MCP handshake and 24-tool catalog, rejection of untrusted ownership, callback/repair regressions, a real local Codex app-server with a stub model and a worker credential-environment probe.

A passing report verifies this local CLI/MCP path. It does **not** validate upstream keys, remote model behavior or automatic return to a real Desktop conversation. After a CLI/Desktop upgrade, repeat local tests and a small end-to-end task in the actual host you intend to use.

## Optional paid integration checks

```sh
python3 "$HOME/.local/share/telos/verify_live_providers.py" \
  --live --report .artifacts/live-providers.json
```

Run from a directory containing `.artifacts/`. This explicit opt-in check uses the installed DeepSeek and Ark profiles/credentials and can incur API charges or consume Coding Plan quota. Read the script and confirm both providers are configured first. Live checks are excluded from CI. Do not commit generated reports: even non-secret usage logs can contain local paths and task details.

## CI

GitHub Actions runs portable installer tests on Ubuntu and the runtime suite plus a fresh-install smoke test on macOS. The workflow uses Codex CLI 0.163.0 and a local stub model; it needs no repository secrets. Reports are retained as Actions artifacts, not committed source files. CI results are additional evidence, not a promise of compatibility with every machine/provider.

## 中文说明

默认测试不调用真实服务商、不消耗额度。安装器测试在临时目录验证启动器、路径转义、配置默认值和禁止覆盖；197 项运行时回归使用真实 Git、进程和 macOS 沙箱，模型边界使用本地替身。

完整安装验证还包括 MCP 握手、24 个工具、可信会话绑定、原生队列、回传和执行环境凭据隔离。临时安装命令不注册 MCP、不覆盖本机部署。已有验证目录请保留或换一个空目录，不要对生产目录强制覆盖。

离线通过不等于真实 Key 有效，也不等于桌面自动回传已通过。升级 CLI/桌面程序后，在目标宿主再做小规模真实任务验证。`--live` 是额外付费验证，会调用配置中的 DeepSeek 与火山服务商；CI 不运行它。报告可能含路径或任务信息，不要提交到仓库。
