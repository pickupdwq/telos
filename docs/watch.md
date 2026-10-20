# Live executor observer

Telos records executor activity continuously and provides a read-only viewer.
The executor and viewer are separate processes: closing the viewer, pressing
`q`/Ctrl-C, or closing its tmux pane does not cancel work. Reopen the viewer to
see the saved history and continue following.

```sh
telos watch --list
telos watch                         # interactive task switcher in a terminal
telos watch <session_id>             # open a task (unique prefixes >= 8 characters work)
telos watch <session_id> --history --no-follow
telos watch <session_id> --json --follow
telos watch --project <project_id>    # one viewer for a project's executors
telos watch --pane                   # reopen/reuse the current tmux window's panel
```

In the terminal UI, `Tab`/`n`/`p` switch tasks, arrows or `j`/`k` and
PageUp/PageDown scroll, `g` goes to the oldest displayed line, `G` returns to
live following, Space pauses only the view, and `q` closes the viewer.
The panel keeps your task selection when new tasks start.

The default initial history is 200 events (`--tail N` changes it). The UI keeps
up to 2,000 events / 6,000 rendered lines; use `--history --no-follow` to export
all retained history, including older content outside that display buffer.
`--json` emits JSONL for piping and defaults to following until Ctrl-C.
`--no-follow` prints recorded events once and exits.

## Ghostty and iTerm2 with tmux

Run Codex CLI **inside tmux**, in either terminal:

```sh
tmux new -s work
codex
```

After a task starts, Telos opens one panel to the right of the originating
Codex pane without changing focus. Other executors in the same tmux window
reuse that panel; different windows remain separate. Use your normal tmux
pane-selection shortcut when you want to interact with the viewer.
Closing/reopening it does not restart or resend the task.

Automatically opened panels close once every associated task is accepted or
cancelled and its running process has exited. Project panels also wait for
final project acceptance or cancellation, including pending/dependent tasks
and integration checks. A response, an exam result awaiting review, a repair,
an error or a pause is not completion. Executor count and scheduling remain
the supervisor's decision; one completed executor does not close a panel
that another still needs.

Only the managed observer pane is removed; the supervisor pane stays open.
New work opens the panel again. Logs remain available with `telos watch`.
Viewers opened manually with `telos watch --ui` or a new `telos watch --pane`
stay open for history until you close them. Unreadable lifecycle state keeps
an automatic panel open rather than treating missing evidence as completion.

tmux is optional and is never installed automatically. Without tmux, or if
opening a pane fails (for example, the terminal is too small), task execution
continues. Open another terminal/tab and use `telos watch`. This version does
not automatically split Ghostty/iTerm2 windows outside tmux.

### Forward the originating pane to MCP

Codex must forward `TMUX` and `TMUX_PANE` to the Telos MCP process. New
registrations and managed upgrades with `--register` add these variable names
to the existing Telos entry and keep a private backup of the original config:

```sh
python3 install.py --upgrade --register
```

An upgrade without `--register` leaves the MCP config unchanged. For manual
registration, merge the two names into the existing `env_vars` array in
`$CODEX_HOME/config.toml` (normally `~/.codex/config.toml`):

```toml
[mcp_servers.telos]
command = "/absolute/path/to/telos"
args = ["serve"]
env_vars = ["TMUX", "TMUX_PANE"]
```

Preserve your real command, arguments and any existing forwarded variables.
Restart Codex **inside the desired tmux pane** after setup. Do not put a
particular socket or pane ID in the static `env` table: those values must be
inherited from each new Codex session. Forwarding uses Codex's documented
[`env_vars` setting](https://learn.chatgpt.com/docs/extend/mcp).

When Codex uses a shared background server, its local environment may not
include the terminal client's tmux variables even with forwarding configured.
If both variables are absent, Telos can recover the pane on the default tmux
server using the trusted CLI caller's working directory. It requires exactly
one live pane in an attached session with that exact directory and a Codex
process in its process tree. Observer panes, unrelated processes, detached
sessions and ambiguous matches are ignored. Explicit but stale/partial tmux
variables are never replaced by a guessed pane. Desktop callers do not use
this fallback, and custom tmux sockets still require environment forwarding.

For an unambiguous inherited environment, Codex also supports
[`codex --no-daemon`](https://learn.chatgpt.com/docs/cli/reference), which runs
the interactive CLI without the shared background server. Existing tasks can
always be viewed with `telos watch <session_id> --ui`; this does not restart them.

To disable automatic panels while retaining recording and manual watching,
merge this into the installed Telos `config.json`, then restart its MCP process:

```json
{"watch": {"auto_tmux": false}}
```

## What is recorded

The recorder drains Codex's
[`exec --json` event stream](https://learn.chatgpt.com/docs/non-interactive-mode)
while the process is running. It shows executor messages, tool starts and
results, command output/exit codes, reported file changes, errors and Telos
lifecycle transitions. Shell-based file changes are additionally summarized at
the end of each implementation work attempt, including attempts that report
an error or time out. A forcibly killed bridge can retain only events already
written before it stopped.
Updates follow CLI event boundaries; this is not token-by-token streaming or
a byte-by-byte shell terminal. Command output becomes visible when the CLI
emits an update/result event.

Records live at:

```text
~/.local/state/telos/runs/<session_id>/activity.jsonl
```

`TELOS_STATE_DIR` and the existing legacy state-directory override apply.
Logs persist across work, repair, exams and viewer restarts. Historical tasks
created before this feature have no reconstructed activity transcript.

The bridge writes outside the executor's sandbox. The viewer does not open
private contracts or review packets, invoke models, grade, send input to an
executor, or control tasks. Exam progress is visible, but exam text, answers,
references, executable-check source/output, and reasoning items are excluded.
Reported state is the stored lifecycle state, not a fresh acceptance decision.

Files are created with mode `0600`. Known relay/probe credentials and common
credential patterns are redacted; terminal control characters are removed.
Arbitrary command output can still contain sensitive project data, so logs
remain private local data. Large text fields are visibly truncated; malformed
or oversized events are omitted without blocking the executor.
An I/O failure disables recording for that recorder and creates an
`activity-unavailable` marker when possible; the viewer reports that condition.
Task execution and its original acceptance gates continue independently.

## 中文使用说明

在 Ghostty 或 iTerm2 中先进入 tmux，再启动 Codex CLI。安装/升级时使用
`--register`，安装器会为 Telos 的 MCP 配置补充 `TMUX`、`TMUX_PANE`
环境变量转发，并备份原配置；之后重启 Codex。

若共享后台服务没有继承终端的两个 tmux 变量，Telos 会使用可信 CLI 会话的
工作目录，在默认 tmux 服务中寻找唯一匹配且正在运行 Codex 的已连接窗格。
多个匹配、桌面会话、失效的显式变量和无法识别的窗格均不会自动连接。
自定义 tmux socket 仍需转发环境变量；也可在 tmux 中使用
`codex --no-daemon`，让新会话直接继承该终端环境。

发起任务后会自动在右侧打开一个只读观察面板，焦点仍留在主对话。
多个执行者共用这个面板，按 `Tab`、`n`、`p` 切换任务；方向键或翻页键
查看已显示的历史，`G` 回到实时跟随。`q` 或关闭窗格只关闭查看器，
不会停止执行者；用 `telos watch --pane` 重新打开。

自动打开的面板会在同一窗口的关联任务全部验收通过或取消、且运行进程退出后
自动关闭。项目还需等待整体验收或取消，包含尚未启动的依赖任务和集成检查；
等待考试、审阅、修复、报错或暂停时保留面板。执行者数量及调度由大总管决定，
不会因为其中一个执行者结束就提前关闭。关闭范围仅限观察窗格，大总管窗格保留；
新任务会重新打开面板，日志继续保存。手动打开的历史查看器保持打开，直到按 `q`
或退出；读取任务状态失败也会保留面板。

非 tmux 环境照常执行，在另一个终端或标签页运行 `telos watch` 即可。
`telos watch --list` 列出任务，
`telos watch <任务ID> --history --no-follow` 输出完整保留历史。
第一版不提供接管执行者或向其输入指令的入口。

## Verification

The regression suite exercises actual subprocess pipes, a real isolated Codex
CLI with a deterministic local model fixture, and (when installed) an isolated
real tmux server. It covers visibility before completion, concurrent pane
reuse, focus, switching, close/reopen, history, timeout handling, log failures,
credential filtering, and closed-book content exclusion. Provider calls in
these tests are local fixtures, not paid model invocations. They do not replace
separate visual checks in each terminal application's GUI.
Completion coverage includes multiple executors, final project review,
cancelled workers still running, manual history, `remain-on-exit`, pane
ownership and serialization with task dispatch.
