# Supervisor Skill / 大总管 Skill

Telos ships one supervisor Skill in [`skills/telos/`](../skills/telos/SKILL.md). MCP remains the execution and acceptance engine. The Skill guides the original conversation through requirements, task decomposition, public boundaries, private questions/checks, evidence review and bounded repairs. It does not create a second supervisor or store another set of executor settings.

## Invocation and scope

The default installer places the Skill at `~/.agents/skills/telos/`, the user scope described in [Codex Skill documentation](https://learn.chatgpt.com/docs/build-skills). It is available across this user's projects. The repository copy is the maintained source, not a second global installation.

Invoke it explicitly:

> $telos Implement [requirements] using the configured executor; verify the result and report the accepted worktree. Do not merge or push.

Or use natural language:

> Use Telos to audit the local admin system's APIs, page interactions and visual presentation. Report confirmed findings and untested scope. Audit only.

The metadata allows implicit selection for matching delegation/audit/continuation requests. Ordinary advice, questions about Telos and unrelated coding do not by themselves authorize executor calls. Skill loading is not a background job or an installation trigger. If the MCP tools are unavailable, the supervisor reports the missing integration instead of inventing a replacement execution path.

The current conversation keeps its model and authority. The runtime's existing configuration selects the executor: DeepSeek, Ark Coding Plan, another configured profile, or the single active local endpoint. Task-specific reasoning overrides still depend on upstream support. Keys stay in the existing credential mechanism. Isolated executors disable host Skill discovery and receive only their public task brief.

## Supported workflows

| Request | Supervisor responsibility |
| --- | --- |
| Development | Inspect requirements and code, choose single task or project, freeze private checks/questions, delegate bounded work and review evidence |
| System audit | Freeze correctness basis, public checklist and environment/actions; inspect code, APIs, interactions and actual screenshots |
| Audit and repair | Confirm findings, respect the user's repair authorization, link repair tasks to accepted evidence and run regressions |
| Completion event | Receive the exact event, honor pause/cancel and `proceed`, review the existing task, and require final acceptance |

The main Skill loads detailed references only when needed. Live MCP schemas define arguments; the Skill does not duplicate their catalog. Acceptance still requires the original supervisor's review and `accepted=true`; projects additionally require integrated acceptance. An accepted audit verifies investigation quality, not the absence of bugs.

Telos creates internal Git commits in isolated worktrees to pin acceptance evidence. An explicit prohibition on commits must be clarified before dispatch. Merge, push and deployment follow the user's separate authorization; Skill installation grants none of those actions.

## Install and upgrade

Fresh install:

```sh
python3 install.py --dry-run
python3 install.py --register
```

Managed upgrade, from the desired source release:

```sh
python3 install.py --upgrade --dry-run
python3 install.py --upgrade
"$HOME/.local/bin/telos" doctor
```

Both runtime and Skill use the same version. `--upgrade` supports the 3.3.0 manifest without a Skill as well as later installations. Custom runtime locations must use their original `--prefix` and `--bin-dir`; an installed Skill keeps its recorded location. `--skills-dir /path/to/skills` selects another root for a fresh install, including isolated smoke tests. Custom locations must be made discoverable by the intended client.

The upgrade checks owned-file hashes and refuses modified/added/missing managed files or unexpected symlinks. It preserves `config.json` bytes and permissions, the existing launcher, external credentials, task state and caches. MCP registration is preserved except for the explicit `--register` environment update described below. Launcher modification detection applies when its hash is recorded in the manifest; legacy launchers are retained unchanged. An unmanaged or conflicting user Skill is preserved and blocks installation into the normal user roots.

When `--register` is supplied, the installer also merges `TMUX` and `TMUX_PANE`
into the Telos MCP entry's `env_vars` and backs up the original Codex config.
Other model/MCP settings remain intact. This enables the optional
[live observer panel](watch.md); upgrade without `--register` keeps registration
bytes unchanged.

Running jobs (including paused jobs that have not finished), stale jobs and project checks block upgrade. Pause or finish active projects, wait for their workers/checks, and recover stale jobs through the runtime before retrying. The installer does not pause/cancel tasks itself. The installation and coordinator locks prevent concurrent publishing/scheduling against the checked state directory. Use the same `TELOS_STATE_DIR` / legacy state override as the running installation when one is configured.

Successful upgrades keep `.telos-runtime-backup-*` beside the runtime and `.telos-skill-backup-*` outside the Skill scan root. Their paths appear in installer output. Publication failures restore the prior runtime and Skill; a failed fresh install removes newly published files. MCP registration happens afterward: if registration fails, installed files remain and the error gives the exact registration retry command.

Do not overlay a locally edited Skill or runtime to force an upgrade. Keep the existing installation and reconcile those changes explicitly. To roll back after a successful upgrade, first stop using the affected MCP installation, preserve the current runtime/Skill pair, and restore the corresponding backup pair to their recorded paths. A first upgrade from 3.3.0 has no previous Skill; preserve/remove the newly installed Skill when restoring that runtime. Do not modify credentials or task state for a file rollback.

Reload the MCP client after upgrading; an already running process retains loaded Python modules. Codex normally detects Skill changes automatically; if `$telos` is absent, reopen the client and check its Skill list and `telos` MCP registration. A new process reading the latest version does not prove an older open conversation reloaded it.

## 中文说明

仓库中的 `skills/telos/` 是维护源，默认安装到 `~/.agents/skills/telos/`，属于当前系统用户的全局 Skill，各项目可用。安装一次即可复用，不需要每次下载；它不会因为被安装或普通咨询就启动执行者。

显式使用 `$telos`，或自然语言说“使用 Telos 开发/走查/修复……”，大总管会按场景加载指导。它负责理解需求、拆分任务、公开执行边界、私有问卷与检查、实际证据审核和预算内返工；MCP 继续负责执行、隔离、状态和验收门禁。只有最终 `accepted=true` 才能算验收通过。走查通过表示调查质量合格，不表示系统没有问题。

Skill 不保存 Key，不另建模型配置，也不改变当前会话的大总管模型。它复用原有 DeepSeek、火山、本地入口与推理强度设置；本地仍然只有一个活动入口。执行者隔离环境关闭宿主 Skill 发现，接收本任务的公开说明。

已有安装执行 `python3 install.py --upgrade --dry-run` 检查，再执行 `python3 install.py --upgrade`。配置原始内容和权限、凭据、任务记录、启动入口及已有 MCP 注册均保留。被修改的受管理文件、冲突 Skill、运行中/失联待恢复的作业或项目检查会阻止升级；先完成或暂停项目并等待作业结束。使用自定义状态路径时，升级进程必须沿用同一个环境设置。

成功升级会输出运行时和 Skill 的备份位置；发布文件过程中失败会回滚。Skill 备份放在扫描目录外，避免重复加载。升级后重载 MCP 客户端；Skill 未出现时重新打开 Codex。本功能不会自动合并、推送或部署仓库代码。
