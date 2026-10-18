"""Fresh-install, quoting and no-overwrite checks; never uses the real home."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import install


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="telos installer ")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.prefix = self.home / "install 'quoted' $path"
        self.bin_dir = self.home / "bin space"
        self.codex = self.home / "codex"
        self.codex.write_text("#!/bin/sh\nprintf 'codex-cli 0.163.0\\n'\n")
        self.codex.chmod(0o755)
        for mock in (patch.object(Path, "home", return_value=self.home),
                     patch.object(install, "preflight", return_value=(self.codex, "codex-cli 0.163.0")),
                     patch.dict(os.environ, {"CODEX_HOME": str(self.home / ".codex")})):
            mock.start()
            self.addCleanup(mock.stop)

    def test_install_manifest_launchers_and_mcp_with_shell_metacharacters(self):
        result = install.install(self.prefix, self.bin_dir)
        self.assertEqual(result["version"], "3.1.0")
        cfg = json.loads((self.prefix / "config.json").read_text())
        self.assertEqual(cfg["default_executor_profile"], "deepseek_official")
        self.assertFalse((self.prefix / "credentials.json").exists())
        manifest = json.loads((self.prefix / "install-manifest.json").read_text())
        for relative, digest in manifest["files"].items():
            self.assertEqual(install.hashlib.sha256((self.prefix / relative).read_bytes()).hexdigest(), digest)
        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"
        env = {**os.environ, "TELOS_STATE_DIR": str(self.home / "state"), "TELOS_CACHE_DIR": str(self.home / "cache")}
        response = subprocess.run([str(self.bin_dir / "telos"), "serve"], input=request,
                                  text=True, capture_output=True, check=True, timeout=10, env=env)
        self.assertEqual(json.loads(response.stdout)["result"]["serverInfo"]["name"], "telos")
        command = subprocess.run([str(self.prefix / "commands/codex"), "--version"],
                                 capture_output=True, text=True, check=True)
        self.assertIn("0.163.0", command.stdout)

    def test_existing_install_is_never_overwritten(self):
        self.prefix.mkdir()
        sentinel = self.prefix / "config.json"
        sentinel.write_text("keep me")
        with self.assertRaisesRegex(RuntimeError, "overwrite"):
            install.install(self.prefix, self.bin_dir)
        self.assertEqual(sentinel.read_text(), "keep me")
        self.assertFalse(self.bin_dir.exists())

    def test_existing_launcher_and_dangling_symlink_are_preserved(self):
        self.bin_dir.mkdir()
        launcher = self.bin_dir / "telos"
        launcher.symlink_to(self.home / "missing")
        with self.assertRaisesRegex(RuntimeError, "overwrite"):
            install.install(self.prefix, self.bin_dir)
        self.assertTrue(launcher.is_symlink())
        self.assertFalse(self.prefix.exists())

    def test_existing_mcp_registration_fails_before_writes(self):
        config = self.home / ".codex/config.toml"
        config.parent.mkdir()
        config.write_text('[mcp_servers.telos]\ncommand = "existing"\n')
        with self.assertRaisesRegex(RuntimeError, "already registered"):
            install.install(self.prefix, self.bin_dir, register=True)
        self.assertFalse(self.prefix.exists())
        self.assertIn('"existing"', config.read_text())

    def test_dry_run_creates_nothing(self):
        result = install.install(self.prefix, self.bin_dir, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertFalse(self.prefix.exists())
        self.assertFalse(self.bin_dir.exists())

    def test_register_passes_literal_paths_and_never_changes_model_settings(self):
        with patch.object(install.subprocess, "run") as run:
            install.install(self.prefix, self.bin_dir, register=True)
        self.assertEqual(run.call_args.args[0], [str(self.codex), "mcp", "add", "telos", "--",
                                               str(self.bin_dir / "telos"), "serve"])
        self.assertFalse((self.home / ".codex/config.toml").exists())


if __name__ == "__main__":
    unittest.main()
