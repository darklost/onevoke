#!/usr/bin/env python3

import json
import os
import pty
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ONEVOKE = PROJECT_ROOT / "bin" / "onevoke"
ROLES = ("PM", "CSA", "Hacker", "QA")


class OnevokeCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.config = self.home / ".config" / "onevoke" / "config.json"
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        self.env = os.environ.copy()
        self.env["HOME"] = str(self.home)
        self.env["ONEVOKE_CONFIG"] = str(self.config)
        self.env["PATH"] = str(self.fake_bin)
        self.env.pop("NO_COLOR", None)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def fake_command(self, name: str, body: str | None = None) -> Path:
        command = self.fake_bin / name
        command.write_text(
            body or f"#!/bin/sh\nprintf '%s\\n' '{name} test-version'\n",
            encoding="utf-8",
        )
        command.chmod(0o755)
        return command

    def install_fake_environment(self, *, tmux: bool, memsearch: bool = True) -> None:
        for name in (
            "onevoke",
            "kanban",
            "codex-review.sh",
            "grok-review.sh",
            "merge-worktree-memory.py",
            "codex",
            "claude",
            "grok",
        ):
            self.fake_command(name)
        if tmux:
            self.fake_command("tmux")
        if memsearch:
            self.fake_command("memsearch")
            hooks = self.home / ".codex" / "hooks.json"
            hooks.parent.mkdir(parents=True)
            hooks.write_text(
                json.dumps(
                    {
                        "hooks": [
                            "memsearch/session-start.sh",
                            "memsearch/user-prompt-submit.sh",
                            "memsearch/stop.sh",
                        ]
                    }
                ),
                encoding="utf-8",
            )

    def install_fake_memsearch_tools(
        self, revision: str, source_status: str = ""
    ) -> tuple[Path, Path, Path]:
        uv_log = self.root / "uv.log"
        git_log = self.root / "git.log"
        bash_log = self.root / "bash.log"
        memsearch_template = self.root / "memsearch-template"
        memsearch_template.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        memsearch_template.chmod(0o755)
        self.env.update(
            {
                "UV_LOG": str(uv_log),
                "GIT_LOG": str(git_log),
                "BASH_LOG": str(bash_log),
                "FAKE_BIN": str(self.fake_bin),
                "MEMSEARCH_TEMPLATE": str(memsearch_template),
                "MEMSEARCH_REVISION": revision,
                "MEMSEARCH_SOURCE_STATUS": source_status,
                "ONEVOKE_MEMSEARCH_SOURCE": str(self.root / "memsearch-source"),
            }
        )
        self.fake_command(
            "uv",
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" > \"$UV_LOG\"\n"
            "/bin/cp \"$MEMSEARCH_TEMPLATE\" \"$FAKE_BIN/memsearch\"\n",
        )
        self.fake_command(
            "git",
            "#!/bin/sh\n"
            "if [ \"$1\" = '-C' ]; then\n"
            "  if [ \"$3\" = 'rev-parse' ]; then\n"
            "    printf '%s\\n' \"$MEMSEARCH_REVISION\"\n"
            "  elif [ \"$3\" = 'status' ] && "
            "[ -n \"$MEMSEARCH_SOURCE_STATUS\" ]; then\n"
            "    printf '%s\\n' \"$MEMSEARCH_SOURCE_STATUS\"\n"
            "  fi\n"
            "  exit 0\n"
            "fi\n"
            "printf '%s\\n' \"$*\" > \"$GIT_LOG\"\n"
            "for destination in \"$@\"; do :; done\n"
            "/bin/mkdir -p \"$destination/plugins/codex/scripts\"\n"
            "printf '%s\\n' '#!/bin/sh' > "
            "\"$destination/plugins/codex/scripts/install.sh\"\n",
        )
        self.fake_command(
            "bash",
            "#!/bin/sh\nprintf '%s\\n' \"$*\" > \"$BASH_LOG\"\n",
        )
        return uv_log, git_log, bash_log

    def run_command(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(ONEVOKE), *args],
            env=self.env,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            check=False,
        )

    def run_on_tty(self, answers: str, *args: str) -> tuple[int, str]:
        master, slave = pty.openpty()
        process = subprocess.Popen(
            [sys.executable, str(ONEVOKE), *args],
            env=self.env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
        )
        os.close(slave)
        seen: list[bytes] = []

        def drain() -> None:
            while True:
                try:
                    data = os.read(master, 4096)
                except OSError:
                    break
                if not data:
                    break
                seen.append(data)

        reader = threading.Thread(target=drain)
        reader.start()
        try:
            os.write(master, answers.encode("utf-8"))
            returncode = process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise
        finally:
            os.close(master)
            reader.join(timeout=5)
        return returncode, b"".join(seen).decode("utf-8", "replace")

    def test_noninteractive_welcome_only_diagnoses(self) -> None:
        self.install_fake_environment(tmux=False)

        result = self.run_command("welcome")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("当前没有交互终端", result.stderr)
        self.assertIn("请在终端运行 onevoke welcome", result.stderr)
        self.assertFalse(self.config.exists())

    def test_welcome_saves_per_role_reviewers_and_foreground_launcher(self) -> None:
        self.install_fake_environment(tmux=False)

        # Codex 执行; PM/CSA/Hacker/QA 依次选 Codex/Grok/Codex/Grok;
        # 拒绝安装 tmux; MemSearch 已就绪; 最后确认保存.
        returncode, output = self.run_on_tty(
            "1\n1\n2\n1\n2\n2\n1\n", "welcome"
        )

        self.assertEqual(0, returncode, output)
        self.assertIn("\033[1;31m[!] 未安装 tmux", output)
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual("codex", config["kanban_agent"])
        self.assertEqual("foreground", config["launcher"])
        self.assertEqual(
            {"PM": "codex", "CSA": "grok", "Hacker": "codex", "QA": "grok"},
            config["reviewers"],
        )
        self.assertTrue(config["memsearch"]["enabled"])
        self.assertTrue(config["welcome_complete"])
        self.assertEqual(0o600, self.config.stat().st_mode & 0o777)
        self.assertIn("配置已保存", output)

        second = self.run_command("welcome")
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertIn("welcome 已完成", second.stderr)

    def test_welcome_installs_tmux_with_available_package_manager(self) -> None:
        self.install_fake_environment(tmux=False)
        brew_log = self.root / "brew.log"
        tmux_template = self.root / "tmux-template"
        tmux_template.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        tmux_template.chmod(0o755)
        self.env.update(
            {
                "BREW_LOG": str(brew_log),
                "FAKE_BIN": str(self.fake_bin),
                "TMUX_TEMPLATE": str(tmux_template),
            }
        )
        self.fake_command(
            "brew",
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" > \"$BREW_LOG\"\n"
            "/bin/cp \"$TMUX_TEMPLATE\" \"$FAKE_BIN/tmux\"\n",
        )

        # Codex 执行和四个 Reviewer; 同意安装 tmux; MemSearch 已就绪; 保存.
        returncode, output = self.run_on_tty(
            "1\n1\n1\n1\n1\n1\n1\n", "welcome"
        )

        self.assertEqual(0, returncode, output)
        self.assertEqual("install tmux", brew_log.read_text(encoding="utf-8").strip())
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual("tmux", config["launcher"])

    def test_welcome_still_asks_about_memsearch_with_only_grok(self) -> None:
        for name in (
            "onevoke",
            "kanban",
            "codex-review.sh",
            "grok-review.sh",
            "merge-worktree-memory.py",
            "grok",
        ):
            self.fake_command(name)

        # Grok 执行和四个 Reviewer; 拒绝 tmux; 拒绝只装 MemSearch CLI; 保存.
        returncode, output = self.run_on_tty(
            "1\n1\n1\n1\n1\n2\n2\n1\n", "welcome"
        )

        self.assertEqual(0, returncode, output)
        self.assertIn("MemSearch CLI 未安装, 是否仍然只安装 CLI?", output)
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual("grok", config["kanban_agent"])
        self.assertEqual({role: "grok" for role in ROLES}, config["reviewers"])
        self.assertFalse(config["memsearch"]["enabled"])

    def test_invalid_config_is_reported_without_fallback(self) -> None:
        self.config.parent.mkdir(parents=True)
        self.config.write_text("not json\n", encoding="utf-8")

        result = self.run_command("config")

        self.assertEqual(1, result.returncode)
        self.assertIn("读取配置失败", result.stderr)

    def test_welcome_installs_pinned_memsearch_and_codex_plugin(self) -> None:
        self.install_fake_environment(tmux=True, memsearch=False)
        uv_log, git_log, bash_log = self.install_fake_memsearch_tools(
            "177d23b0e76f4a3a4a8bb920bd1bed421bb664d8"
        )

        # Codex 执行和四个 Reviewer; tmux launcher; 安装 MemSearch; 保存.
        returncode, output = self.run_on_tty(
            "1\n1\n1\n1\n1\n1\n1\n1\n", "welcome"
        )

        self.assertEqual(0, returncode, output)
        self.assertEqual(
            "tool install -U memsearch[onnx]==0.4.15",
            uv_log.read_text(encoding="utf-8").strip(),
        )
        self.assertIn("--branch v0.4.15", git_log.read_text(encoding="utf-8"))
        self.assertTrue((self.root / "memsearch-source").is_dir())
        self.assertIn(
            "plugins/codex/scripts/install.sh",
            bash_log.read_text(encoding="utf-8"),
        )
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertTrue(config["memsearch"]["enabled"])

    def test_welcome_rejects_unexpected_memsearch_source_revision(self) -> None:
        self.install_fake_environment(tmux=True, memsearch=False)
        _, _, bash_log = self.install_fake_memsearch_tools("unexpected-revision")

        returncode, output = self.run_on_tty(
            "1\n1\n1\n1\n1\n1\n1\n1\n", "welcome"
        )

        self.assertEqual(0, returncode, output)
        self.assertIn("MemSearch 源码校验失败", output)
        self.assertFalse(bash_log.exists())
        self.assertFalse((self.root / "memsearch-source").exists())
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertFalse(config["memsearch"]["enabled"])

    def test_welcome_rejects_a_dirty_cached_memsearch_source(self) -> None:
        self.install_fake_environment(tmux=True, memsearch=False)
        _, _, bash_log = self.install_fake_memsearch_tools(
            "177d23b0e76f4a3a4a8bb920bd1bed421bb664d8",
            " M plugins/codex/scripts/install.sh",
        )
        installer = (
            self.root
            / "memsearch-source"
            / "plugins"
            / "codex"
            / "scripts"
            / "install.sh"
        )
        installer.parent.mkdir(parents=True)
        installer.write_text("#!/bin/sh\n", encoding="utf-8")

        returncode, output = self.run_on_tty(
            "1\n1\n1\n1\n1\n1\n1\n1\n", "welcome"
        )

        self.assertEqual(0, returncode, output)
        self.assertIn("MemSearch 源码工作树不干净", output)
        self.assertFalse(bash_log.exists())
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertFalse(config["memsearch"]["enabled"])

    def test_welcome_decline_keeps_existing_config_unchanged(self) -> None:
        self.install_fake_environment(tmux=True)
        existing = {
            "schema_version": 1,
            "welcome_complete": True,
            "kanban_agent": "grok",
            "launcher": "foreground",
            "reviewers": {role: "grok" for role in ROLES},
            "memsearch": {"enabled": True},
        }
        self.config.parent.mkdir(parents=True)
        self.config.write_text(
            json.dumps(existing, ensure_ascii=False, indent=4) + "\n",
            encoding="utf-8",
        )
        before = self.config.read_bytes()

        # 重选所有项目, 最后拒绝保存.
        returncode, output = self.run_on_tty(
            "1\n1\n1\n1\n1\n1\n2\n", "welcome", "--reset"
        )

        self.assertEqual(1, returncode, output)
        self.assertIn("用户取消, 配置未更改", output)
        self.assertEqual(before, self.config.read_bytes())

    def test_review_dispatches_role_to_configured_wrapper(self) -> None:
        log = self.root / "review.log"
        wrapper = self.fake_command(
            "grok-review.sh",
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$REVIEW_LOG\"\n",
        )
        self.assertTrue(wrapper.exists())
        self.env["REVIEW_LOG"] = str(log)
        config = {
            "schema_version": 1,
            "welcome_complete": True,
            "kanban_agent": "codex",
            "launcher": "tmux",
            "reviewers": {role: "codex" for role in ROLES},
            "memsearch": {"enabled": False},
        }
        config["reviewers"]["QA"] = "grok"
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(config), encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                str(ONEVOKE),
                "review",
                "/worktree",
                "base",
                "commit",
                "qa",
                "目标",
            ],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            ["/worktree", "base", "commit", "QA", "目标"],
            log.read_text(encoding="utf-8").splitlines(),
        )


if __name__ == "__main__":
    unittest.main()
