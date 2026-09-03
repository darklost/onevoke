#!/usr/bin/env python3

import json
import os
import shutil
import subprocess
import sys
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
        working_directory: Path | None = None,
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
        if working_directory is not None:
            kwargs["cwd"] = working_directory
        if input_text is None:
            kwargs["stdin"] = subprocess.DEVNULL
        else:
            kwargs["input"] = input_text
        return subprocess.run(command, **kwargs)

    def assert_private_acl(self, path: Path) -> None:
        acl = subprocess.run(
            ["icacls.exe", str(path)],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, acl.returncode, acl.stderr)
        self.assertNotIn("(I)", acl.stdout, acl.stdout)
        self.assertEqual(1, acl.stdout.count("(F)"), acl.stdout)

    def write_valid_config(
        self,
        home: Path,
        *,
        welcome_complete: bool,
        language: str,
    ) -> Path:
        path = Path(self.install_env(home)["ONEVOKE_CONFIG"])
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "welcome_complete": welcome_complete,
                    "kanban_agent": "codex",
                    "launcher": "console",
                    "reviewers": {
                        "PM": "codex",
                        "CSA": "codex",
                        "Hacker": "codex",
                        "QA": "codex",
                    },
                    "memsearch": {"enabled": False},
                    "language": language,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def python3_only_path(self) -> str:
        where_executable = shutil.which("where.exe")
        self.assertIsNotNone(where_executable)
        shutil.copy2(where_executable, self.fake_bin / "py.exe")
        shutil.copy2(where_executable, self.fake_bin / "python.exe")
        python3_dir = self.root / "python3-only"
        python3_dir.mkdir(exist_ok=True)
        python3 = python3_dir / "python3.exe"
        source = Path(sys.executable)
        shutil.copy2(source, python3)
        for dll in source.parent.glob("python*.dll"):
            shutil.copy2(dll, python3_dir / dll.name)
        system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        environment = os.environ.copy()
        environment["PATH"] = os.pathsep.join((str(python3_dir), str(system32)))
        probe = subprocess.run(
            [
                str(python3),
                "-X",
                "utf8",
                "-c",
                "import sys; raise SystemExit(sys.version_info.major != 3)",
            ],
            env=environment,
            capture_output=True,
            check=False,
        )
        if probe.returncode != 0:
            self.skipTest("cannot execute a copied python3.exe")
        return os.pathsep.join((str(self.fake_bin), str(python3_dir), str(system32)))

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

    def test_valid_legacy_config_is_migrated_to_private_acl_when_loaded(self) -> None:
        home = self.root / "legacy-config-home"
        installed = self.run_installer(home)
        self.assertEqual(0, installed.returncode, installed.stderr)

        config_path = self.write_valid_config(
            home,
            welcome_complete=True,
            language="cn",
        )
        reset_acl = subprocess.run(
            ["icacls.exe", str(config_path), "/reset"],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, reset_acl.returncode, reset_acl.stderr)
        inherited_acl = subprocess.run(
            ["icacls.exe", str(config_path)],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, inherited_acl.returncode, inherited_acl.stderr)
        self.assertIn("(I)", inherited_acl.stdout, inherited_acl.stdout)

        loaded = subprocess.run(
            [
                sys.executable,
                "-B",
                "-X",
                "utf8",
                str(home / ".local" / "bin" / "onevoke"),
                "config",
                "--json",
            ],
            env=self.install_env(home),
            text=True,
            encoding="utf-8",
            errors="strict",
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, loaded.returncode, loaded.stderr)
        self.assertTrue(json.loads(loaded.stdout)["welcome_complete"])
        self.assert_private_acl(config_path)

    def test_config_acl_migration_failure_is_explicit(self) -> None:
        home = self.root / "acl-failure-home"
        installed = self.run_installer(home)
        self.assertEqual(0, installed.returncode, installed.stderr)
        self.write_valid_config(
            home,
            welcome_complete=False,
            language="en",
        )
        installed_bin = home / ".local" / "bin"
        probe = (
            "import sys\n"
            f"sys.path.insert(0, {str(installed_bin)!r})\n"
            "import onevoke_config as config\n"
            "def denied(file, path):\n"
            "    raise OSError('acl denied')\n"
            "config.tighten_private_open_file_permissions = denied\n"
            "try:\n"
            "    config.load_config()\n"
            "except config.ConfigError as error:\n"
            "    print(error)\n"
            "else:\n"
            "    raise SystemExit('load_config accepted an ACL migration failure')\n"
        )

        failed = subprocess.run(
            [sys.executable, "-B", "-X", "utf8", "-c", probe],
            env=self.install_env(home, ONEVOKE_LANG="en"),
            text=True,
            encoding="utf-8",
            errors="strict",
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, failed.returncode, failed.stderr)
        self.assertIn("failed to tighten config file permissions", failed.stdout)
        self.assertIn("acl denied", failed.stdout)

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

    def test_installer_falls_back_from_broken_py_to_python(self) -> None:
        home = self.root / "python-fallback-home"
        home.mkdir(parents=True)
        self.write_valid_config(
            home,
            welcome_complete=True,
            language="en",
        )
        where_executable = shutil.which("where.exe")
        self.assertIsNotNone(where_executable)
        shutil.copy2(where_executable, self.fake_bin / "py.exe")
        isolated_path = os.pathsep.join(
            (str(self.fake_bin), str(Path(sys.executable).parent))
        )

        result = self.run_installer(
            home,
            PATH=isolated_path,
            ONEVOKE_LANG="cn",
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("Onevoke installed\n", result.stdout)
        self.assertNotIn("welcome did not complete", result.stderr)

    def test_installer_falls_back_from_broken_py_and_python_to_python3(self) -> None:
        home = self.root / "python3-fallback-home"
        home.mkdir(parents=True)
        self.write_valid_config(
            home,
            welcome_complete=True,
            language="en",
        )

        result = self.run_installer(
            home,
            PATH=self.python3_only_path(),
            ONEVOKE_LANG="cn",
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("Onevoke installed\n", result.stdout)
        self.assertNotIn("welcome did not complete", result.stderr)

    def test_installer_skips_zero_byte_python_and_tries_next(self) -> None:
        home = self.root / "zero-byte-python-home"
        home.mkdir(parents=True)
        self.write_valid_config(
            home,
            welcome_complete=True,
            language="en",
        )
        windows_apps = self.root / "Microsoft" / "WindowsApps"
        windows_apps.mkdir(parents=True)
        (windows_apps / "python.exe").write_bytes(b"")
        (windows_apps / "python3.exe").write_bytes(b"")
        isolated_path = os.pathsep.join(
            (
                str(windows_apps),
                str(self.fake_bin),
                str(Path(sys.executable).parent),
            )
        )

        result = self.run_installer(
            home,
            PATH=isolated_path,
            ONEVOKE_LANG="cn",
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("Onevoke installed\n", result.stdout)
        self.assertNotIn("welcome did not complete", result.stderr)

    def test_installer_skips_current_directory_python_and_tries_next(self) -> None:
        home = self.root / "current-directory-python-home"
        home.mkdir(parents=True)
        self.write_valid_config(
            home,
            welcome_complete=True,
            language="en",
        )
        untrusted_directory = self.root / "untrusted-repository"
        untrusted_directory.mkdir()
        where_executable = shutil.which("where.exe")
        self.assertIsNotNone(where_executable)
        shutil.copy2(where_executable, untrusted_directory / "python.exe")
        isolated_path = os.pathsep.join(
            (
                str(untrusted_directory),
                str(self.fake_bin),
                str(Path(sys.executable).parent),
            )
        )

        result = self.run_installer(
            home,
            working_directory=untrusted_directory,
            PATH=isolated_path,
            ONEVOKE_LANG="cn",
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("Onevoke installed\n", result.stdout)
        self.assertNotIn("welcome did not complete", result.stderr)

    def test_installer_skips_provider_location_python_after_set_location(self) -> None:
        home = self.root / "provider-location-python-home"
        home.mkdir(parents=True)
        self.write_valid_config(
            home,
            welcome_complete=True,
            language="en",
        )
        untrusted_directory = self.root / "provider-location-repository"
        untrusted_directory.mkdir()
        malicious_python = untrusted_directory / "python.exe"
        shutil.copy2(sys.executable, malicious_python)
        hook_directory = self.root / "python-hook"
        hook_directory.mkdir()
        marker = self.root / "malicious-python-ran"
        (hook_directory / "sitecustomize.py").write_text(
            "import os, sys\n"
            "from pathlib import Path\n"
            "if Path(sys.executable).resolve() == "
            "Path(os.environ['ONEVOKE_MALICIOUS_PYTHON']).resolve():\n"
            "    Path(os.environ['ONEVOKE_MALICIOUS_MARKER']).write_text("
            "'ran', encoding='utf-8')\n",
            encoding="utf-8",
        )
        isolated_path = os.pathsep.join(
            (
                str(untrusted_directory),
                str(self.fake_bin),
                str(Path(sys.executable).parent),
            )
        )
        environment = self.install_env(
            home,
            PATH=isolated_path,
            ONEVOKE_LANG="cn",
            PYTHONPATH=str(hook_directory),
            ONEVOKE_MALICIOUS_PYTHON=str(malicious_python),
            ONEVOKE_MALICIOUS_MARKER=str(marker),
            ONEVOKE_TEST_INSTALLER=str(INSTALLER),
            ONEVOKE_TEST_PROVIDER_LOCATION=str(untrusted_directory),
        )
        malicious_probe = subprocess.run(
            [str(malicious_python), "-X", "utf8", "-c", "pass"],
            env=environment,
            text=True,
            encoding="utf-8",
            errors="strict",
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, malicious_probe.returncode, malicious_probe.stderr)
        self.assertTrue(marker.is_file(), "malicious Python probe did not run")
        marker.unlink()
        command = (
            "Set-Location -LiteralPath $env:ONEVOKE_TEST_PROVIDER_LOCATION; "
            "if ([Environment]::CurrentDirectory -eq "
            "$ExecutionContext.SessionState.Path.CurrentFileSystemLocation.Path) { "
            "throw 'test requires distinct process and provider locations' }; "
            "& $env:ONEVOKE_TEST_INSTALLER"
        )

        result = subprocess.run(
            [
                str(POWERSHELL),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            env=environment,
            text=True,
            encoding="utf-8",
            errors="strict",
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("Onevoke installed\n", result.stdout)
        self.assertFalse(marker.exists(), "provider-location python.exe was executed")
        self.assertNotIn("welcome did not complete", result.stderr)

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

    def test_python_shims_query_path_with_where_exe_name_syntax(self) -> None:
        for shim_name in (
            "onevoke.cmd",
            "kanban.cmd",
            "merge-worktree-memory.cmd",
            "onevoke-review.cmd",
        ):
            content = (PROJECT_ROOT / "bin" / shim_name).read_text(encoding="ascii")
            with self.subTest(shim=shim_name):
                for launcher in ("py.exe", "python.exe", "python3.exe"):
                    self.assertIn(
                        f'where.exe\" {launcher} ',
                        content,
                    )
                    self.assertNotIn(
                        f'where.exe\" \"$PATH:{launcher}',
                        content,
                    )

    def test_python_shims_do_not_resolve_launchers_from_current_directory(self) -> None:
        shim_dir = self.root / "shim R&D"
        shim_dir.mkdir()
        malicious_cwd = self.root / "untrusted-repository"
        malicious_cwd.mkdir()
        for name in ("py.exe", "python.exe", "python3.exe", "where.exe"):
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

    def test_python_shims_fall_back_to_python3(self) -> None:
        shim_dir = self.root / "python3 shim"
        shim_dir.mkdir()
        script = (
            "import json, sys\n"
            "print(json.dumps(sys.argv[1:], ensure_ascii=False))\n"
        )
        isolated_path = self.python3_only_path()
        for shim_name, target_name in {
            "onevoke.cmd": "onevoke",
            "kanban.cmd": "kanban",
            "merge-worktree-memory.cmd": "merge-worktree-memory.py",
            "onevoke-review.cmd": "onevoke_review.py",
        }.items():
            shutil.copy2(PROJECT_ROOT / "bin" / shim_name, shim_dir / shim_name)
            (shim_dir / target_name).write_text(script, encoding="utf-8")

            with self.subTest(shim=shim_name):
                environment = self.install_env(
                    self.root / "python3-shim-home",
                    PATH=isolated_path,
                )
                environment.pop("ONEVOKE_PYTHON", None)
                result = subprocess.run(
                    [str(shim_dir / shim_name), "python3 fallback"],
                    env=environment,
                    text=True,
                    encoding="utf-8",
                    errors="strict",
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(["python3 fallback"], json.loads(result.stdout))

    def test_python_shims_skip_zero_byte_python_and_tries_next(self) -> None:
        shim_dir = self.root / "zero-byte shim"
        shim_dir.mkdir()
        windows_apps = self.root / "Microsoft" / "WindowsApps"
        windows_apps.mkdir(parents=True)
        (windows_apps / "py.exe").write_bytes(b"")
        (windows_apps / "python.exe").write_bytes(b"")
        (windows_apps / "python3.exe").write_bytes(b"")
        script = (
            "import json, sys\n"
            "print(json.dumps(sys.argv[1:], ensure_ascii=False))\n"
        )
        isolated_path = os.pathsep.join(
            (
                str(windows_apps),
                str(self.fake_bin),
                str(Path(sys.executable).parent),
            )
        )
        for shim_name, target_name in {
            "onevoke.cmd": "onevoke",
            "kanban.cmd": "kanban",
            "merge-worktree-memory.cmd": "merge-worktree-memory.py",
        }.items():
            shutil.copy2(PROJECT_ROOT / "bin" / shim_name, shim_dir / shim_name)
            (shim_dir / target_name).write_text(script, encoding="utf-8")

            with self.subTest(shim=shim_name):
                environment = self.install_env(
                    self.root / "zero-byte-shim-home",
                    PATH=isolated_path,
                )
                environment.pop("ONEVOKE_PYTHON", None)
                result = subprocess.run(
                    [str(shim_dir / shim_name), "skip store alias"],
                    env=environment,
                    text=True,
                    encoding="utf-8",
                    errors="strict",
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(["skip store alias"], json.loads(result.stdout))


if __name__ == "__main__":
    unittest.main()
