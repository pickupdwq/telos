"""Persistent executor CLI driver; provider credentials stay in the bridge relay."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import uuid
import providers
from identity import data_path


def run(b, state, contract, message, endpoint, key, relay_port, phase="work"):
    if contract.get('task_type') == 'audit' and phase == 'work':
        from audit_probe import service
        with service(b, state, contract) as probe:
            return _run(b, state, contract, message, endpoint, key, relay_port, phase, probe)
    return _run(b, state, contract, message, endpoint, key, relay_port, phase)


def _run(b, state, contract, message, endpoint, key, relay_port, phase="work", probe=None):
    """Run or resume exactly one executor; work/exam differ only in capabilities."""
    if phase not in ("work", "exam"):
        raise b.GateError("Unknown executor phase")
    exam = phase == "exam"
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
    env[key_env] = key
    if probe:
        import audit
        artifact_root = audit.artifacts(b, state)
        env.update(TELOS_AUDIT_URL=probe['url'], TELOS_AUDIT_TOKEN=probe['token'], TELOS_AUDIT_ARTIFACTS=str(artifact_root),
                   TELOS_AUDIT_SOURCE=state['worktree'])
    common_git = git(state["worktree"], "rev-parse", "--git-common-dir")
    common_git = str((Path(state["worktree"]) / common_git).resolve())
    # Provider IDs are persisted CLI protocol identities, not product labels.
    # Preserve them so frozen pre-rename sessions resume without switching identity.
    provider_id = 'ark_exam_loop' if 'backends' not in contract and 'executor' not in contract else 'exam_loop_' + binding['provider']
    settings = [f"model_provider={json.dumps(provider_id)}", f"model={json.dumps(binding['model'])}",
                f"model_providers.{provider_id}.name={json.dumps('TELOS ' + binding['provider'])}",
                f"model_providers.{provider_id}.base_url={json.dumps(endpoint)}",
                f"model_providers.{provider_id}.env_key={json.dumps(key_env)}",
                f'model_providers.{provider_id}.wire_api="responses"',
                'shell_environment_policy.exclude=["ARK_API_KEY","DEEPSEEK_API_KEY","OPENAI_API_KEY","CODEX_API_KEY","TELOS_RELAY_TOKEN","EXAM_LOOP_RELAY_TOKEN"]',
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
    args = [str(pinned) if pinned.exists() else (shutil.which("codex") or "codex"), "--no-daemon", "--cd", str(cwd)]
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
               "Keep your reply compact: changed files, checks performed, remaining issues. Never claim final acceptance.\n"
               + "Task:\n" + contract["task"] + "\nPublic context:\n" + contract["context"] + "\n")
    if contract.get('allowed_paths'):
        initial += "Allowed change paths (enforced at submission): " + encoded(contract['allowed_paths']) + "\n"
    if contract.get('experience_context'):
        initial += "\nExperience reference:\n" + contract['experience_context'] + "\n"
    if probe:
        initial += audit.public_prompt(b, state, contract)
    prompt = (initial if not state.get("thread_id") else "") + "Codex message:\n" + message
    readable_cache = [peer_home] if exam else [state["worktree"], peer_home, common_git]
    writable = [peer_home] if exam else [state['worktree'], peer_home]
    protected = [common_git, Path(state['worktree']) / '.git']
    if probe:
        readable_cache = [peer_home, artifact_root, *audit.source_paths(state, contract['audit'])]
        writable = [peer_home, artifact_root]
        protected.append(state['worktree'])
    command = ["/usr/bin/sandbox-exec", "-p", sandbox_profile(network=False if exam else contract.get('command_network', False),
               writable=writable, relay_port=relay_port,
               protected=protected, service_ports=[probe['port']] if probe else [],
               readable=[] if exam else contract.get('dependency_paths', []),
               readable_cache=readable_cache), *args]
    p = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = p.communicate(prompt.encode(), timeout=cfg["job_timeout"])
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        out, err = p.communicate()
        detail = (err.decode(errors='replace')[-1800:] + out.decode(errors='replace')[-1200:]).replace(key, '[REDACTED]')
        raise GateError("Execution model timed out; no acceptance. " + detail) from None
    events = []
    for line in stdout.decode(errors="replace").splitlines():
        try:
            events.append(json.loads(line))
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
        detail = stderr.decode(errors="replace").replace(key, "[REDACTED]")[-1200:]
        errors = [e for e in events if e.get("type") in ("error", "turn.failed")]
        if errors:
            detail += encoded(errors).replace(key, "[REDACTED]")[-1200:]
        raise GateError("Execution model failed: " + detail)
    if exam and thread != state["thread_id"]:
        raise GateError("Closed-book exam resumed a different executor conversation")
    summary = output.read_text().replace(key, "[REDACTED]")
    return {"thread_id": thread, "summary": summary if exam else summary[:12000], "summary_truncated": not exam and len(summary) > 12000,
            "usage": usage, "model": binding['model'], 'backend': providers.public_binding(binding)}
