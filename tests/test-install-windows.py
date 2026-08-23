#!/usr/bin/env python3

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = PROJECT_ROOT / "install.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
WINDOWS = os.name == "nt" and POWERSHELL is not None


@unittest.skipUnless(WINDOWS, "Windows PowerShell is required")
class WindowsInstallerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fake_bin = self.root / "fake-bin"
        self.fake_bin.mkdir()
        # Keep environment diagnosis deterministic and fast without hiding Python.
        for name in ("codex", "claude", "grok", "tmux"):
            (self.fake_bin / f"{name}.cmd").write_text(
                "@echo off\r\nexit /b 1\r\n", encoding="ascii"
            )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def install_env(self, home: Path, **extra: str) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in (
                "ONEVOKE_LANG",
                "LC_ALL",
                "LC_MESSAGES",
                "LANG",
            )
        }
        env.update(
            {
                "HOME": str(home),
                "USERPROFILE": str(home),
                "ONEVOKE_CONFIG": str(home / "onevoke-config.json"),
                "KANBAN_DIR": str(home / "kanban"),
                "ONEVOKE_LANG": "cn",
                "PATH": str(self.fake_bin) + os.pathsep + env.get("PATH", ""),
            }
        )
        env.update(extra)
        return env

    def run_installer(
        self,
        home: Path,
        *arguments: str,
        input_text: str | None = None,
        installer: Path = INSTALLER,
        **environment: str,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            str(POWERSHELL),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(installer),
            *arguments,
        ]
        kwargs: dict[str, object] = {
            "env": self.install_env(home, **environment),
            "text": True,
            "encoding": "utf-8",
            "errors": "strict",
            "capture_output": True,
            "check": False,
        }
        if input_text is None:
            kwargs["stdin"] = subprocess.DEVNULL
        else:
            kwargs["input"] = input_text
        return subprocess.run(command, **kwargs)

    def test_installer_copies_payload_keeps_then_removes_legacy_entries(self) -> None:
        home = self.root / "install-home"
        installed_bin = home / ".local" / "bin"
        installed_bin.mkdir(parents=True)
        legacy_names = ("codex-review.sh", "claude-review.sh", "grok-review.sh")
        for name in legacy_names:
            (installed_bin / name).write_text("legacy\n", encoding="utf-8")

        kept = self.run_installer(home)

        self.assertEqual(0, kept.returncode, kept.stderr)
        self.assertEqual("Onevoke 已安装\n", kept.stdout)
        self.assertIn("检测到已退役的 Reviewer 脚本", kept.stderr)
        self.assertIn("已保留旧 Reviewer 脚本", kept.stderr)
        self.assertIn("安装器不会自动修改用户 PATH", kept.stderr)
        for name in legacy_names:
            self.assertEqual("legacy\n", (installed_bin / name).read_text(encoding="utf-8"))

        for source in sorted((PROJECT_ROOT / "bin").iterdir()):
            if source.is_file():
                self.assertEqual(
                    source.read_bytes(),
                    (installed_bin / source.name).read_bytes(),
                    source.name,
                )
        for source in sorted((PROJECT_ROOT / "rules").glob("*.md")):
            self.assertEqual(
                source.read_bytes(),
                (home / ".agents" / source.name).read_bytes(),
                source.name,
            )
        for source in sorted((PROJECT_ROOT / "share" / "kanban-web").iterdir()):
            if source.is_file():
                self.assertEqual(
                    source.read_bytes(),
                    (
                        home
                        / ".local"
                        / "share"
                        / "onevoke"
                        / "kanban-web"
                        / source.name
                    ).read_bytes(),
                    source.name,
                )

        own_rules = home / ".agents" / "AGENTS.md"
        entry_rules = home / ".agents" / "ONEVOKE-AGENTS.md"
        self.assertTrue(own_rules.exists())
        self.assertTrue(os.path.samefile(own_rules, entry_rules))
        self.assertEqual(entry_rules.read_bytes(), own_rules.read_bytes())
        self.assertFalse((installed_bin / "__pycache__").exists())

        removed = self.run_installer(home, input_text="y\n")

        self.assertEqual(0, removed.returncode, removed.stderr)
        self.assertEqual("Onevoke 已安装\n", removed.stdout)
        self.assertIn("已删除旧 Reviewer 脚本", removed.stderr)
        for name in legacy_names:
            self.assertFalse((installed_bin / name).exists(), name)
        self.assertTrue(os.path.samefile(own_rules, entry_rules))

    def test_installer_uses_userprofile_when_home_points_elsewhere(self) -> None:
        user_profile = self.root / "windows-profile"
        unrelated_home = self.root / "git-bash-home"

        result = self.run_installer(user_profile, HOME=str(unrelated_home))

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue((user_profile / ".local" / "bin" / "onevoke.cmd").is_file())
        self.assertTrue((user_profile / ".agents" / "ONEVOKE-AGENTS.md").is_file())
        self.assertFalse((unrelated_home / ".local").exists())
        self.assertFalse((unrelated_home / ".agents").exists())

    def test_installer_preflights_all_targets_before_writing(self) -> None:
        home = self.root / "bad-target-home"
        bad_target = home / ".local" / "share" / "onevoke" / "kanban-web" / "board.js"
        bad_target.mkdir(parents=True)

        result = self.run_installer(home)

        self.assertEqual(1, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertIn("安装目标是目录", result.stderr)
        self.assertFalse((home / ".local" / "bin").exists())
        self.assertFalse((home / ".agents").exists())
        self.assertTrue(bad_target.is_dir())

    def test_installer_preserves_any_existing_agents_entry(self) -> None:
        cases = ("file", "directory")
        for kind in cases:
            with self.subTest(kind=kind):
                home = self.root / f"existing-agents-{kind}"
                agent_rules = home / ".agents" / "AGENTS.md"
                agent_rules.parent.mkdir(parents=True)
                if kind == "file":
                    agent_rules.write_text("本机规则\n", encoding="utf-8")
                else:
                    agent_rules.mkdir()
                    (agent_rules / "keep").write_text("keep\n", encoding="utf-8")

                result = self.run_installer(home)

                self.assertEqual(0, result.returncode, result.stderr)
                if kind == "file":
                    self.assertEqual("本机规则\n", agent_rules.read_text(encoding="utf-8"))
                else:
                    self.assertEqual(
                        "keep\n", (agent_rules / "keep").read_text(encoding="utf-8")
                    )

    def test_installer_reports_welcome_failure_without_rollback(self) -> None:
        home = self.root / "welcome-failure-home"
        config = Path(self.install_env(home)["ONEVOKE_CONFIG"])
        config.parent.mkdir(parents=True)
        config.write_text("not json\n", encoding="utf-8")

        result = self.run_installer(home)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("Onevoke 已安装\n", result.stdout)
        self.assertIn("welcome 未完成", result.stderr)
        self.assertTrue((home / ".local" / "bin" / "onevoke.cmd").is_file())

    def test_installer_language_and_argument_validation(self) -> None:
        home = self.root / "language-home"

        help_result = self.run_installer(home, "--lang", "en", "--help")
        self.assertEqual(0, help_result.returncode, help_result.stderr)
        self.assertIn("usage: install.ps1", help_result.stdout)
        self.assertFalse((home / ".local").exists())

        invalid = self.run_installer(home, "--lang=fr", ONEVOKE_LANG="en")
        self.assertEqual(2, invalid.returncode)
        self.assertIn("error: --lang must be cn or en", invalid.stderr)
        self.assertFalse((home / ".local").exists())

    def test_installer_invokes_absolute_welcome_with_explicit_language(self) -> None:
        project = self.root / "minimal project"
        (project / "bin").mkdir(parents=True)
        shutil.copy2(INSTALLER, project / "install.ps1")
        (project / "bin" / "onevoke.cmd").write_text(
            "@echo off\r\n"
            "> \"%WELCOME_LOG%\" echo %~f0\r\n"
            ">> \"%WELCOME_LOG%\" echo %1\r\n"
            ">> \"%WELCOME_LOG%\" echo %2\r\n"
            ">> \"%WELCOME_LOG%\" echo %3\r\n"
            "exit /b %WELCOME_EXIT%\r\n",
            encoding="ascii",
        )
        home = self.root / "minimal home"
        welcome_log = self.root / "welcome.log"

        result = self.run_installer(
            home,
            "--lang",
            "en",
            installer=project / "install.ps1",
            WELCOME_LOG=str(welcome_log),
            WELCOME_EXIT="0",
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("Onevoke installed\n", result.stdout)
        lines = welcome_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(str((home / ".local" / "bin" / "onevoke.cmd").resolve()), lines[0])
        self.assertEqual(["--lang", "en", "welcome"], lines[1:])

    def test_python_shims_preserve_arguments_utf8_and_exit_status(self) -> None:
        shim_dir = self.root / "shim dir"
        shim_dir.mkdir()
        targets = {
            "onevoke.cmd": "onevoke",
            "kanban.cmd": "kanban",
            "merge-worktree-memory.cmd": "merge-worktree-memory.py",
        }
        script = (
            "import json, os, sys\n"
            "print(json.dumps({\"args\": sys.argv[1:], \"utf8\": sys.flags.utf8_mode, "
            "\"encoding\": sys.stdout.encoding, \"no_bytecode\": "
            "os.environ.get(\"PYTHONDONTWRITEBYTECODE\")}, ensure_ascii=False))\n"
            "raise SystemExit(37 if \"--fail\" in sys.argv else 0)\n"
        )
        for shim_name, target_name in targets.items():
            shutil.copy2(PROJECT_ROOT / "bin" / shim_name, shim_dir / shim_name)
            (shim_dir / target_name).write_text(script, encoding="utf-8")

            with self.subTest(shim=shim_name):
                result = subprocess.run(
                    [str(shim_dir / shim_name), "空 格", "--flag=value"],
                    env=self.install_env(self.root / "shim-home"),
                    text=True,
                    encoding="utf-8",
                    errors="strict",
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(["空 格", "--flag=value"], payload["args"])
                self.assertEqual(1, payload["utf8"])
                self.assertEqual("utf-8", payload["encoding"].lower())
                self.assertEqual("1", payload["no_bytecode"])

                failed = subprocess.run(
                    [str(shim_dir / shim_name), "--fail"],
                    env=self.install_env(self.root / "shim-home"),
                    text=True,
                    encoding="utf-8",
                    errors="strict",
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(37, failed.returncode, failed.stderr)

    def test_python_shims_do_not_resolve_launchers_from_current_directory(self) -> None:
        shim_dir = self.root / "shim R&D"
        shim_dir.mkdir()
        malicious_cwd = self.root / "untrusted-repository"
        malicious_cwd.mkdir()
        for name in ("py.exe", "python.exe", "where.exe"):
            shutil.copy2(os.environ["COMSPEC"], malicious_cwd / name)
        script = (
            "import json, sys\n"
            "print(json.dumps(sys.argv[1:], ensure_ascii=False))\n"
        )
        for shim_name, target_name in {
            "onevoke.cmd": "onevoke",
            "kanban.cmd": "kanban",
            "merge-worktree-memory.cmd": "merge-worktree-memory.py",
        }.items():
            shutil.copy2(PROJECT_ROOT / "bin" / shim_name, shim_dir / shim_name)
            (shim_dir / target_name).write_text(script, encoding="utf-8")

            with self.subTest(shim=shim_name):
                environment = self.install_env(self.root / "shim-home")
                environment.pop("ONEVOKE_PYTHON", None)
                result = subprocess.run(
                    [str(shim_dir / shim_name), "safe argument"],
                    cwd=malicious_cwd,
                    env=environment,
                    text=True,
                    encoding="utf-8",
                    errors="strict",
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(["safe argument"], json.loads(result.stdout))


if __name__ == "__main__":
    unittest.main()
