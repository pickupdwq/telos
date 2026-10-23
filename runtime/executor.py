"""Persistent executor CLI driver; provider credentials stay in the bridge relay."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import uuid
import activity
from delivery import WORK_REPLY_GUIDANCE
import preflight
import providers
from identity import data_path


def _patch_format_guidance():
    """Advisory apply_patch argument format and bounded recovery for implementation work."""
    return [
        'Patch format: apply_patch takes raw patch text, starting with *** Begin Patch and ending with '
        '*** End Patch, without a JSON wrapper or Markdown fence.',
        "In that text, '*** Add File: path' is only for new paths. Use '*** Update File: path' with "
        "matching context for existing files, never Add File to replace one.",
        'After a patch-format rejection, allow one corrected attempt; if that still fails, use an '
        'already-available scoped file-edit capability or report a concrete blocker rather than repeating '
        'the same invalid call.',
        'A fallback must stay within existing allowed paths and sandbox permissions; it does not authorize '
        'a new tool, wider access, disabled checks or silent removal of assertions.',
    ]


def _work_guidance(evidence, contract, state, *, artifact_root=None):
    """Compact, allowlisted facts from successful preflight and runtime paths."""
    if evidence.get('status') != 'passed' or not evidence.get('interpreter') or not evidence.get('cwd'):
        raise ValueError('Verified preflight evidence is incomplete')
    lines = [
        'Verified work guidance (sandbox preflight passed):',
        '- Python interpreter: ' + evidence['interpreter'] + ' (use this exact executable).',
        '- Work cwd: ' + evidence['cwd'] + '.',
    ]
    if artifact_root is not None:
        root = Path(artifact_root)
        report = root / 'report.json'
        report_help = root / 'audit-report-help.json'
        lines += [
            '- Audit source: ' + state['worktree'] + ' (read-only; frozen source ' + state['base_commit'] + ').',
            '- Audit artifacts: ' + str(root) + ' (writable).',
            '- Write the report only to ' + str(report) + '; evidence stays in the artifact directory.',
            '- Read report help once at ' + str(report_help) + ' (report/finding schemas and evidence rules).',
        ]
        if contract['audit'].get('targets'):
            lines.append('- Audit mode: online/mixed. API/browser actions must use the authenticated local broker for '
                         'declared targets only, never direct network. Read probe help once at '
                         + str(root / 'audit-probe-help.json') + ' for request shapes.')
        else:
            lines.append('- Audit mode: offline source. Inspect the scoped source and produce complete evidence. '
                         'Do not read probe help or discover, start or test a network/browser environment.')
        lines += [
            '- Source access is file-scoped to declared audit scope paths: Git metadata and whole-root listings are '
            'not granted. Do not run source Git or repository-root discovery; use the frozen source commit and '
            'scoped file bytes/hashes.',
        ]
        lines.append('- Writes are limited to the artifact directory and session scratch; the source root is read-only.')
    else:
        lines.append('- Writes are limited to the worktree and session scratch; the shared Git directory is protected.')
    lines += [
        '- Scope: preflight checked only the selected CLI, this Python interpreter, and Git version/availability; '
        'other tools and the wider environment are untested.',
        '- Do not repeat bootstrap, version, interpreter, Git availability, PATH, ancestor, or repository-layout '
        'discovery. If a real command fails with a concrete contradictory error, diagnose only that error narrowly.',
    ]
    if (artifact_root is None and contract.get('task_type') != 'audit'
            and 'audit' not in contract):
        lines += _patch_format_guidance()
    return '\n'.join(lines) + '\n'


def _execution_guidance(b, state, contract, binding):
    duration = binding['job_timeout']
    margin = (contract.get('budget') or {}).get('closeout_reserve_seconds', 0)
    text = f'Execution limit: {duration}s. '
    if margin:
        text += f'New requests stop {margin}s early. '
    text += 'Limit reads, use the supplied test command, and reserve one work request for a final factual report.\n'
    if contract.get('budget'):
        snapshot = b.usage_snapshot(state, contract)
        remaining = snapshot.get('budget_state', {}).get('remaining', {})
        work = snapshot.get('work_budget_state', {}).get('remaining', {})
        limits = {key: min(remaining[key], work.get(key, remaining[key]))
                  for key in ('max_requests', 'max_total_tokens') if type(remaining.get(key)) is int}
        text += 'Remaining work allowance: ' + json.dumps(limits, separators=(',', ':')) + '. '
        text += 'Exam reserves are unavailable to work/repair; token thresholds may be overshot by one response.\n'
    return text


def _retain_thread(error, stdout, state):
    """Preserve a CLI-issued identity without treating interrupted work as done."""
    for line in stdout.decode(errors='replace').splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get('type') == 'thread.started':
            thread = event.get('thread_id')
            if (isinstance(thread, str) and 0 < len(thread) <= 128
                    and not any(c.isspace() for c in thread)
                    and state.get('thread_id') in (None, thread)):
                error.thread_id = thread
    return error


def run(b, state, contract, message, endpoint, key, relay_port, phase="work", closeout=None):
    if contract.get('task_type') == 'audit' and phase == 'work':
        from audit_probe import service
        with service(b, state, contract) as probe:
            return _run(b, state, contract, message, endpoint, key, relay_port, phase, probe, closeout)
    return _run(b, state, contract, message, endpoint, key, relay_port, phase, closeout=closeout)


def _run(b, state, contract, message, endpoint, key, relay_port, phase="work", probe=None, closeout=None):
    """Run or resume exactly one executor; work/exam differ only in capabilities."""
    if phase not in ("work", "exam"):
        raise b.GateError("Unknown executor phase")
    exam = phase == "exam"
    discussion = contract.get('task_type') == 'discussion'
    if discussion and exam:
        raise b.GateError('Design discussions have no code acceptance exam')
    if exam and not state.get("thread_id"):
        raise b.GateError("An exam cannot create a new executor conversation")
    HOME, CACHE, INSTALL = b.HOME, b.CACHE, b.INSTALL
    GateError = b.GateError
    backend_bindings, minimal_env, git = b.backend_bindings, b.minimal_env, b.git
    write_json, read_json, encoded, sandbox_profile = b.write_json, b.read_json, b.encoded, b.sandbox_profile
    binding = backend_bindings(contract)['executor']
    peer_home = CACHE / "homes" / state["session_id"]
    peer_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = peer_home / "tmp"
    temp.mkdir(exist_ok=True)
    cfg = binding
    # Isolated CODEX_HOME is for this independent CLI session only, never the leader.
    env = minimal_env()
    key_env = "TELOS_RELAY_TOKEN"
    env.update(CODEX_HOME=str(peer_home), HOME=str(peer_home), TMPDIR=str(temp),
               TMPPREFIX=str(temp / 'zsh'), PYTHONDONTWRITEBYTECODE='1',
               NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost')
    if discussion:
        env['TELOS_DISCUSSION_SCRATCH'] = str(temp)
    env[key_env] = key
    # Apple's /usr/bin/git is an xcrun shim whose cache write is blocked by the
    # sandbox. Give the worker the already-installed Git binary directly.
    git_bin = preflight.git_executable()
    if git_bin:
        shim = peer_home / 'bin'
        shim.mkdir(exist_ok=True, mode=0o700)
        link = shim / 'git'
        if (link.exists() or link.is_symlink()) and not (link.is_symlink() and os.readlink(link) == git_bin):
            link.unlink()
        if not link.is_symlink():
            os.symlink(git_bin, link)
        env['PATH'] = str(shim) + os.pathsep + env.get('PATH', '/usr/bin:/bin')
    if probe:
        import audit
        artifact_root = audit.artifacts(b, state)
        env.update(TELOS_AUDIT_URL=probe['url'], TELOS_AUDIT_TOKEN=probe['token'], TELOS_AUDIT_ARTIFACTS=str(artifact_root),
                   TELOS_AUDIT_SOURCE=state['worktree'])
    # Closed-book exams must not inspect the worktree. Their only writable path
    # is the peer home, so the common Git directory is irrelevant there.
    common_git = None if exam else git(state["worktree"], "rev-parse", "--git-common-dir")
    if common_git is not None:
        common_git = str((Path(state["worktree"]) / common_git).resolve())
    # Provider IDs are persisted CLI protocol identities, not product labels.
    # Preserve them so frozen pre-rename sessions resume without switching identity.
    provider_id = 'ark_exam_loop' if 'backends' not in contract and 'executor' not in contract else 'exam_loop_' + binding['provider']
    settings = [f"model_provider={json.dumps(provider_id)}", f"model={json.dumps(binding['model'])}",
                f"model_providers.{provider_id}.name={json.dumps('TELOS ' + binding['provider'])}",
                f"model_providers.{provider_id}.base_url={json.dumps(endpoint)}",
                f"model_providers.{provider_id}.env_key={json.dumps(key_env)}",
                f'model_providers.{provider_id}.wire_api="responses"',
                'shell_environment_policy.exclude=["ARK_API_KEY","DEEPSEEK_API_KEY","DASHSCOPE_API_KEY","OPENAI_API_KEY","CODEX_API_KEY","TELOS_RELAY_TOKEN","EXAM_LOOP_RELAY_TOKEN"]',
                # macOS cannot apply a second Seatbelt sandbox inside this process.
                # The mandatory OUTER profile supplies the write/read/IPC restrictions.
                "approval_policy=\"never\"", "sandbox_mode=\"danger-full-access\"",
                f"model_reasoning_effort={json.dumps(binding['reasoning_effort'])}", "web_search=\"disabled\"",
                "features.multi_agent=false", "features.multi_agent_v2=false", "features.hooks=false", "features.shell_snapshot=false",
                "features.plugins=false", "features.apps=false", "features.skip_host_skill_discovery=true"]
    if binding['provider'] == 'deepseek':
        catalog_path = peer_home / 'models.json'
        if not catalog_path.exists():
            write_json(catalog_path, read_json(INSTALL / 'deepseek_models.json'))
        settings += [f'model_catalog_json={json.dumps(str(catalog_path))}',
                     'model_reasoning_summary="none"', 'show_raw_agent_reasoning=true',
                     'model_supports_reasoning_summaries=true']
    if binding['provider'] == 'bailian':
        # Keep Bailian's tested tool/context capabilities in the isolated
        # session, never the supervisor; no remote compaction endpoint is used.
        catalog_path = peer_home / 'bailian_models.json'
        if not catalog_path.exists():
            write_json(catalog_path, read_json(INSTALL / 'bailian_models.json'))
        settings += [f'model_catalog_json={json.dumps(str(catalog_path))}',
                     'features.remote_compaction=false', 'model_reasoning_summary="none"',
                     'model_supports_reasoning_summaries=false']
    if binding['provider'] == 'local':
        settings += ['features.remote_compaction=false', 'model_reasoning_summary="none"',
                     'model_supports_reasoning_summaries=false']
    local_config = Path(state["worktree"]) / ".codex/config.toml"
    if local_config.exists():
        raise GateError("Project .codex/config.toml requires explicit compatibility review before isolated peer execution")
    cwd = peer_home / 'exam_room' if exam else Path(state['worktree'])
    if probe:
        cwd = peer_home / 'audit_room'
        cwd.mkdir(exist_ok=True)
        settings += ['project_doc_max_bytes=0']
    if exam:
        cwd.mkdir(exist_ok=True)
        settings += ['project_doc_max_bytes=0', 'features.remote_compaction=false']
    output = peer_home / (uuid.uuid4().hex + ".txt")
    pinned = data_path(HOME, '.local/share/{name}-cli') / 'node_modules/.bin/codex'
    cli = str(pinned) if pinned.exists() else shutil.which("codex")
    if not cli:
        raise GateError("Environment preflight failed before any model request: dedicated Codex CLI not found; "
                        "install the dedicated Telos CLI or restore it on PATH")
    args = [cli, "--no-daemon", "--cd", str(cwd)]
    for setting in settings:
        args += ["-c", setting]
    args += ["exec"]
    if state.get("thread_id"):
        args += ["resume", state["thread_id"]]
    args += ["--ignore-user-config", "--json", "--color", "never"] if not state.get("thread_id") else ["--ignore-user-config", "--json"]
    if exam or probe:
        args += ["--skip-git-repo-check", "--ignore-rules"]
    args += ["-o", str(output), "-"]
    initial = (f"You are an independent coding session using {binding['model']}, communicating with the GPT supervisor through a local MCP bridge. "
               "Work only on the specified task in this isolated git worktree. Do not use subagents, OMX team, MCP, "
               "or read reviewer materials. Leave your changes for the bridge to snapshot; do not push, merge, or deploy. "
               "Report your work to the supervisor for independent verification.\n"
               + "Task:\n" + contract["task"] + "\nPublic context:\n" + contract["context"] + "\n")
    if contract.get('allowed_paths'):
        initial += "Allowed change paths (enforced at submission): " + encoded(contract['allowed_paths']) + "\n"
    if contract.get('experience_context'):
        initial += "\nExperience reference:\n" + contract['experience_context'] + "\n"
    if probe:
        initial += audit.public_prompt(b, state, contract, artifact_root=artifact_root)
    readable_cache = [peer_home] if exam else [state["worktree"], peer_home, common_git]
    writable = [peer_home] if exam else [state['worktree'], peer_home]
    protected = [p for p in (common_git, Path(state['worktree']) / '.git') if p is not None]
    if discussion:
        writable = [peer_home]
        protected.append(state['worktree'])
    audit_sources = []
    if probe:
        audit_sources = audit.source_paths(state, contract['audit'])
        readable_cache = [peer_home, artifact_root, *audit_sources]
        writable = [peer_home, artifact_root]
        protected.append(state['worktree'])
    profile = sandbox_profile(network=False if exam else contract.get('command_network', False),
                              writable=writable, relay_port=relay_port,
                              protected=protected, service_ports=[probe['port']] if probe else [],
                              readable=[] if exam else contract.get('dependency_paths', []),
                              readable_cache=readable_cache)
    guidance = ''
    if phase == 'work':
        # One bounded probe per work invocation, in the same sandbox/env/cwd as
        # the CLI. Exams never probe the worktree; audits use their isolated room.
        evidence = preflight.run(b, cli, cwd, env, profile,
                                 check_repo=(probe is None and contract.get('task_type') != 'audit'),
                                 secrets=(key,))
        if discussion:
            from meetings import GUIDANCE
            guidance = (GUIDANCE + 'Verified source cwd: ' + str(cwd) + '; interpreter: '
                        + evidence['interpreter'] + '. Source and Git metadata are read-only.\n')
        else:
            guidance = WORK_REPLY_GUIDANCE + _work_guidance(evidence, contract, state, artifact_root=artifact_root if probe else None)
        guidance += _execution_guidance(b, state, contract, binding)
    prompt = (initial if not state.get("thread_id") else "") + guidance + "Codex message:\n" + message
    command = ["/usr/bin/sandbox-exec", "-p", profile, *args]
    if closeout is not None:
        closeout.start(cfg["job_timeout"])
    recorder = activity.for_state(b, state, phase, secrets=(key, probe['token'] if probe else ''))
    if recorder:
        recorder.emit('status', 'Executor started' if not exam else 'Closed-book exam started')
    p = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = (activity.communicate(p, prompt.encode(), cfg["job_timeout"], recorder)
                          if recorder else p.communicate(prompt.encode(), timeout=cfg["job_timeout"]))
    except subprocess.TimeoutExpired as expired:
        if recorder:
            out, err = expired.output or b'', expired.stderr or b''
            recorder.emit('error', 'Execution timed out; no acceptance')
        else:
            os.killpg(p.pid, signal.SIGKILL)
            out, err = p.communicate()
        detail = err.decode(errors='replace')[-1800:] + out.decode(errors='replace')[-1200:]
        if key:
            detail = detail.replace(key, '[REDACTED]')
        error = providers.ExecutionTimeout("Execution model timed out; no acceptance. " + detail)
        raise _retain_thread(error, out, state) from None
    events = []
    for line in stdout.decode(errors="replace").splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                events.append(value)
        except ValueError:
            pass
    thread = state.get("thread_id")
    usage = None
    for e in events:
        if e.get("type") == "thread.started":
            thread = e.get("thread_id", thread)
        if e.get("type") == "turn.completed":
            usage = e.get("usage")
    if p.returncode or not thread or not output.exists() or not output.read_text().strip():
        detail = stderr.decode(errors="replace")[-1200:]
        if key:
            detail = detail.replace(key, "[REDACTED]")
        errors = [e for e in events if e.get("type") in ("error", "turn.failed")]
        if errors:
            extra = encoded(errors)
            if key:
                extra = extra.replace(key, "[REDACTED]")
            detail += extra[-1200:]
        raise _retain_thread(GateError("Execution model failed: " + detail), stdout, state)
    if exam and thread != state["thread_id"]:
        raise GateError("Closed-book exam resumed a different executor conversation")
    summary = output.read_text()
    if key:
        summary = summary.replace(key, "[REDACTED]")
    return {"thread_id": thread, "summary": summary if exam else summary[:12000], "summary_truncated": not exam and len(summary) > 12000,
            "usage": usage, "model": binding['model'], 'backend': providers.public_binding(binding)}
