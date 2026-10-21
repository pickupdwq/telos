"""Runtime/skill installation and upgrade regressions; never uses the real home."""
import fcntl
import json
import os
from pathlib import Path
import shutil
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
        self.skill = self.home / ".agents/skills/telos"
        self.state = self.home / "state"
        self.codex = self.home / "codex"
        self.codex.write_text("#!/bin/sh\nprintf 'codex-cli 0.163.0\\n'\n")
        self.codex.chmod(0o755)
        for mock in (patch.object(Path, "home", return_value=self.home),
                     patch.object(install, "preflight", return_value=(self.codex, "codex-cli 0.163.0")),
                     patch.dict(os.environ, {"CODEX_HOME": str(self.home / ".codex"),
                                             "TELOS_STATE_DIR": str(self.state),
                                             "TELOS_CACHE_DIR": str(self.home / "cache")})):
            mock.start()
            self.addCleanup(mock.stop)

    def test_install_manifest_launchers_and_mcp_with_shell_metacharacters(self):
        result = install.install(self.prefix, self.bin_dir)
        self.assertEqual(result["version"], "3.7.4")
        self.assertTrue((self.prefix / 'audit_browser.cjs').is_file())
        self.assertTrue((self.prefix / 'local_executor.py').is_file())
        self.assertTrue((self.prefix / 'chat_adapter.py').is_file())
        self.assertTrue((self.prefix / 'bailian_models.json').is_file())
        cfg = json.loads((self.prefix / "config.json").read_text())
        self.assertEqual(cfg["default_executor_profile"], "deepseek_official")
        self.assertEqual(cfg['local_executor'], {'active': None, 'endpoints': {}})
        self.assertEqual(cfg['profiles']['bailian_deepseek']['provider'], 'bailian')
        self.assertFalse((self.prefix / "credentials.json").exists())
        manifest = json.loads((self.prefix / "install-manifest.json").read_text())
        for relative, digest in manifest["files"].items():
            self.assertEqual(install.hashlib.sha256((self.prefix / relative).read_bytes()).hexdigest(), digest)
        skill_manifest = install.managed_tree(self.skill, install.SKILL_MANIFEST, "telos-skill")
        self.assertEqual(skill_manifest["runtime"], str(self.prefix))
        self.assertEqual(skill_manifest["files"], result["skill"]["files"])
        self.assertEqual(skill_manifest["version"], result["version"])
        self.assertEqual((self.skill / "SKILL.md").read_bytes(), (install.ROOT / "skills/telos/SKILL.md").read_bytes())
        self.assertTrue((self.skill / "references/acceptance.md").is_file())
        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"
        env = {**os.environ, "TELOS_STATE_DIR": str(self.home / "state"), "TELOS_CACHE_DIR": str(self.home / "cache")}
        response = subprocess.run([str(self.bin_dir / "telos"), "serve"], input=request,
                                  text=True, capture_output=True, check=True, timeout=10, env=env)
        self.assertEqual(json.loads(response.stdout)["result"]["serverInfo"]["name"], "telos")
        self.assertEqual(json.loads(response.stdout)["result"]["serverInfo"]["version"], result["version"])
        command = subprocess.run([str(self.prefix / "commands/codex"), "--version"],
                                 capture_output=True, text=True, check=True)
        self.assertIn("0.163.0", command.stdout)
        observer = subprocess.run([str(self.bin_dir / "telos"), "watch", "--help"],
                                  capture_output=True, text=True, check=True, timeout=10, env=env)
        self.assertIn("--history", observer.stdout)
        self.assertIn("--pane", observer.stdout)

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
        self.assertFalse(self.skill.parent.exists())
        self.assertFalse(self.state.exists())

    def test_register_passes_literal_paths_and_never_changes_model_settings(self):
        with patch.object(install.subprocess, "run") as run, \
                patch.object(install, 'watch_environment') as watch_setup:
            install.install(self.prefix, self.bin_dir, register=True)
        self.assertEqual(run.call_args.args[0], [str(self.codex), "mcp", "add", "telos", "--",
                                               str(self.bin_dir / "telos"), "serve"])
        self.assertFalse((self.home / ".codex/config.toml").exists())
        watch_setup.assert_called_once_with(self.home / '.codex/config.toml', self.bin_dir / 'telos')

    def test_unmanaged_or_duplicate_user_skill_is_preserved(self):
        for destination in (self.skill, self.home / ".codex/skills/telos"):
            with self.subTest(destination=destination):
                destination.mkdir(parents=True)
                (destination / "SKILL.md").write_text("User instructions")
                with self.assertRaisesRegex(RuntimeError, "overwrite|Another user-level"):
                    install.install(self.prefix, self.bin_dir)
                self.assertEqual((destination / "SKILL.md").read_text(), "User instructions")
                self.assertFalse(self.prefix.exists())
                shutil.rmtree(destination)

    def test_custom_skill_root_isolated_from_existing_user_skill(self):
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("Existing user skill")
        isolated = self.home / "smoke/skills"
        result = install.install(self.prefix, self.bin_dir, skills_dir=isolated)
        self.assertEqual(result["skill"]["path"], str(isolated / "telos"))
        self.assertTrue((isolated / "telos/SKILL.md").is_file())
        self.assertEqual((self.skill / "SKILL.md").read_text(), "Existing user skill")
        upgraded = install.install(self.prefix, self.bin_dir, upgrade=True)
        self.assertEqual(upgraded["skill"]["path"], str(isolated / "telos"))

    def test_upgrade_preserves_config_credentials_state_and_registration(self):
        install.install(self.prefix, self.bin_dir)
        config = self.prefix / "config.json"
        content = b'{\r\n  "default_executor_profile": "local", "custom": true\r\n}\r\n'
        config.write_bytes(content)
        config.chmod(0o600)
        credentials = self.home / ".config/telos/credentials.json"
        credentials.parent.mkdir(parents=True)
        credentials.write_text('{"deepseek": "fake-test-key", "ark": "fake-other-key"}\n')
        credentials.chmod(0o600)
        record = self.state / "runs/task/state.json"
        record.parent.mkdir(parents=True)
        record.write_text('{"phase": "accepted", "historical": true}\n')
        registration = self.home / ".codex/config.toml"
        registration.parent.mkdir()
        registration.write_text('model = "existing-supervisor"\n[mcp_servers.telos]\ncommand = '
                                + json.dumps(str(self.bin_dir / "telos")) + '\nargs = ["serve"]\n')
        untouched = {p: p.read_bytes() for p in (credentials, record, registration, self.bin_dir / "telos")}
        runtime_before, skill_before = install.hashes(self.prefix), install.hashes(self.skill)
        with patch.object(install.subprocess, "run") as run:
            result = install.install(self.prefix, self.bin_dir, upgrade=True, register=True)
            run.assert_not_called()
        self.assertTrue(result["upgraded"])
        self.assertEqual(config.read_bytes(), content)
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        for path, data in untouched.items():
            if path == registration:
                self.assertEqual(path.read_bytes().replace(b'env_vars = ["TMUX", "TMUX_PANE"]\n', b''), data)
                self.assertEqual(Path(result['watch_environment']['backup']).read_bytes(), data)
            else:
                self.assertEqual(path.read_bytes(), data)
        self.assertEqual(credentials.stat().st_mode & 0o777, 0o600)
        self.assertEqual(install.hashes(Path(result["backups"]["runtime"])), runtime_before)
        skill_backup = Path(result["backups"]["skill"])
        self.assertEqual(install.hashes(skill_backup), skill_before)
        self.assertNotIn(self.skill.parent, skill_backup.parents)
        install.managed_tree(self.prefix, "install-manifest.json", "telos")
        install.managed_tree(self.skill, install.SKILL_MANIFEST, "telos-skill")

    def test_watch_environment_preserves_other_settings_multiline_values_and_is_idempotent(self):
        config = self.home / 'config.toml'
        original = ('model = "keep-model"\n[mcp_servers.telos]\ncommand = '
                    + json.dumps(str(self.bin_dir / 'telos')) + '\nargs = ["serve"]\n'
                    'env_vars = [\n "EXISTING",\n { name = "OTHER", source = "local" },\n]\n'
                    '[mcp_servers.telos.env]\nCUSTOM = "keep"\n'
                    '[mcp_servers.other]\ncommand = "unrelated"\n').encode()
        config.write_bytes(original)
        result = install.watch_environment(config, self.bin_dir / 'telos')
        self.assertEqual(Path(result['backup']).read_bytes(), original)
        parsed = install.tomllib.loads(config.read_text())
        self.assertEqual(parsed['model'], 'keep-model')
        self.assertEqual(parsed['mcp_servers']['telos']['env_vars'],
                         ['EXISTING', {'name': 'OTHER', 'source': 'local'}, 'TMUX', 'TMUX_PANE'])
        self.assertEqual(parsed['mcp_servers']['telos']['env'], {'CUSTOM': 'keep'})
        self.assertEqual(parsed['mcp_servers']['other'], {'command': 'unrelated'})
        updated = config.read_bytes()
        self.assertFalse(install.watch_environment(config, self.bin_dir / 'telos')['changed'])
        self.assertEqual(config.read_bytes(), updated)

    def test_upgrade_supports_legacy_manifest_and_retains_launcher(self):
        install.install(self.prefix, self.bin_dir)
        shutil.rmtree(self.skill)
        manifest_path = self.prefix / "install-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest.pop("skill")
        manifest.pop("launcher_sha256")
        manifest.update(version="3.3.0", local_patches={"already_verified": True})
        manifest_path.write_text(json.dumps(manifest))
        launcher = self.bin_dir / "telos"
        before = launcher.read_bytes()
        result = install.install(self.prefix, self.bin_dir, upgrade=True)
        self.assertEqual(result["version"], "3.7.4")
        self.assertEqual(launcher.read_bytes(), before)
        self.assertTrue((self.skill / "SKILL.md").is_file())
        self.assertEqual(set(result["backups"]), {"runtime"})

    def test_upgrade_refuses_local_runtime_skill_and_launcher_changes(self):
        install.install(self.prefix, self.bin_dir)
        for path in (self.prefix / "telos.py", self.skill / "SKILL.md", self.bin_dir / "telos"):
            with self.subTest(path=path):
                original = path.read_bytes()
                path.write_bytes(original + b"\n# user change\n")
                before = (install.hashes(self.prefix), install.hashes(self.skill))
                with self.assertRaisesRegex(RuntimeError, "modified"):
                    install.install(self.prefix, self.bin_dir, upgrade=True)
                self.assertEqual(before, (install.hashes(self.prefix), install.hashes(self.skill)))
                self.assertEqual(path.read_bytes(), original + b"\n# user change\n")
                path.write_bytes(original)

    def test_upgrade_refuses_added_missing_files_and_symlinks(self):
        install.install(self.prefix, self.bin_dir)
        extra = self.prefix / "user-notes.txt"
        extra.write_text("keep")
        with self.assertRaisesRegex(RuntimeError, "Added or missing"):
            install.install(self.prefix, self.bin_dir, upgrade=True)
        self.assertEqual(extra.read_text(), "keep")
        extra.unlink()
        managed = self.skill / "references/planning.md"
        data = managed.read_bytes()
        managed.unlink()
        with self.assertRaisesRegex(RuntimeError, "Added or missing"):
            install.install(self.prefix, self.bin_dir, upgrade=True)
        managed.symlink_to(self.home / "missing")
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            install.install(self.prefix, self.bin_dir, upgrade=True)
        self.assertTrue(managed.is_symlink())
        managed.unlink()
        managed.write_bytes(data)

    def test_upgrade_dry_run_preserves_everything(self):
        install.install(self.prefix, self.bin_dir)
        before = install.hashes(self.home)
        result = install.install(self.prefix, self.bin_dir, upgrade=True, dry_run=True)
        self.assertTrue(result["upgrade"])
        self.assertEqual(install.hashes(self.home), before)

    def test_upgrade_refuses_active_jobs_including_paused_or_stale_jobs(self):
        install.install(self.prefix, self.bin_dir)
        record = self.state / "runs/task/state.json"
        record.parent.mkdir(parents=True)
        for phase, continuation in (("running", "active"), ("running", "paused"), ("stale", "cancelled")):
            with self.subTest(phase=phase, continuation=continuation):
                record.write_text(json.dumps({"phase": phase, "continuation": continuation}))
                before = install.hashes(self.prefix)
                with self.assertRaisesRegex(RuntimeError, "job is running"):
                    install.install(self.prefix, self.bin_dir, upgrade=True)
                self.assertEqual(install.hashes(self.prefix), before)

    def test_upgrade_checks_active_projects_and_allows_idle_paused_project(self):
        install.install(self.prefix, self.bin_dir)
        record = self.state / "projects/task/project.json"
        record.parent.mkdir(parents=True)
        for phase, continuation in (("running", "active"), ("verifying", "paused")):
            with self.subTest(phase=phase, continuation=continuation):
                record.write_text(json.dumps({"phase": phase, "continuation": continuation}))
                with self.assertRaisesRegex(RuntimeError, "project is active"):
                    install.install(self.prefix, self.bin_dir, upgrade=True)
        record.write_text('{"phase":"running","continuation":"paused"}')
        before = record.read_bytes()
        result = install.install(self.prefix, self.bin_dir, upgrade=True)
        self.assertTrue(result["upgraded"])
        self.assertEqual(record.read_bytes(), before)

    def test_upgrade_refuses_while_scheduler_holds_lock(self):
        install.install(self.prefix, self.bin_dir)
        self.state.mkdir()
        with (self.state / "coordinator.lock").open("a+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, "scheduler is active"):
                install.install(self.prefix, self.bin_dir, upgrade=True)

    def test_upgrade_refuses_downgrade_and_changed_install_paths(self):
        install.install(self.prefix, self.bin_dir)
        with self.assertRaisesRegex(RuntimeError, "original runtime and launcher"):
            install.install(self.prefix, self.home / "another-bin", upgrade=True)
        with self.assertRaisesRegex(RuntimeError, "skill location"):
            install.install(self.prefix, self.bin_dir, upgrade=True, skills_dir=self.home / "another-skills")
        manifest_path = self.prefix / "install-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["version"] = "99.0.0"
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(RuntimeError, "Downgrading"):
            install.install(self.prefix, self.bin_dir, upgrade=True)

    def test_failed_skill_publication_rolls_back_fresh_and_upgrade(self):
        rename = Path.rename

        def fail_skill_stage(path, target):
            if path.name.startswith(".telos-skill-stage-"):
                raise OSError("simulated skill publication failure")
            return rename(path, target)

        with patch.object(Path, "rename", fail_skill_stage):
            with self.assertRaisesRegex(OSError, "publication failure"):
                install.install(self.prefix, self.bin_dir)
        self.assertFalse(self.prefix.exists())
        self.assertFalse(self.skill.exists())
        self.assertFalse((self.bin_dir / "telos").exists())
        install.install(self.prefix, self.bin_dir)
        before = (install.hashes(self.prefix), install.hashes(self.skill), (self.bin_dir / "telos").read_bytes())
        with patch.object(Path, "rename", fail_skill_stage):
            with self.assertRaisesRegex(OSError, "publication failure"):
                install.install(self.prefix, self.bin_dir, upgrade=True)
        self.assertEqual(before, (install.hashes(self.prefix), install.hashes(self.skill),
                                  (self.bin_dir / "telos").read_bytes()))
        self.assertFalse(list(self.home.rglob(".telos-*-backup-*")))
        self.assertFalse(list(self.home.rglob(".telos-skill-stage-*")))

    def test_symlink_parent_cannot_hide_overlapping_install_paths(self):
        self.home.joinpath("alias").symlink_to(self.home, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "must not overlap"):
            install.install(self.prefix, self.bin_dir,
                            skills_dir=self.home / "alias" / self.prefix.name / "skills")
        self.assertFalse(self.prefix.exists())

    def test_malformed_manifest_is_rejected_without_writes(self):
        self.prefix.mkdir()
        manifest = self.prefix / "install-manifest.json"
        for value in ([], {"name": "telos", "files": {"../outside": "a" * 64}}):
            with self.subTest(value=value):
                manifest.write_text(json.dumps(value))
                with self.assertRaisesRegex(RuntimeError, "Invalid"):
                    install.install(self.prefix, self.bin_dir, upgrade=True)
                self.assertEqual(json.loads(manifest.read_text()), value)
                self.assertFalse(self.skill.exists())


if __name__ == "__main__":
    unittest.main()
