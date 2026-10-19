#!/usr/bin/env python3
"""Install a fresh Telos runtime without copying credentials or task state."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parent
MIN_CODEX = (0, 163, 0)


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


def install(prefix: Path, bin_dir: Path, *, register: bool = False,
            dry_run: bool = False) -> dict:
    home = Path.home()
    # Do not resolve the final component: existing symlinks must fail closed.
    prefix = Path(os.path.abspath(prefix.expanduser()))
    bin_dir = Path(os.path.abspath(bin_dir.expanduser()))
    launcher = bin_dir / "telos"
    ensure_absent(prefix)
    ensure_absent(launcher)
    if prefix == bin_dir or prefix in bin_dir.parents:
        raise RuntimeError("The launcher directory must be outside the runtime directory.")
    codex, codex_version = preflight(home)
    codex_home = Path(os.environ.get("CODEX_HOME", home / ".codex")).expanduser()
    config_file = codex_home / "config.toml"
    if register and config_file.exists():
        config = tomllib.loads(config_file.read_text())
        if "telos" in config.get("mcp_servers", {}):
            raise RuntimeError("MCP server 'telos' is already registered; existing configuration was preserved.")
    files = sorted((ROOT / "runtime").glob("*.py"))
    files.append(ROOT / "runtime/deepseek_models.json")
    files.append(ROOT / 'runtime/audit_browser.cjs')
    if not files or not all(p.is_file() and not p.is_symlink() for p in files):
        raise RuntimeError("Runtime source is missing or contains unexpected symlinks.")
    config_text = (ROOT / "config.example.json").read_text()
    json.loads(config_text)
    version_match = re.search(r"^VERSION = ['\"]([^'\"]+)['\"]", (ROOT / "runtime/identity.py").read_text(), re.M)
    if not version_match:
        raise RuntimeError("Runtime version is missing.")
    manifest = {"name": "telos", "version": version_match[1], "python": sys.executable,
                "codex": codex_version, "codex_executable": str(codex),
                "codex_launcher": str(prefix / "commands/codex"), "install": str(prefix),
                "launcher": str(launcher), "files": {}}
    if dry_run:
        return {**manifest, "dry_run": True, "register": register}

    prefix.parent.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".telos-install-", dir=prefix.parent))
    installed = False
    launcher_created = False
    try:
        for source in files:
            shutil.copyfile(source, stage / source.name)
        (stage / "config.json").write_text(config_text)
        shutil.copyfile(ROOT / "LICENSE", stage / "LICENSE")
        (stage / "commands").mkdir()
        node_dir = Path(shutil.which("node")).parent
        search_paths = [prefix / "commands", codex.parent, Path(sys.executable).parent, node_dir]
        (stage / "commands/codex").write_text(shell_launcher([str(codex)], search_paths))
        (stage / "commands/codex").chmod(0o755)
        manifest["files"] = {str(p.relative_to(stage)): hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in stage.rglob("*") if p.is_file()}
        (stage / "install-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        # Recheck immediately before publishing; never replace an existing install.
        ensure_absent(prefix)
        stage.rename(prefix)
        installed = True
        with launcher.open("x") as stream:
            launcher_created = True
            stream.write(shell_launcher([sys.executable, str(prefix / "telos.py")], search_paths))
        launcher.chmod(0o755)
    except BaseException:
        if launcher_created:
            launcher.unlink(missing_ok=True)
        if installed:
            shutil.rmtree(prefix)
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    if register:
        try:
            subprocess.run([str(codex), "mcp", "add", "telos", "--", str(launcher), "serve"],
                           check=True, timeout=30)
        except (subprocess.SubprocessError, OSError) as exc:
            raise RuntimeError(f"Installed at {prefix}, but MCP registration failed. Retry: "
                               f"{shlex.join([str(codex), 'mcp', 'add', 'telos', '--', str(launcher), 'serve'])}") from exc
    return {**manifest, "registered": register}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, default=Path.home() / ".local/share/telos")
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin")
    parser.add_argument("--register", action="store_true", help="Register the telos MCP server in Codex")
    parser.add_argument("--dry-run", action="store_true", help="Check prerequisites and print the plan without writing")
    args = parser.parse_args()
    try:
        result = install(args.prefix, args.bin_dir, register=args.register, dry_run=args.dry_run)
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as exc:
        parser.exit(1, f"telos: {exc}\n")
    print(json.dumps(result, indent=2))
    if not args.dry_run:
        print("Next: telos auth deepseek; telos doctor. Restart Codex to load MCP tools.")


if __name__ == "__main__":
    main()
