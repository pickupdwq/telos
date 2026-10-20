#!/usr/bin/env python3
"""Install or explicitly upgrade Telos and its user-scoped supervisor skill."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import uuid

ROOT = Path(__file__).resolve().parent
MIN_CODEX = (0, 163, 0)
SKILL_MANIFEST = ".telos-skill.json"


def preflight(home: Path) -> tuple[Path, str]:
    if sys.platform != "darwin" or sys.version_info < (3, 12):
        raise RuntimeError("Telos requires macOS and Python 3.12+ (Seatbelt isolation).")
    if not Path("/usr/bin/sandbox-exec").is_file():
        raise RuntimeError("macOS sandbox-exec is required; isolation cannot be disabled.")
    for command in ("git", "node"):
        if not shutil.which(command):
            raise RuntimeError(f"Missing dependency: {command}")
    cli_root = home / ".local/share/telos-cli"
    legacy = home / ".local/share/exam_loop-cli"
    if not cli_root.exists() and legacy.exists():
        cli_root = legacy
    codex = cli_root / "node_modules/.bin/codex"
    if not codex.is_file():
        raise RuntimeError('Install the dedicated CLI first: npm install --prefix '
                           '"$HOME/.local/share/telos-cli" @openai/codex@0.163.0')
    result = subprocess.run([str(codex), "--version"], capture_output=True,
                            text=True, check=True, timeout=20)
    version = result.stdout.strip()
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", version)
    if not match or tuple(map(int, match.groups())) < MIN_CODEX:
        raise RuntimeError("Codex CLI 0.163.0+ is required; 0.163.0 is the tested baseline.")
    return codex, version


def ensure_absent(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise RuntimeError(f"Refusing to overwrite {path}. This installer is for fresh installations.")


def shell_launcher(command: list[str], search_paths: list[Path]) -> str:
    paths = os.pathsep.join(map(str, dict.fromkeys(search_paths)))
    return ("#!/bin/sh\nset -eu\n"
            f"export PATH={shlex.quote(paths)}:\"${{PATH:-/usr/bin:/bin}}\"\n"
            f"exec {shlex.join(command)} \"$@\"\n")


def hashes(root: Path) -> dict[str, str]:
    result = {}
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError(f"Expected an ordinary directory: {root}")
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_symlink():
            raise RuntimeError(f"Unexpected symlink: {path}")
        if "__pycache__" in relative.parts:
            continue
        if path.is_file():
            result[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif not path.is_dir():
            raise RuntimeError(f"Unexpected special file: {path}")
    return result


def managed_tree(root: Path, manifest_name: str, name: str, *, mutable=()) -> dict:
    actual = hashes(root)
    if manifest_name not in actual:
        raise RuntimeError(f"No Telos ownership manifest at {root}; existing files were preserved.")
    manifest = json.loads((root / manifest_name).read_text())
    expected = manifest.get("files") if isinstance(manifest, dict) else None
    if not expected or not isinstance(expected, dict) or manifest.get("name") != name:
        raise RuntimeError(f"Invalid ownership manifest at {root}")
    for relative, digest in expected.items():
        if not isinstance(relative, str):
            raise RuntimeError(f"Invalid managed file entry at {root}")
        path = PurePosixPath(relative)
        if (not relative or relative == "." or path.is_absolute()
                or path.as_posix() != relative or ".." in path.parts or "\\" in relative
                or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)):
            raise RuntimeError(f"Invalid managed file entry at {root}")
    actual.pop(manifest_name)
    if actual.keys() != expected.keys():
        raise RuntimeError(f"Added or missing files at {root}; preserve/reconcile local changes before upgrading.")
    changed = [p for p in actual if p not in mutable and actual[p] != expected[p]]
    if changed:
        raise RuntimeError(f"Locally modified files at {root}: {', '.join(changed[:5])}; upgrade refused.")
    return manifest


def assert_idle(home: Path) -> None:
    from runtime.identity import runtime_path
    state = runtime_path(home, "STATE")
    for path in (state / "runs").glob("*/state.json"):
        if json.loads(path.read_text()).get("phase") in ("running", "stale"):
            raise RuntimeError("A Telos job is running/scheduled. Wait or recover it before upgrading.")
    for path in (state / "projects").glob("*/project.json"):
        project = json.loads(path.read_text())
        if (project.get("phase") == "verifying" or project.get("phase") == "running"
                and project.get("continuation", "active") == "active"):
            raise RuntimeError("A Telos project is active. Pause/finish it and wait for jobs/checks before upgrading.")


@contextlib.contextmanager
def installation_locks(prefix: Path, skill: Path, home: Path, upgrade: bool):
    paths = [prefix.parent / ".telos-install.lock", skill.parent.parent / ".telos-skill-install.lock"]
    if upgrade:
        from runtime.identity import runtime_path
        paths.append(runtime_path(home, "STATE") / "coordinator.lock")
    with contextlib.ExitStack() as stack:
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            stream = stack.enter_context(path.open("a+"))
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Another install or task scheduler is active; retry when idle.") from exc
        if upgrade:
            assert_idle(home)
        yield


def install(prefix: Path, bin_dir: Path, *, register: bool = False,
            dry_run: bool = False, upgrade: bool = False, skills_dir: Path | None = None) -> dict:
    home = Path.home()
    # Do not resolve the final component: existing symlinks must fail closed.
    prefix = Path(os.path.abspath(prefix.expanduser()))
    bin_dir = Path(os.path.abspath(bin_dir.expanduser()))
    launcher = bin_dir / "telos"
    previous = None
    if upgrade:
        previous = managed_tree(prefix, "install-manifest.json", "telos", mutable=("config.json",))
        if previous.get("install") != str(prefix) or previous.get("launcher") != str(launcher):
            raise RuntimeError("Upgrade must use the original runtime and launcher paths.")
        if launcher.is_symlink() or not launcher.is_file():
            raise RuntimeError("Existing launcher is missing or a symlink; upgrade refused.")
        launcher_hash = hashlib.sha256(launcher.read_bytes()).hexdigest()
        if previous.get("launcher_sha256", launcher_hash) != launcher_hash:
            raise RuntimeError("Existing launcher was modified; upgrade refused.")
        assert_idle(home)
    else:
        ensure_absent(prefix)
        ensure_absent(launcher)
    skill_metadata = (previous or {}).get("skill", {})
    if not isinstance(skill_metadata, dict):
        raise RuntimeError("Invalid skill ownership metadata in the runtime manifest.")
    old_skill = skill_metadata.get("path")
    if skills_dir is None:
        skills_dir = Path(old_skill).parent if old_skill else home / ".agents/skills"
    skill = Path(os.path.abspath(skills_dir.expanduser())) / "telos"
    if old_skill and old_skill != str(skill):
        raise RuntimeError("Upgrade must preserve the installed skill location; use the original --skills-dir.")
    runtime_path, launcher_path, skill_path = (p.resolve() for p in (prefix, launcher, skill))
    if runtime_path == bin_dir.resolve() or runtime_path in bin_dir.resolve().parents:
        raise RuntimeError("The launcher directory must be outside the runtime directory.")
    if any(a == b or a in b.parents or b in a.parents
           for a, b in ((runtime_path, launcher_path), (runtime_path, skill_path), (launcher_path, skill_path))):
        raise RuntimeError("Runtime, launcher and skill paths must not overlap.")
    previous_skill = None
    if upgrade and (skill.exists() or skill.is_symlink()):
        previous_skill = managed_tree(skill, SKILL_MANIFEST, "telos-skill")
        if previous_skill.get("runtime") != str(prefix):
            raise RuntimeError("The existing skill belongs to another Telos installation.")
    else:
        ensure_absent(skill)
    # Avoid making the same user skill discoverable twice through legacy roots.
    codex_home = Path(os.environ.get("CODEX_HOME", home / ".codex")).expanduser()
    user_skills = (home / ".agents/skills/telos", codex_home / "skills/telos", home / ".codex/skills/telos")
    for other in user_skills if skill.resolve() in {p.resolve() for p in user_skills} else ():
        if other.resolve() != skill.resolve() and (other.exists() or other.is_symlink()):
            raise RuntimeError(f"Another user-level telos skill exists at {other}; existing skill was preserved.")
    codex, codex_version = preflight(home)
    config_file = codex_home / "config.toml"
    already_registered = False
    if register and config_file.exists():
        config = tomllib.loads(config_file.read_text())
        if "telos" in config.get("mcp_servers", {}):
            entry = config["mcp_servers"]["telos"]
            if not upgrade or entry.get("command") != str(launcher) or entry.get("args") != ["serve"]:
                raise RuntimeError("MCP server 'telos' is already registered; existing configuration was preserved.")
            already_registered = True
    files = sorted((ROOT / "runtime").glob("*.py"))
    files.append(ROOT / "runtime/deepseek_models.json")
    files.append(ROOT / "runtime/bailian_models.json")
    files.append(ROOT / 'runtime/audit_browser.cjs')
    if not files or not all(p.is_file() and not p.is_symlink() for p in files):
        raise RuntimeError("Runtime source is missing or contains unexpected symlinks.")
    config_source = prefix / "config.json" if upgrade else ROOT / "config.example.json"
    config_bytes = config_source.read_bytes()
    if not isinstance(json.loads(config_bytes), dict):
        raise RuntimeError("Runtime config must be a JSON object.")
    version_match = re.search(r"^VERSION = ['\"]([^'\"]+)['\"]", (ROOT / "runtime/identity.py").read_text(), re.M)
    if not version_match:
        raise RuntimeError("Runtime version is missing.")
    version = version_match[1]
    if previous and tuple(map(int, previous["version"].split("."))) > tuple(map(int, version.split("."))):
        raise RuntimeError("Downgrading a managed runtime is not supported.")
    skill_source = ROOT / "skills/telos"
    skill_hashes = hashes(skill_source)
    if ("SKILL.md" not in skill_hashes or "agents/openai.yaml" not in skill_hashes
            or f'  version: "{version}"' not in (skill_source / "SKILL.md").read_text()):
        raise RuntimeError("The bundled skill is missing or does not match the runtime version.")
    manifest = {"name": "telos", "version": version, "python": (previous or {}).get("python", sys.executable),
                "codex": codex_version, "codex_executable": str(codex),
                "codex_launcher": str(prefix / "commands/codex"), "install": str(prefix),
                "launcher": str(launcher), "files": {},
                "skill": {"path": str(skill), "version": version, "scope": "user", "files": skill_hashes}}
    if dry_run:
        return {**manifest, "dry_run": True, "register": register, "upgrade": upgrade}

    backups = {}
    with installation_locks(prefix, skill, home, upgrade):
        # Recheck after locking; dry-run/preflight are not write authorization for changed files.
        if upgrade:
            if managed_tree(prefix, "install-manifest.json", "telos", mutable=("config.json",)) != previous:
                raise RuntimeError("Runtime ownership changed during upgrade preparation.")
            if hashlib.sha256(launcher.read_bytes()).hexdigest() != launcher_hash or launcher.is_symlink():
                raise RuntimeError("Launcher changed during upgrade preparation.")
            config_bytes = config_source.read_bytes()
            if not isinstance(json.loads(config_bytes), dict):
                raise RuntimeError("Runtime config must be a JSON object.")
        else:
            ensure_absent(prefix)
            ensure_absent(launcher)
        if previous_skill:
            if managed_tree(skill, SKILL_MANIFEST, "telos-skill") != previous_skill:
                raise RuntimeError("Skill ownership changed during upgrade preparation.")
        else:
            ensure_absent(skill)
        bin_dir.mkdir(parents=True, exist_ok=True)
        skill.parent.mkdir(parents=True, exist_ok=True)
        stages, published = [], []
        launcher_created = False
        try:
            stage = Path(tempfile.mkdtemp(prefix=".telos-install-", dir=prefix.parent))
            stages.append(stage)
            skill_stage = Path(tempfile.mkdtemp(prefix=".telos-skill-stage-", dir=skill.parent.parent))
            stages.append(skill_stage)
            for source in files:
                shutil.copyfile(source, stage / source.name)
            shutil.copy2(config_source, stage / "config.json")
            shutil.copyfile(ROOT / "LICENSE", stage / "LICENSE")
            (stage / "commands").mkdir()
            search_paths = [prefix / "commands", codex.parent, Path(manifest["python"]).parent,
                            Path(shutil.which("node")).parent]
            (stage / "commands/codex").write_text(shell_launcher([str(codex)], search_paths))
            (stage / "commands/codex").chmod(0o755)
            for relative in skill_hashes:
                destination = skill_stage / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(skill_source / relative, destination)
            if hashes(skill_stage) != skill_hashes:
                raise RuntimeError("Skill source changed during installation.")
            (skill_stage / SKILL_MANIFEST).write_text(json.dumps({"name": "telos-skill", "version": version,
                "runtime": str(prefix), "files": skill_hashes}, indent=2) + "\n")
            launcher_text = shell_launcher([manifest["python"], str(prefix / "telos.py")], search_paths)
            manifest["launcher_sha256"] = launcher_hash if upgrade else hashlib.sha256(launcher_text.encode()).hexdigest()
            manifest["files"] = hashes(stage)
            (stage / "install-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
            if (config_source.read_bytes() != config_bytes
                    or (stage / "config.json").read_bytes() != config_bytes):
                raise RuntimeError("Runtime configuration changed during installation; retry when idle.")
            for source, destination, old, label in ((stage, prefix, bool(previous), "runtime"),
                                                   (skill_stage, skill, bool(previous_skill), "skill")):
                backup = None
                if old:
                    parent = prefix.parent if label == "runtime" else skill.parent.parent
                    backup = parent / (".telos-" + label + "-backup-" + uuid.uuid4().hex)
                    destination.rename(backup)
                    backups[label] = str(backup)
                else:
                    ensure_absent(destination)
                published.append([destination, backup, False])
                source.rename(destination)
                published[-1][2] = True
            if not upgrade:
                with launcher.open("x") as stream:
                    launcher_created = True
                    stream.write(launcher_text)
                launcher.chmod(0o755)
        except BaseException:
            if launcher_created:
                launcher.unlink(missing_ok=True)
            for destination, backup, replaced in reversed(published):
                if replaced:
                    shutil.rmtree(destination)
                if backup:
                    backup.rename(destination)
            raise
        finally:
            for path in stages:
                if path.exists():
                    shutil.rmtree(path)
    if register and not already_registered:
        try:
            subprocess.run([str(codex), "mcp", "add", "telos", "--", str(launcher), "serve"],
                           check=True, timeout=30)
        except (subprocess.SubprocessError, OSError) as exc:
            raise RuntimeError(f"Installed at {prefix}, but MCP registration failed. Retry: "
                               f"{shlex.join([str(codex), 'mcp', 'add', 'telos', '--', str(launcher), 'serve'])}") from exc
    return {**manifest, "registered": register, "upgraded": upgrade, "backups": backups}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, default=Path.home() / ".local/share/telos")
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin")
    parser.add_argument("--register", action="store_true", help="Register the telos MCP server in Codex")
    parser.add_argument("--skills-dir", type=Path, help="User skill root (default: ~/.agents/skills)")
    parser.add_argument("--upgrade", action="store_true", help="Upgrade a managed installation; preserve config and back up runtime/skill")
    parser.add_argument("--dry-run", action="store_true", help="Check prerequisites and print the plan without writing")
    args = parser.parse_args()
    try:
        result = install(args.prefix, args.bin_dir, register=args.register, dry_run=args.dry_run,
                         upgrade=args.upgrade, skills_dir=args.skills_dir)
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as exc:
        parser.exit(1, f"telos: {exc}\n")
    print(json.dumps(result, indent=2))
    if not args.dry_run:
        print("Next: telos doctor. Restart Codex if needed to load the MCP tools and $telos skill. "
              "Configure a provider with telos auth only when its credentials are missing.")


if __name__ == "__main__":
    main()
