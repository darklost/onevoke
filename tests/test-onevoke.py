#!/usr/bin/env python3

import importlib.machinery
import importlib.util
import json
import os
import pty
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ONEVOKE = PROJECT_ROOT / "bin" / "onevoke"
ROLES = ("PM", "CSA", "Hacker", "QA")


def load_onevoke_module():
    loader = importlib.machinery.SourceFileLoader("onevoke_under_test", str(ONEVOKE))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError("无法加载 onevoke 测试模块")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(PROJECT_ROOT / "bin"))
    try:
        loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


class OnevokeCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.language = mock.patch.dict(os.environ, {"ONEVOKE_LANG": "zh"})
        self.language.start()
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
        self.language.stop()

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
            self.fake_command(
                "memsearch", "#!/bin/sh\nprintf '%s\\n' 'memsearch, version 0.4.15'\n"
            )
            self.install_fake_codex_memsearch_hooks()
            self.fake_command(
                "git",
                "#!/bin/sh\n"
                "if [ \"$1\" = '-C' ] && [ \"$3\" = 'remote' ]; then\n"
                "  printf '%s\\n' 'https://github.com/zilliztech/memsearch.git'\n"
                "fi\n",
            )

    def install_fake_codex_memsearch_hooks(self) -> Path:
        hooks_dir = (
            self.root / "verified-memsearch-source" / "plugins" / "codex" / "hooks"
        )
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "common.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        events = {
            "SessionStart": "session-start.sh",
            "UserPromptSubmit": "user-prompt-submit.sh",
            "Stop": "stop.sh",
        }
        hooks: dict[str, list[dict[str, object]]] = {}
        for event, name in events.items():
            script = hooks_dir / name
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            script.chmod(0o755)
            hooks[event] = [
                {"hooks": [{"type": "command", "command": f"bash {script}"}]}
            ]
        hooks_file = self.home / ".codex" / "hooks.json"
        hooks_file.parent.mkdir(parents=True, exist_ok=True)
        hooks_file.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
        return hooks_file

    def install_fake_claude_memsearch_plugin(self, version: str) -> Path:
        marketplace = (
            self.home / ".claude" / "plugins" / "marketplaces" / "memsearch-plugins"
        )
        source_plugin = marketplace / "plugins" / "claude-code"
        (source_plugin / ".claude-plugin").mkdir(parents=True)
        (source_plugin / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "memsearch", "version": version}),
            encoding="utf-8",
        )
        hooks_dir = source_plugin / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "common.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (hooks_dir / "helper.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        hooks = {}
        for event, name in {
            "SessionStart": "session-start.sh",
            "UserPromptSubmit": "user-prompt-submit.sh",
            "Stop": "stop.sh",
        }.items():
            script = hooks_dir / name
            script.write_text(f"#!/bin/sh\n# {event} hook:\n", encoding="utf-8")
            script.chmod(0o755)
            hooks[event] = [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"bash ${{CLAUDE_PLUGIN_ROOT}}/hooks/{name}",
                        }
                    ]
                }
            ]
        (hooks_dir / "hooks.json").write_text(
            json.dumps({"hooks": hooks}), encoding="utf-8"
        )
        git = shutil.which("git")
        if git is None:
            self.fail("测试需要 git")
        subprocess.run([git, "init", "-q", str(marketplace)], check=True)
        subprocess.run(
            [git, "-C", str(marketplace), "add", "plugins/claude-code"],
            check=True,
        )
        subprocess.run(
            [
                git,
                "-C",
                str(marketplace),
                "-c",
                "user.name=Onevoke Test",
                "-c",
                "user.email=onevoke@example.invalid",
                "commit",
                "-q",
                "-m",
                "fixture",
            ],
            check=True,
        )
        subprocess.run(
            [
                git,
                "-C",
                str(marketplace),
                "remote",
                "add",
                "origin",
                "https://github.com/zilliztech/memsearch.git",
            ],
            check=True,
        )
        revision = subprocess.run(
            [git, "-C", str(marketplace), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        plugin = (
            self.home
            / ".claude"
            / "plugins"
            / "cache"
            / "memsearch-plugins"
            / "memsearch"
            / version
        )
        shutil.copytree(source_plugin, plugin)
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            json.dumps({"enabledPlugins": {"memsearch@memsearch-plugins": True}}),
            encoding="utf-8",
        )
        installed = self.home / ".claude" / "plugins" / "installed_plugins.json"
        installed.parent.mkdir(parents=True, exist_ok=True)
        installed.write_text(
            json.dumps(
                {
                    "plugins": {
                        "memsearch@memsearch-plugins": [
                            {
                                "installPath": str(plugin),
                                "version": version,
                                "gitCommitSha": revision,
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        marketplaces = self.home / ".claude" / "plugins" / "known_marketplaces.json"
        marketplaces.write_text(
            json.dumps(
                {
                    "memsearch-plugins": {
                        "source": {
                            "source": "github",
                            "repo": "zilliztech/memsearch",
                        },
                        "installLocation": str(marketplace),
                    }
                }
            ),
            encoding="utf-8",
        )
        self.fake_command(
            "git",
            "#!/bin/sh\n"
            "if [ \"$1\" = '-C' ] && [ \"$3\" = 'remote' ]; then\n"
            "  printf '%s\\n' 'https://github.com/zilliztech/memsearch.git'\n"
            "elif [ \"$1\" = '-C' ] && [ \"$3\" = 'rev-parse' ]; then\n"
            f"  printf '%s\\n' '{revision}'\n"
            "elif [ \"$1\" = '-C' ] && "
            "{ [ \"$3\" = 'ls-tree' ] || [ \"$3\" = 'cat-file' ]; }; then\n"
            f"  exec '{git}' \"$@\"\n"
            "fi\n",
        )
        return plugin

    def install_fake_memsearch_tools(
        self,
        source_status: str = "",
        *,
        remote_url: str = "https://github.com/zilliztech/memsearch.git",
        install_hooks: bool = True,
        create_installer: bool = True,
        create_bash: bool = True,
        installer_exit: int = 0,
        uv_exit: int = 0,
    ) -> tuple[Path, Path, Path]:
        uv_log = self.root / "uv.log"
        git_log = self.root / "git.log"
        bash_log = self.root / "bash.log"
        memsearch_template = self.root / "memsearch-template"
        memsearch_template.write_text(
            "#!/bin/sh\nprintf '%s\\n' 'memsearch, version 0.4.15'\n",
            encoding="utf-8",
        )
        memsearch_template.chmod(0o755)
        self.env.update(
            {
                "UV_LOG": str(uv_log),
                "GIT_LOG": str(git_log),
                "BASH_LOG": str(bash_log),
                "FAKE_BIN": str(self.fake_bin),
                "MEMSEARCH_TEMPLATE": str(memsearch_template),
                "MEMSEARCH_REMOTE_URL": remote_url,
                "MEMSEARCH_SOURCE_STATUS": source_status,
                "MEMSEARCH_CREATE_INSTALLER": "1" if create_installer else "",
                "MEMSEARCH_INSTALLER_EXIT": str(installer_exit),
                "MEMSEARCH_UV_EXIT": str(uv_exit),
                "ONEVOKE_MEMSEARCH_SOURCE": str(self.root / "memsearch-source"),
                "MEMSEARCH_HOOKS_TEMPLATE": str(self.root / "hooks-template.json"),
            }
        )
        hooks_template_source = Path(self.env["MEMSEARCH_HOOKS_TEMPLATE"])
        hooks_template_source.write_text(
            json.dumps(
                {
                    "hooks": {
                        event: [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": (
                                            f"bash {self.root / 'memsearch-source' / 'plugins' / 'codex' / 'hooks' / name}"
                                        ),
                                    }
                                ]
                            }
                        ]
                        for event, name in {
                            "SessionStart": "session-start.sh",
                            "UserPromptSubmit": "user-prompt-submit.sh",
                            "Stop": "stop.sh",
                        }.items()
                    }
                }
            ),
            encoding="utf-8",
        )
        self.fake_command(
            "uv",
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" > \"$UV_LOG\"\n"
            "memsearch_uv_exit=${MEMSEARCH_UV_EXIT:-0}\n"
            "if [ \"$memsearch_uv_exit\" -eq 0 ]; then\n"
            "/bin/cp \"$MEMSEARCH_TEMPLATE\" \"$FAKE_BIN/memsearch\"\n"
            "fi\n"
            "exit \"$memsearch_uv_exit\"\n",
        )
        self.fake_command(
            "git",
            "#!/bin/sh\n"
            "if [ \"$1\" = '-C' ]; then\n"
            "  if [ \"$3\" = 'remote' ]; then\n"
            "    printf '%s\\n' \"$MEMSEARCH_REMOTE_URL\"\n"
            "  elif [ \"$3\" = 'status' ] && "
            "[ -n \"$MEMSEARCH_SOURCE_STATUS\" ]; then\n"
            "    printf '%s\\n' \"$MEMSEARCH_SOURCE_STATUS\"\n"
            "  fi\n"
            "  exit 0\n"
            "fi\n"
            "printf '%s\\n' \"$*\" > \"$GIT_LOG\"\n"
            "for destination in \"$@\"; do :; done\n"
            "/bin/mkdir -p \"$destination/plugins/codex/scripts\" "
            "\"$destination/plugins/codex/hooks\"\n"
            "if [ -n \"$MEMSEARCH_CREATE_INSTALLER\" ]; then\n"
            "printf '%s\\n' '#!/bin/sh' > "
            "\"$destination/plugins/codex/scripts/install.sh\"\n"
            "fi\n"
            "printf '%s\\n' '#!/bin/sh' > "
            "\"$destination/plugins/codex/hooks/common.sh\"\n"
            "printf '%s\\n' '#!/bin/sh' '# SessionStart hook:' > "
            "\"$destination/plugins/codex/hooks/session-start.sh\"\n"
            "printf '%s\\n' '#!/bin/sh' '# UserPromptSubmit hook:' > "
            "\"$destination/plugins/codex/hooks/user-prompt-submit.sh\"\n"
            "printf '%s\\n' '#!/bin/sh' '# Stop hook:' > "
            "\"$destination/plugins/codex/hooks/stop.sh\"\n"
            "/bin/chmod +x \"$destination/plugins/codex/hooks/\"*.sh\n",
        )
        bash_body = (
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" > \"$BASH_LOG\"\n"
            "/bin/mkdir -p \"$HOME/.codex\" \"$HOME/.agents/skills\"\n"
            "printf '%s\\n' '[features]' 'hooks = true' > \"$HOME/.codex/config.toml\"\n"
            "for skill in memory-recall memory-config memory-to-skill; do\n"
            "  /bin/mkdir -p \"$HOME/.agents/skills/$skill\"\n"
            "  printf '%s\\n' 'test skill' > \"$HOME/.agents/skills/$skill/SKILL.md\"\n"
            "done\n"
        )
        if install_hooks:
            bash_body += (
                "/bin/cp \"$MEMSEARCH_HOOKS_TEMPLATE\" \"$HOME/.codex/hooks.json\"\n"
            )
        bash_body += "exit \"$MEMSEARCH_INSTALLER_EXIT\"\n"
        if create_bash:
            self.fake_command("bash", bash_body)
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

    def test_locale_selects_chinese_or_english_and_honors_override(self) -> None:
        chinese = self.run_command("--help")
        self.assertIn("Onevoke 配置与诊断", chinese.stdout)
        self.assertIn("--lang {cn,en}", chinese.stdout)
        self.assertNotIn("usage:", chinese.stdout)

        chinese_error = self.run_command("nope")
        self.assertIn("参数 命令: 无效选择", chinese_error.stderr)
        self.assertNotIn("argument command", chinese_error.stderr)
        review_help = self.run_command("review", "--help")
        self.assertIn("参数", review_help.stdout)
        self.assertNotIn("arguments", review_help.stdout)
        option_error = self.run_command("config", "--json=foo")
        self.assertIn("不接受显式参数 'foo'", option_error.stderr)
        self.assertNotIn("ignored explicit argument", option_error.stderr)

        self.env.pop("ONEVOKE_LANG")
        self.env["LC_ALL"] = "zh_CN.UTF-8"
        fallback = self.run_command("--help")
        self.assertIn("Onevoke 配置与诊断", fallback.stdout)

        self.env["LC_ALL"] = "en_US.UTF-8"
        self.env["LC_MESSAGES"] = "zh_CN.UTF-8"
        self.assertIn("Onevoke configuration and diagnostics", self.run_command("--help").stdout)

        self.env.pop("LC_ALL")
        self.assertIn("Onevoke 配置与诊断", self.run_command("--help").stdout)

        self.env.pop("LC_MESSAGES")
        self.env["LANG"] = "zh_CN.UTF-8"
        self.assertIn("Onevoke 配置与诊断", self.run_command("--help").stdout)

        self.env["ONEVOKE_LANG"] = "en"
        self.env["LC_ALL"] = "zh_CN.UTF-8"
        english = self.run_command("--help")
        self.assertIn("Onevoke configuration and diagnostics", english.stdout)
        self.assertNotIn("配置与诊断", english.stdout)

        forced_chinese = self.run_command("--lang", "cn", "--help")
        self.assertIn("Onevoke 配置与诊断", forced_chinese.stdout)
        self.env["ONEVOKE_LANG"] = "zh"
        forced_english = self.run_command("--lang", "en", "--help")
        self.assertIn("Onevoke configuration and diagnostics", forced_english.stdout)
        invalid = self.run_command("--lang", "fr", "--help")
        self.assertEqual(2, invalid.returncode)
        self.assertIn("无效选择", invalid.stderr)
        missing = self.run_command("--lang")
        self.assertEqual(2, missing.returncode)
        self.assertIn("需要一个参数", missing.stderr)

        self.env["ONEVOKE_LANG"] = "en"
        status = self.run_command("config")
        self.assertEqual(0, status.returncode, status.stderr)
        self.assertIn("welcome: incomplete", status.stdout)
        self.assertIn("MemSearch: disabled", status.stdout)

        rejected = self.run_command("review")
        self.assertEqual(1, rejected.returncode)
        self.assertIn("usage: onevoke review", rejected.stderr)

    def test_welcome_interaction_uses_english(self) -> None:
        self.install_fake_environment(tmux=False)
        self.env["ONEVOKE_LANG"] = "en"

        returncode, output = self.run_on_tty(
            "1\n1\n1\n1\n1\n2\n1\n", "welcome"
        )

        self.assertEqual(0, returncode, output)
        self.assertIn("Which Agent should kanban use by default?", output)
        self.assertIn("Configuration summary:", output)
        self.assertIn("Configuration saved:", output)
        self.assertNotIn("配置摘要", output)

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

    def test_welcome_colors_question_titles_and_honors_no_color(self) -> None:
        self.install_fake_environment(tmux=True, memsearch=False)
        answers = "1\n1\n1\n1\n1\n1\n2\n1\n"
        prompts = (
            "kanban 默认用哪个 Agent 执行任务?",
            "PM 使用哪个 Reviewer?",
            "CSA 使用哪个 Reviewer?",
            "Hacker 使用哪个 Reviewer?",
            "QA 使用哪个 Reviewer?",
            "kanban start 使用哪种启动方式?",
            "确认现在由 Onevoke 执行 MemSearch 安装过程?",
            "保存以上配置?",
        )

        returncode, output = self.run_on_tty(answers, "welcome")

        self.assertEqual(0, returncode, output)
        for prompt in prompts:
            self.assertIn(f"\033[1;36m{prompt}\033[0m", output)
        self.assertNotIn("\033[1;36m请选择", output)

        self.env["NO_COLOR"] = "1"
        returncode, output = self.run_on_tty(answers, "welcome", "--reset")

        self.assertEqual(0, returncode, output)
        self.assertNotIn("\033[", output)
        for prompt in prompts:
            self.assertIn(prompt, output)

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
        self.assertIn("MemSearch CLI 未安装, 是否确认执行 CLI 安装命令?", output)
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual("grok", config["kanban_agent"])
        self.assertEqual({role: "grok" for role in ROLES}, config["reviewers"])
        self.assertFalse(config["memsearch"]["enabled"])

    def test_welcome_repairs_unreadable_memsearch_version_with_only_grok(self) -> None:
        for name in (
            "onevoke",
            "kanban",
            "codex-review.sh",
            "grok-review.sh",
            "merge-worktree-memory.py",
            "grok",
        ):
            self.fake_command(name)
        self.fake_command(
            "memsearch", "#!/bin/sh\nprintf '%s\\n' 'memsearch, version unknown'\n"
        )
        uv_log = self.root / "uv.log"
        template = self.root / "memsearch-template"
        template.write_text(
            "#!/bin/sh\nprintf '%s\\n' 'memsearch, version 0.4.15'\n",
            encoding="utf-8",
        )
        template.chmod(0o755)
        self.env.update({"UV_LOG": str(uv_log), "MEMSEARCH_TEMPLATE": str(template)})
        self.fake_command(
            "uv",
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" > \"$UV_LOG\"\n"
            "/bin/cp \"$MEMSEARCH_TEMPLATE\" \"$PATH/memsearch\"\n",
        )

        # Grok 执行和四个 Reviewer; 拒绝 tmux; 修正 CLI; 保存.
        returncode, output = self.run_on_tty(
            "1\n1\n1\n1\n1\n2\n1\n1\n", "welcome"
        )

        self.assertEqual(0, returncode, output)
        self.assertIn(
            "MemSearch CLI 无法正常报告版本, 是否确认安装 PyPI 最新版?",
            output,
        )
        self.assertEqual(
            "tool install -U memsearch[onnx]",
            uv_log.read_text(encoding="utf-8").strip(),
        )
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertFalse(config["memsearch"]["enabled"])

    def test_doctor_rejects_stale_memsearch_artifacts(self) -> None:
        self.install_fake_environment(tmux=True, memsearch=False)
        self.fake_command(
            "memsearch", "#!/bin/sh\nprintf '%s\\n' 'memsearch, version 0.4.15'\n"
        )
        codex_hooks = self.home / ".codex" / "hooks.json"
        codex_hooks.parent.mkdir(parents=True)
        codex_hooks.write_text(
            json.dumps(
                {
                    "note": (
                        "memsearch session-start.sh user-prompt-submit.sh stop.sh"
                    )
                }
            ),
            encoding="utf-8",
        )
        (self.home / ".claude" / "plugins" / "memsearch-empty").mkdir(parents=True)

        result = self.run_command("doctor")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Codex 插件: 未接入", result.stderr)
        self.assertIn("Claude 插件: 未接入", result.stderr)

    def test_claude_plugin_accepts_any_version_and_rejects_symlinks(self) -> None:
        self.install_fake_environment(tmux=True, memsearch=False)
        plugin = self.install_fake_claude_memsearch_plugin("9.8.7")
        onevoke = load_onevoke_module()

        with mock.patch.object(Path, "home", return_value=self.home):
            runtime_marker = plugin / ".in_use" / "session"
            runtime_marker.parent.mkdir()
            runtime_marker.write_text("active\n", encoding="utf-8")
            self.assertTrue(onevoke.claude_memsearch_ready())

            common = plugin / "hooks" / "common.sh"
            common.write_text("#!/bin/sh\nprintf 'changed\\n'\n", encoding="utf-8")
            self.assertFalse(onevoke.claude_memsearch_ready())
            shutil.copy2(
                self.home
                / ".claude"
                / "plugins"
                / "marketplaces"
                / "memsearch-plugins"
                / "plugins"
                / "claude-code"
                / "hooks"
                / "common.sh",
                common,
            )

            extra_skill = plugin / "skills" / "extra" / "SKILL.md"
            extra_skill.parent.mkdir(parents=True)
            extra_skill.write_text("extra instructions\n", encoding="utf-8")
            self.assertFalse(onevoke.claude_memsearch_ready())
            shutil.rmtree(plugin / "skills")

            common.unlink()
            common.symlink_to(plugin / "README.md")
            self.assertFalse(onevoke.claude_memsearch_ready())

    def test_claude_plugin_rejects_external_install_path(self) -> None:
        self.install_fake_environment(tmux=True, memsearch=False)
        plugin = self.install_fake_claude_memsearch_plugin("3.2.1")
        external = self.root / "external-memsearch"
        shutil.copytree(plugin, external)
        installed = self.home / ".claude" / "plugins" / "installed_plugins.json"
        data = json.loads(installed.read_text(encoding="utf-8"))
        data["plugins"]["memsearch@memsearch-plugins"][0]["installPath"] = str(
            external
        )
        installed.write_text(json.dumps(data), encoding="utf-8")
        onevoke = load_onevoke_module()

        with mock.patch.object(Path, "home", return_value=self.home):
            self.assertFalse(onevoke.claude_memsearch_ready())

    def test_claude_plugin_rejects_symlinked_marketplace_or_cache_parent(
        self,
    ) -> None:
        self.install_fake_environment(tmux=True, memsearch=False)
        self.install_fake_claude_memsearch_plugin("3.2.1")
        onevoke = load_onevoke_module()
        plugins = self.home / ".claude" / "plugins"

        with mock.patch.object(Path, "home", return_value=self.home):
            self.assertTrue(onevoke.claude_memsearch_ready())
            for name in ("marketplaces", "cache"):
                with self.subTest(parent=name):
                    parent = plugins / name
                    external = self.root / f"external-{name}"
                    os.replace(parent, external)
                    parent.symlink_to(external, target_is_directory=True)
                    try:
                        self.assertFalse(onevoke.claude_memsearch_ready())
                    finally:
                        parent.unlink()
                        os.replace(external, parent)
            self.assertTrue(onevoke.claude_memsearch_ready())

    def test_claude_plugin_rejects_ignored_file_in_marketplace_and_cache(
        self,
    ) -> None:
        self.install_fake_environment(tmux=True, memsearch=False)
        plugin = self.install_fake_claude_memsearch_plugin("3.2.1")
        marketplace = (
            self.home / ".claude" / "plugins" / "marketplaces" / "memsearch-plugins"
        )
        exclude = marketplace / ".git" / "info" / "exclude"
        exclude.write_text(
            "plugins/claude-code/skills/local/\n", encoding="utf-8"
        )
        source_skill = (
            marketplace
            / "plugins"
            / "claude-code"
            / "skills"
            / "local"
            / "SKILL.md"
        )
        source_skill.parent.mkdir(parents=True)
        source_skill.write_text("injected skill\n", encoding="utf-8")
        cache_skill = plugin / "skills" / "local" / "SKILL.md"
        cache_skill.parent.mkdir(parents=True)
        cache_skill.write_text("injected skill\n", encoding="utf-8")
        onevoke = load_onevoke_module()

        with mock.patch.object(Path, "home", return_value=self.home):
            self.assertFalse(onevoke.claude_memsearch_ready())

    def test_claude_plugin_ignores_local_export_attributes(self) -> None:
        self.install_fake_environment(tmux=True, memsearch=False)
        plugin = self.install_fake_claude_memsearch_plugin("3.2.1")
        marketplace = (
            self.home / ".claude" / "plugins" / "marketplaces" / "memsearch-plugins"
        )
        attributes = marketplace / ".git" / "info" / "attributes"
        attributes.write_text(
            "plugins/claude-code/hooks/helper.sh export-ignore\n",
            encoding="utf-8",
        )
        (plugin / "hooks" / "helper.sh").unlink()
        onevoke = load_onevoke_module()

        with mock.patch.object(Path, "home", return_value=self.home):
            self.assertFalse(onevoke.claude_memsearch_ready())

    def test_memsearch_environment_commit_rolls_back_on_failure_or_interrupt(
        self,
    ) -> None:
        onevoke = load_onevoke_module()
        original_replace = os.replace
        targets = (
            Path(".codex/config.toml"),
            Path(".agents/skills/memory-recall"),
        )

        for raised in (OSError("publish failed"), KeyboardInterrupt()):
            with self.subTest(exception=type(raised).__name__):
                staged = self.root / f"staged-{type(raised).__name__}"
                backup = self.root / f"backup-{type(raised).__name__}"
                config = self.home / targets[0]
                skill = self.home / targets[1]
                shutil.rmtree(staged, ignore_errors=True)
                shutil.rmtree(backup, ignore_errors=True)
                shutil.rmtree(skill, ignore_errors=True)
                config.parent.mkdir(parents=True, exist_ok=True)
                config.write_text("old config\n", encoding="utf-8")
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text("old skill\n", encoding="utf-8")
                (staged / targets[0]).parent.mkdir(parents=True)
                (staged / targets[0]).write_text("new config\n", encoding="utf-8")
                (staged / targets[1]).mkdir(parents=True)
                (staged / targets[1] / "SKILL.md").write_text(
                    "new skill\n", encoding="utf-8"
                )
                promoted = 0

                def replace(source, destination):
                    nonlocal promoted
                    if Path(source).is_relative_to(staged):
                        promoted += 1
                        if promoted == 2:
                            raise raised
                    return original_replace(source, destination)

                with mock.patch.object(Path, "home", return_value=self.home):
                    with mock.patch.object(onevoke.os, "replace", side_effect=replace):
                        with self.assertRaises(type(raised)):
                            onevoke.commit_staged_memsearch_home(
                                staged, backup, targets
                            )

                self.assertEqual("old config\n", config.read_text(encoding="utf-8"))
                self.assertEqual(
                    "old skill\n",
                    (skill / "SKILL.md").read_text(encoding="utf-8"),
                )

    def test_memsearch_cleanup_failure_does_not_reverse_committed_install(self) -> None:
        self.install_fake_environment(tmux=True, memsearch=False)
        self.install_fake_memsearch_tools()
        onevoke = load_onevoke_module()
        original_remove = onevoke.remove_path
        failed_roots: list[Path] = []

        def fail_temporary_cleanup(path: Path) -> None:
            if path.name.startswith(("memsearch.", ".onevoke-memsearch.")):
                failed_roots.append(path)
                raise OSError("cleanup failed")
            original_remove(path)

        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch.object(Path, "home", return_value=self.home):
                with mock.patch.object(
                    onevoke, "remove_path", side_effect=fail_temporary_cleanup
                ):
                    self.assertTrue(onevoke.install_memsearch_for("codex"))

        self.assertEqual(2, len(failed_roots))
        self.assertTrue((self.root / "memsearch-source").is_dir())
        self.assertIn(
            "hooks = true",
            (self.home / ".codex" / "config.toml").read_text(encoding="utf-8"),
        )
        for root in failed_roots:
            shutil.rmtree(root, ignore_errors=True)

    def test_memsearch_interrupt_at_commit_point_rolls_back_install(self) -> None:
        self.install_fake_environment(tmux=True, memsearch=False)
        self.install_fake_memsearch_tools()
        source = self.root / "memsearch-source"
        installer = source / "plugins" / "codex" / "scripts" / "install.sh"
        installer.parent.mkdir(parents=True)
        installer.write_text("#!/bin/sh\n", encoding="utf-8")
        marker = source / "old-cache"
        marker.write_text("keep\n", encoding="utf-8")
        hooks = self.home / ".codex" / "hooks.json"
        hooks.parent.mkdir(parents=True)
        hooks.write_text('{"original": true}\n', encoding="utf-8")
        config_file = self.home / ".codex" / "config.toml"
        config_file.write_text("original = true\n", encoding="utf-8")
        skill = self.home / ".agents" / "skills" / "memory-recall" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("original skill\n", encoding="utf-8")
        onevoke = load_onevoke_module()
        original_commit = onevoke.commit_staged_memsearch_home

        def interrupt_after_publish(*args, **kwargs):
            original_commit(*args, **kwargs)
            raise KeyboardInterrupt()

        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch.object(Path, "home", return_value=self.home):
                with mock.patch.object(
                    onevoke,
                    "commit_staged_memsearch_home",
                    side_effect=interrupt_after_publish,
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        onevoke.install_memsearch_for("codex")

        self.assertTrue(marker.is_file())
        self.assertEqual('{"original": true}\n', hooks.read_text(encoding="utf-8"))
        self.assertEqual("original = true\n", config_file.read_text(encoding="utf-8"))
        self.assertEqual("original skill\n", skill.read_text(encoding="utf-8"))

    def test_doctor_rejects_invalid_claude_plugin_version(self) -> None:
        self.install_fake_environment(tmux=True, memsearch=False)
        self.fake_command(
            "memsearch", "#!/bin/sh\nprintf '%s\\n' 'memsearch, version 0.4.15'\n"
        )
        self.install_fake_claude_memsearch_plugin("latest")

        result = self.run_command("doctor")

        self.assertIn("Claude 插件: 未接入", result.stderr)

    def test_claude_plugin_requires_official_marketplace_source(self) -> None:
        self.install_fake_environment(tmux=True, memsearch=False)
        self.install_fake_claude_memsearch_plugin("3.2.1")
        onevoke = load_onevoke_module()
        marketplaces = self.home / ".claude" / "plugins" / "known_marketplaces.json"

        with mock.patch.object(Path, "home", return_value=self.home):
            self.assertTrue(onevoke.claude_memsearch_ready())

            marketplaces.unlink()
            self.assertFalse(onevoke.claude_memsearch_ready())

            marketplaces.write_text(
                json.dumps(
                    {
                        "memsearch-plugins": {
                            "source": {
                                "source": "github",
                                "repo": "untrusted/memsearch",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            self.assertFalse(onevoke.claude_memsearch_ready())

    def test_doctor_rejects_unverified_same_name_codex_hooks(self) -> None:
        self.install_fake_environment(tmux=True, memsearch=False)
        self.fake_command(
            "memsearch", "#!/bin/sh\nprintf '%s\\n' 'memsearch, version 0.4.15'\n"
        )
        hooks_dir = self.root / "unrelated-hooks"
        hooks_dir.mkdir()
        hooks = {}
        for event, name in {
            "SessionStart": "session-start.sh",
            "UserPromptSubmit": "user-prompt-submit.sh",
            "Stop": "stop.sh",
        }.items():
            script = hooks_dir / name
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            script.chmod(0o755)
            hooks[event] = [
                {"hooks": [{"type": "command", "command": f"bash {script}"}]}
            ]
        hooks_file = self.home / ".codex" / "hooks.json"
        hooks_file.parent.mkdir(parents=True)
        hooks_file.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")

        result = self.run_command("doctor")

        self.assertIn("Codex 插件: 未接入", result.stderr)

    def test_doctor_rejects_codex_hook_command_with_extra_tokens(self) -> None:
        self.install_fake_environment(tmux=True)
        hooks_file = self.home / ".codex" / "hooks.json"
        original = json.loads(hooks_file.read_text(encoding="utf-8"))

        for position in ("prefix", "suffix"):
            with self.subTest(position=position):
                hooks = json.loads(json.dumps(original))
                command = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
                if position == "prefix":
                    command = f"false {command.split(' ', 1)[1]}"
                else:
                    command = f"{command} extra"
                hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"] = command
                hooks_file.write_text(json.dumps(hooks), encoding="utf-8")

                result = self.run_command("doctor")

                self.assertIn("Codex 插件: 未接入", result.stderr)

    def test_doctor_fails_without_any_agent_or_reviewer(self) -> None:
        for name in (
            "onevoke",
            "kanban",
            "codex-review.sh",
            "grok-review.sh",
            "merge-worktree-memory.py",
        ):
            self.fake_command(name)

        result = self.run_command("doctor")

        self.assertEqual(1, result.returncode)
        self.assertIn("没有发现可执行 Agent", result.stderr)
        self.assertIn("没有发现 Reviewer", result.stderr)

    def test_doctor_rejects_enabled_memsearch_when_cli_version_is_unreadable(self) -> None:
        self.install_fake_environment(tmux=True)
        self.fake_command(
            "memsearch", "#!/bin/sh\nprintf '%s\\n' 'memsearch, version unknown'\n"
        )
        config = {
            "schema_version": 1,
            "welcome_complete": True,
            "kanban_agent": "codex",
            "launcher": "tmux",
            "reviewers": {role: "codex" for role in ROLES},
            "memsearch": {"enabled": True},
        }
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(config), encoding="utf-8")

        result = self.run_command("doctor")

        self.assertEqual(1, result.returncode)
        self.assertIn("CLI 无法正常报告版本", result.stderr)
        self.assertIn("配置启用了 MemSearch", result.stderr)

    def test_doctor_rejects_malformed_or_unrelated_version_output(self) -> None:
        self.install_fake_environment(tmux=True)

        for output in (
            "memsearch, version 9.8.7broken",
            "memsearch, version 9.8.7.4",
            "memsearch, version 9.8.7-...",
            "memsearch, version 9.8.7+.",
            "memsearch, version ١.٢.٣",
            "Python 3.12.0",
        ):
            with self.subTest(output=output):
                self.fake_command(
                    "memsearch", f"#!/bin/sh\nprintf '%s\\n' '{output}'\n"
                )

                result = self.run_command("doctor")

                self.assertEqual(1, result.returncode)
                self.assertIn("CLI 无法正常报告版本", result.stderr)

    def test_claude_plugin_rejects_version_suffixes(self) -> None:
        self.install_fake_environment(tmux=True, memsearch=False)
        plugin = self.install_fake_claude_memsearch_plugin("3.2.1")
        onevoke = load_onevoke_module()
        manifest = plugin / ".claude-plugin" / "plugin.json"

        with mock.patch.object(Path, "home", return_value=self.home):
            self.assertTrue(onevoke.claude_memsearch_ready())
            for version in ("3.2.1-alpha", "3.2.1-...", "3.2.1+."):
                with self.subTest(version=version):
                    manifest.write_text(
                        json.dumps({"name": "memsearch", "version": version}),
                        encoding="utf-8",
                    )
                    self.assertFalse(onevoke.claude_memsearch_ready())

    def test_invalid_config_is_reported_without_fallback(self) -> None:
        self.config.parent.mkdir(parents=True)
        self.config.write_text("not json\n", encoding="utf-8")

        result = self.run_command("config")

        self.assertEqual(1, result.returncode)
        self.assertIn("读取配置失败", result.stderr)

    def test_config_defaults_to_human_output_and_json_is_explicit(self) -> None:
        human = self.run_command("config")
        machine = self.run_command("config", "--json")

        self.assertIn("welcome: 未完成", human.stdout)
        self.assertIn("kanban agent: codex", human.stdout)
        self.assertFalse(human.stdout.lstrip().startswith("{"))
        self.assertFalse(json.loads(machine.stdout)["welcome_complete"])

    def test_welcome_installs_latest_memsearch_and_codex_plugin(self) -> None:
        self.install_fake_environment(tmux=True, memsearch=False)
        uv_log, git_log, bash_log = self.install_fake_memsearch_tools()

        # Codex 执行和四个 Reviewer; tmux launcher; 安装 MemSearch; 保存.
        returncode, output = self.run_on_tty(
            "1\n1\n1\n1\n1\n1\n1\n1\n", "welcome"
        )

        self.assertEqual(0, returncode, output)
        self.assertEqual(
            "tool install -U memsearch[onnx]",
            uv_log.read_text(encoding="utf-8").strip(),
        )
        clone_command = git_log.read_text(encoding="utf-8")
        self.assertIn("clone --depth 1", clone_command)
        self.assertNotIn("--branch", clone_command)
        self.assertTrue((self.root / "memsearch-source").is_dir())
        self.assertIn(
            "plugins/codex/scripts/install.sh",
            bash_log.read_text(encoding="utf-8"),
        )
        self.assertIn(
            "hooks = true",
            (self.home / ".codex" / "config.toml").read_text(encoding="utf-8"),
        )
        for skill_name in ("memory-recall", "memory-config", "memory-to-skill"):
            self.assertTrue(
                (self.home / ".agents" / "skills" / skill_name / "SKILL.md").is_file()
            )
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertTrue(config["memsearch"]["enabled"])

    def test_welcome_enables_memsearch_after_install_commands_without_readiness_gate(
        self,
    ) -> None:
        """安装只要求命令执行完, 不要求 hooks 已就绪的二次校验."""
        self.install_fake_environment(tmux=True, memsearch=False)
        uv_log, _, bash_log = self.install_fake_memsearch_tools(install_hooks=False)

        returncode, output = self.run_on_tty(
            "1\n1\n1\n1\n1\n1\n1\n1\n", "welcome"
        )

        self.assertEqual(0, returncode, output)
        self.assertIn("确认现在由 Onevoke 执行 MemSearch 安装过程?", output)
        self.assertIn("已执行 MemSearch Codex 插件安装命令", output)
        self.assertNotIn("安装后校验未通过", output)
        self.assertEqual(
            "tool install -U memsearch[onnx]",
            uv_log.read_text(encoding="utf-8").strip(),
        )
        self.assertTrue(bash_log.exists())
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertTrue(config["memsearch"]["enabled"])

    def test_welcome_upgrades_existing_cli_when_plugin_needs_install(self) -> None:
        self.install_fake_environment(tmux=True, memsearch=False)
        self.fake_command(
            "memsearch", "#!/bin/sh\nprintf '%s\\n' 'memsearch, version 9.9.9'\n"
        )
        uv_log, _, _ = self.install_fake_memsearch_tools()

        returncode, output = self.run_on_tty(
            "1\n1\n1\n1\n1\n1\n1\n1\n", "welcome"
        )

        self.assertEqual(0, returncode, output)
        self.assertIn("现有 MemSearch CLI 版本为 9.9.9", output)
        self.assertEqual(
            "tool install -U memsearch[onnx]",
            uv_log.read_text(encoding="utf-8").strip(),
        )
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertTrue(config["memsearch"]["enabled"])

    def test_welcome_skips_plugin_installer_for_nonofficial_source(
        self,
    ) -> None:
        self.install_fake_environment(tmux=True, memsearch=False)
        uv_log, _, bash_log = self.install_fake_memsearch_tools(
            remote_url="https://example.com/untrusted/memsearch.git",
        )

        returncode, output = self.run_on_tty(
            "1\n1\n1\n1\n1\n1\n1\n1\n", "welcome"
        )

        self.assertEqual(0, returncode, output)
        self.assertIn("MemSearch 源码来源不是官方仓库", output)
        self.assertIn("已跳过 Codex 插件安装器", output)
        self.assertIn("MemSearch 安装未完成", output)
        self.assertFalse(bash_log.exists())
        self.assertIn("memsearch[onnx]", uv_log.read_text(encoding="utf-8"))
        # CLI 可能已装好, 但 Codex 插件步骤未完成时不得标 enabled.
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertFalse(config["memsearch"]["enabled"])

    def test_welcome_replaces_clean_cached_source_with_latest_clone(self) -> None:
        self.install_fake_environment(tmux=True, memsearch=False)
        uv_log, git_log, bash_log = self.install_fake_memsearch_tools()
        source = self.root / "memsearch-source"
        source.mkdir()
        stale_marker = source / "stale"
        stale_marker.write_text("old checkout\n", encoding="utf-8")

        returncode, output = self.run_on_tty(
            "1\n1\n1\n1\n1\n1\n1\n1\n", "welcome"
        )

        self.assertEqual(0, returncode, output)
        self.assertIn("clone --depth 1", git_log.read_text(encoding="utf-8"))
        self.assertFalse(stale_marker.exists())
        self.assertTrue(bash_log.exists())
        self.assertIn("memsearch[onnx]", uv_log.read_text(encoding="utf-8"))
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertTrue(config["memsearch"]["enabled"])

    def test_welcome_skips_codex_plugin_when_cli_install_fails(self) -> None:
        self.install_fake_environment(tmux=True, memsearch=False)
        _, git_log, bash_log = self.install_fake_memsearch_tools(uv_exit=1)

        returncode, output = self.run_on_tty(
            "1\n1\n1\n1\n1\n1\n1\n1\n", "welcome"
        )

        self.assertEqual(0, returncode, output)
        self.assertIn("CLI 安装未完成, 已跳过 Codex 插件安装", output)
        self.assertFalse(git_log.exists())
        self.assertFalse(bash_log.exists())
        self.assertFalse((self.root / "memsearch-source").exists())
        self.assertFalse((self.home / ".codex" / "hooks.json").exists())
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertFalse(config["memsearch"]["enabled"])

    def test_welcome_skips_plugin_installer_on_dirty_cached_source(self) -> None:
        self.install_fake_environment(tmux=True, memsearch=False)
        uv_log, _, bash_log = self.install_fake_memsearch_tools(
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
        self.assertIn("已跳过 Codex 插件安装器", output)
        self.assertIn("MemSearch 安装未完成", output)
        self.assertFalse(bash_log.exists())
        self.assertIn("memsearch[onnx]", uv_log.read_text(encoding="utf-8"))
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

    def test_review_ignores_unfinished_welcome_selections(self) -> None:
        log = self.root / "review.log"
        self.fake_command(
            "codex-review.sh",
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$REVIEW_LOG\"\n",
        )
        self.fake_command("grok-review.sh", "#!/bin/sh\nexit 99\n")
        self.env["REVIEW_LOG"] = str(log)
        config = {
            "schema_version": 1,
            "welcome_complete": False,
            "kanban_agent": "grok",
            "launcher": "foreground",
            "reviewers": {role: "grok" for role in ROLES},
            "memsearch": {"enabled": True},
        }
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(config), encoding="utf-8")

        result = self.run_command(
            "review", "/worktree", "base", "commit", "QA", "目标"
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(log.exists())

    def test_doctor_rejects_agent_when_version_check_fails(self) -> None:
        self.install_fake_environment(tmux=True)
        self.fake_command("codex", "#!/bin/sh\nexit 1\n")

        result = self.run_command("doctor")

        self.assertEqual(1, result.returncode)
        self.assertIn("Codex:", result.stderr)
        self.assertIn("--version 失败", result.stderr)
        self.assertNotRegex(result.stderr, r"\[OK\].*Codex:")

    def test_welcome_excludes_agent_with_failed_version(self) -> None:
        self.install_fake_environment(tmux=False)
        self.fake_command("codex", "#!/bin/sh\nexit 1\n")
        self.fake_command("claude", "#!/bin/sh\nexit 1\n")
        # Only grok reports a version; four reviewers must also be usable.

        # 仅 Grok 可用: 执行 1; 四个 Reviewer 各 1; 拒绝装 tmux 2; 保存 1.
        returncode, output = self.run_on_tty("1\n1\n1\n1\n1\n2\n1\n", "welcome")

        self.assertEqual(0, returncode, output)
        self.assertIn("--version 失败", output)
        self.assertIn("已从可选列表排除", output)
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual("grok", config["kanban_agent"])
        self.assertFalse(config["memsearch"]["enabled"])

    def test_rules_integration_accepts_production_entry_with_internal_bu_shi_yong(
        self,
    ) -> None:
        """生产入口正文含「不使用其他长期分支模型」, 全文合并不得被自身误拒."""
        onevoke = load_onevoke_module()
        production = PROJECT_ROOT / "rules" / "ONEVOKE-AGENTS.md"
        production_text = production.read_text(encoding="utf-8")
        self.assertIn("不使用其他长期分支模型", production_text)
        entry = self.home / ".agents" / "ONEVOKE-AGENTS.md"
        entry.parent.mkdir(parents=True)
        entry.write_text(production_text, encoding="utf-8")
        for agent, target in (
            ("codex", self.home / ".codex" / "AGENTS.md"),
            ("grok", self.home / ".grok" / "AGENTS.md"),
        ):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                production_text.strip() + "\n\n## 我自己的规则\n",
                encoding="utf-8",
            )
            with mock.patch.object(Path, "home", return_value=self.home):
                with self.subTest(agent=agent, case="accept-production-body"):
                    ok, detail = onevoke.rules_integration(agent)
                    self.assertTrue(ok, detail)
                with self.subTest(agent=agent, case="reject-outer-negation"):
                    target.write_text(
                        "以下规则不使用:\n\n" + production_text.strip() + "\n",
                        encoding="utf-8",
                    )
                    ok, _ = onevoke.rules_integration(agent)
                    self.assertFalse(ok)

    def test_rules_integration_rejects_comment_negation_and_placeholder(self) -> None:
        onevoke = load_onevoke_module()
        entry = self.home / ".agents" / "ONEVOKE-AGENTS.md"
        entry.parent.mkdir(parents=True)
        current_entry = (
            "# Onevoke 全局工作流规则\n\n"
            "## 默认取值\n\n"
            "| 分册 | 说明 |\n"
            "| `BASE-RULES.md` | 通用条款 |\n"
            "| `GIT-RULES.md` | Git 工作流 |\n\n"
            "### 看板任务完成\n\n"
            "- 先报告并等确认, 确认后才合回 `develop`.\n"
        )
        entry.write_text(current_entry, encoding="utf-8")

        cases = {
            "codex": (
                self.home / ".codex" / "AGENTS.md",
                (
                    "# 其它标题\n"
                    "<!--\n# Onevoke 全局工作流规则\n| `BASE-RULES.md` |\n-->\n"
                    "不要使用 BASE-RULES.md\n"
                    "TODO 占位 BASE-RULES.md\n"
                    "```md\n# Onevoke 全局工作流规则\n`BASE-RULES.md`\n```\n"
                    "# Onevoke 全局工作流规则\n"
                    "## 默认取值\n"
                    "合回初始分支\n"
                ),
                current_entry + "\n## 我自己的规则\n",
            ),
            "claude": (
                self.home / ".claude" / "CLAUDE.md",
                (
                    "# 说明\n"
                    "未导入 ~/.agents/ONEVOKE-AGENTS.md\n"
                    "<!--\n@~/.agents/ONEVOKE-AGENTS.md\n-->\n"
                    "# @~/.agents/ONEVOKE-AGENTS.md\n"
                    "```\n@~/.agents/ONEVOKE-AGENTS.md\n```\n"
                ),
                "@~/.agents/ONEVOKE-AGENTS.md\n\n## 我自己的规则\n",
            ),
            "grok": (
                self.home / ".grok" / "AGENTS.md",
                (
                    "# Onevoke 全局工作流规则\n"
                    "BASE-RULES.md 已禁用\n"
                    "残留 BASE-RULES.md 但没有入口标题\n"
                ),
                current_entry,
            ),
        }

        with mock.patch.object(Path, "home", return_value=self.home):
            for agent, (target, bad, good) in cases.items():
                with self.subTest(agent=agent, case="reject"):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(bad, encoding="utf-8")
                    ok, _ = onevoke.rules_integration(agent)
                    self.assertFalse(ok)
                with self.subTest(agent=agent, case="accept"):
                    target.write_text(good, encoding="utf-8")
                    ok, detail = onevoke.rules_integration(agent)
                    self.assertTrue(ok, detail)
                if agent in ("codex", "grok"):
                    with self.subTest(agent=agent, case="reject-negated-full"):
                        target.write_text(
                            "以下 Onevoke 规则已禁用, 不要遵守:\n\n" + current_entry,
                            encoding="utf-8",
                        )
                        ok, _ = onevoke.rules_integration(agent)
                        self.assertFalse(ok)
                    with self.subTest(agent=agent, case="reject-far-negation"):
                        padding = "说明行\n" * 40
                        target.write_text(
                            "已废弃, 不要遵守下列入口:\n"
                            + padding
                            + current_entry,
                            encoding="utf-8",
                        )
                        ok, _ = onevoke.rules_integration(agent)
                        self.assertFalse(ok)
                if agent == "claude":
                    with self.subTest(agent=agent, case="reject-adjacent-negation"):
                        target.write_text(
                            "以下导入已废弃, 不要遵守:\n@~/.agents/ONEVOKE-AGENTS.md\n",
                            encoding="utf-8",
                        )
                        ok, _ = onevoke.rules_integration(agent)
                        self.assertFalse(ok)
                    with self.subTest(agent=agent, case="reject-unclosed-comment"):
                        target.write_text(
                            "<!-- unclosed\n@~/.agents/ONEVOKE-AGENTS.md\n",
                            encoding="utf-8",
                        )
                        ok, _ = onevoke.rules_integration(agent)
                        self.assertFalse(ok)
                if agent in ("codex", "grok"):
                    with self.subTest(agent=agent, case="reject-post-negation"):
                        target.write_text(
                            current_entry + "\n\n以上规则已废弃, 不要遵守.\n",
                            encoding="utf-8",
                        )
                        ok, _ = onevoke.rules_integration(agent)
                        self.assertFalse(ok)
                    with self.subTest(agent=agent, case="reject-do-not-use"):
                        target.write_text(
                            "以下规则不要使用:\n\n" + current_entry,
                            encoding="utf-8",
                        )
                        ok, _ = onevoke.rules_integration(agent)
                        self.assertFalse(ok)
                    for phrase in (
                        "不使用",
                        "不遵守",
                        "请勿使用",
                        "disabled",
                        "deprecated",
                        "ignore",
                    ):
                        with self.subTest(agent=agent, case=f"reject-{phrase}"):
                            if phrase.isascii():
                                prefix = f"These rules are {phrase}:\n\n"
                            else:
                                prefix = f"以下规则{phrase}:\n\n"
                            target.write_text(prefix + current_entry, encoding="utf-8")
                            ok, _ = onevoke.rules_integration(agent)
                            self.assertFalse(ok)
                if agent == "claude":
                    for phrase in ("不使用", "请勿使用", "disabled"):
                        with self.subTest(agent=agent, case=f"reject-claude-{phrase}"):
                            if phrase.isascii():
                                body = (
                                    f"These imports are {phrase}:\n"
                                    "@~/.agents/ONEVOKE-AGENTS.md\n"
                                )
                            else:
                                body = (
                                    f"以下导入{phrase}:\n"
                                    "@~/.agents/ONEVOKE-AGENTS.md\n"
                                )
                            target.write_text(body, encoding="utf-8")
                            ok, _ = onevoke.rules_integration(agent)
                            self.assertFalse(ok)

    def test_doctor_validates_configured_agent_reviewers_wrapper_and_launcher(self) -> None:
        self.install_fake_environment(tmux=True)
        config = {
            "schema_version": 1,
            "welcome_complete": True,
            "kanban_agent": "claude",
            "launcher": "tmux",
            "reviewers": {
                "PM": "codex",
                "CSA": "grok",
                "Hacker": "codex",
                "QA": "grok",
            },
            "memsearch": {"enabled": False},
        }
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(config), encoding="utf-8")
        # Remove configured execution agent and one reviewer wrapper.
        (self.fake_bin / "claude").unlink()
        (self.fake_bin / "grok-review.sh").unlink()

        result = self.run_command("doctor")

        self.assertEqual(1, result.returncode)
        self.assertIn("配置的执行 Agent 不可用: claude", result.stderr)
        self.assertIn("配置的 QA wrapper 不在 PATH: grok-review.sh", result.stderr)

    def test_doctor_rejects_tmux_launcher_when_tmux_missing(self) -> None:
        self.install_fake_environment(tmux=False)
        config = {
            "schema_version": 1,
            "welcome_complete": True,
            "kanban_agent": "codex",
            "launcher": "tmux",
            "reviewers": {role: "codex" for role in ROLES},
            "memsearch": {"enabled": False},
        }
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(config), encoding="utf-8")

        result = self.run_command("doctor")

        self.assertEqual(1, result.returncode)
        self.assertIn("配置的 launcher 是 tmux", result.stderr)
        self.assertIn("welcome --reset", result.stderr)

    def test_install_memsearch_for_claude_requires_plugin_before_success(
        self,
    ) -> None:
        """Claude 路径: 仅 CLI 成功不算完成; 插件已接入才返回 True."""
        self.install_fake_environment(tmux=True, memsearch=False)
        onevoke = load_onevoke_module()
        self.fake_command(
            "memsearch", "#!/bin/sh\nprintf '%s\\n' 'memsearch, version 9.9.9'\n"
        )
        uv_log, _, _ = self.install_fake_memsearch_tools()
        env_keys = (
            "PATH",
            "HOME",
            "UV_LOG",
            "FAKE_BIN",
            "MEMSEARCH_TEMPLATE",
            "ONEVOKE_MEMSEARCH_SOURCE",
        )
        previous = {key: os.environ.get(key) for key in env_keys}
        try:
            os.environ["PATH"] = str(self.fake_bin)
            os.environ["HOME"] = str(self.home)
            for key in (
                "UV_LOG",
                "FAKE_BIN",
                "MEMSEARCH_TEMPLATE",
                "ONEVOKE_MEMSEARCH_SOURCE",
            ):
                if key in self.env:
                    os.environ[key] = self.env[key]
            with mock.patch.object(Path, "home", return_value=self.home):
                _, version, ready = onevoke.memsearch_cli_state()
                self.assertTrue(ready, version)
                self.assertFalse(onevoke.install_memsearch_for("claude"))
                with mock.patch.object(
                    onevoke, "claude_memsearch_ready", return_value=True
                ):
                    self.assertTrue(onevoke.install_memsearch_for("claude"))
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertEqual(
            "tool install -U memsearch[onnx]",
            uv_log.read_text(encoding="utf-8").strip(),
        )

    def test_welcome_claude_without_plugin_does_not_enable_memsearch(self) -> None:
        """Claude 执行 Agent 且插件未接入时, 仅装 CLI 不得标 enabled."""
        self.install_fake_environment(tmux=True, memsearch=False)
        uv_log, _, _ = self.install_fake_memsearch_tools()

        # 2=claude 执行; 四个 Reviewer 全选 codex; tmux launcher; 确认安装; 保存.
        returncode, output = self.run_on_tty(
            "2\n1\n1\n1\n1\n1\n1\n1\n", "welcome"
        )

        self.assertEqual(0, returncode, output)
        self.assertIn("plugin marketplace add zilliztech/memsearch", output)
        self.assertIn("plugin marketplace update memsearch-plugins", output)
        self.assertIn("plugin install memsearch@memsearch-plugins", output)
        self.assertIn("MemSearch 安装未完成", output)
        self.assertIn("memsearch[onnx]", uv_log.read_text(encoding="utf-8"))
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual("claude", config["kanban_agent"])
        self.assertFalse(config["memsearch"]["enabled"])

    def test_welcome_claude_enables_memsearch_after_cli_repair_with_plugin(
        self,
    ) -> None:
        """Claude 插件已就绪时, 修复不可读 CLI 后可启用 MemSearch."""
        self.install_fake_environment(tmux=True, memsearch=False)
        self.fake_command(
            "memsearch", "#!/bin/sh\nprintf '%s\\n' 'memsearch, version unknown'\n"
        )
        uv_log, _, _ = self.install_fake_memsearch_tools()
        self.install_fake_claude_memsearch_plugin("8.7.6")

        returncode, output = self.run_on_tty(
            "2\n1\n1\n1\n1\n1\n1\n1\n", "welcome"
        )

        self.assertEqual(0, returncode, output)
        self.assertIn("memsearch[onnx]", uv_log.read_text(encoding="utf-8"))
        self.assertIn("Claude 插件已接入", output)
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual("claude", config["kanban_agent"])
        self.assertTrue(config["memsearch"]["enabled"])

    def test_welcome_skips_when_codex_installer_missing(self) -> None:
        """最新源码缺 install.sh 时不得标 enabled."""
        self.install_fake_environment(tmux=True, memsearch=False)
        uv_log, _, bash_log = self.install_fake_memsearch_tools(
            create_installer=False
        )

        returncode, output = self.run_on_tty(
            "1\n1\n1\n1\n1\n1\n1\n1\n", "welcome"
        )

        self.assertEqual(0, returncode, output)
        self.assertIn("找不到 MemSearch Codex 安装器", output)
        self.assertIn("MemSearch 安装未完成", output)
        self.assertFalse(bash_log.exists())
        self.assertIn("memsearch[onnx]", uv_log.read_text(encoding="utf-8"))
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertFalse(config["memsearch"]["enabled"])

    def test_welcome_keeps_clean_cache_when_latest_installer_is_missing(self) -> None:
        self.install_fake_environment(tmux=True, memsearch=False)
        self.install_fake_memsearch_tools(create_installer=False)
        source = self.root / "memsearch-source"
        installer = source / "plugins" / "codex" / "scripts" / "install.sh"
        installer.parent.mkdir(parents=True)
        installer.write_text("#!/bin/sh\n", encoding="utf-8")
        marker = source / "old-cache"
        marker.write_text("keep\n", encoding="utf-8")

        returncode, output = self.run_on_tty(
            "1\n1\n1\n1\n1\n1\n1\n1\n", "welcome"
        )

        self.assertEqual(0, returncode, output)
        self.assertIn("找不到 MemSearch Codex 安装器", output)
        self.assertTrue(marker.is_file())

    def test_welcome_restores_clean_cache_when_latest_installer_fails(self) -> None:
        self.install_fake_environment(tmux=True, memsearch=False)
        self.install_fake_memsearch_tools(installer_exit=1)
        source = self.root / "memsearch-source"
        installer = source / "plugins" / "codex" / "scripts" / "install.sh"
        installer.parent.mkdir(parents=True)
        installer.write_text("#!/bin/sh\n", encoding="utf-8")
        marker = source / "old-cache"
        marker.write_text("keep\n", encoding="utf-8")
        hooks = self.home / ".codex" / "hooks.json"
        hooks.parent.mkdir(parents=True)
        hooks.write_text('{"original": true}\n', encoding="utf-8")
        config_file = self.home / ".codex" / "config.toml"
        config_file.write_text("original = true\n", encoding="utf-8")
        skill = self.home / ".agents" / "skills" / "memory-recall" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("original skill\n", encoding="utf-8")

        returncode, output = self.run_on_tty(
            "1\n1\n1\n1\n1\n1\n1\n1\n", "welcome"
        )

        self.assertEqual(0, returncode, output)
        self.assertIn("Codex 插件安装命令未成功退出; 已恢复原缓存", output)
        self.assertTrue(marker.is_file())
        self.assertEqual('{"original": true}\n', hooks.read_text(encoding="utf-8"))
        self.assertEqual("original = true\n", config_file.read_text(encoding="utf-8"))
        self.assertEqual("original skill\n", skill.read_text(encoding="utf-8"))
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertFalse(config["memsearch"]["enabled"])

    def test_welcome_restores_cache_when_codex_installer_cannot_start(self) -> None:
        self.install_fake_environment(tmux=True, memsearch=False)
        self.install_fake_memsearch_tools(create_bash=False)
        source = self.root / "memsearch-source"
        installer = source / "plugins" / "codex" / "scripts" / "install.sh"
        installer.parent.mkdir(parents=True)
        installer.write_text("#!/bin/sh\n", encoding="utf-8")
        marker = source / "old-cache"
        marker.write_text("keep\n", encoding="utf-8")
        hooks = self.home / ".codex" / "hooks.json"
        hooks.parent.mkdir(parents=True)
        hooks.write_text('{"original": true}\n', encoding="utf-8")

        returncode, output = self.run_on_tty(
            "1\n1\n1\n1\n1\n1\n1\n1\n", "welcome"
        )

        self.assertEqual(0, returncode, output)
        self.assertIn("MemSearch 安装过程异常", output)
        self.assertTrue(marker.is_file())
        self.assertEqual('{"original": true}\n', hooks.read_text(encoding="utf-8"))
        self.assertFalse((self.home / ".codex" / "config.toml").exists())
        self.assertFalse(
            (self.home / ".agents" / "skills" / "memory-recall").exists()
        )

    def test_welcome_reports_single_tmux_session_hint(self) -> None:
        """tmux 已装但当前不在 session 时, welcome/doctor 只给一次准确命令."""
        self.install_fake_environment(tmux=True)
        self.env.pop("TMUX", None)

        # MemSearch 已就绪, 无安装提问: agent + 4 reviewers + launcher + save.
        returncode, output = self.run_on_tty(
            "1\n1\n1\n1\n1\n1\n1\n", "welcome"
        )

        self.assertEqual(0, returncode, output)
        self.assertIn("已安装但当前不在 session", output)
        self.assertEqual(1, output.count("tmux new -A -s onevoke"))

    def test_latest_claude_plugin_matches_structural_contract(self) -> None:
        """用上游默认分支最新版验证 Claude 插件结构契约."""
        self.install_fake_environment(tmux=True, memsearch=False)
        onevoke = load_onevoke_module()
        source = (
            self.home / ".claude" / "plugins" / "marketplaces" / "memsearch-plugins"
        )
        source.parent.mkdir(parents=True)
        clone = subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                onevoke.MEMSEARCH_REPOSITORY,
                str(source),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if clone.returncode != 0:
            self.fail(f"无法拉取 MemSearch 默认分支最新版: {clone.stderr}")
        source_plugin = source / "plugins" / "claude-code"
        manifest = json.loads(
            (source_plugin / ".claude-plugin" / "plugin.json").read_text(
                encoding="utf-8"
            )
        )
        version = manifest["version"]
        plugin = (
            self.home
            / ".claude"
            / "plugins"
            / "cache"
            / "memsearch-plugins"
            / "memsearch"
            / version
        )
        shutil.copytree(source_plugin, plugin)
        revision = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            json.dumps({"enabledPlugins": {"memsearch@memsearch-plugins": True}}),
            encoding="utf-8",
        )
        installed = self.home / ".claude" / "plugins" / "installed_plugins.json"
        installed.parent.mkdir(parents=True, exist_ok=True)
        installed.write_text(
            json.dumps(
                {
                    "plugins": {
                        "memsearch@memsearch-plugins": [
                            {
                                "installPath": str(plugin),
                                "version": version,
                                "gitCommitSha": revision,
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        marketplaces = self.home / ".claude" / "plugins" / "known_marketplaces.json"
        marketplaces.write_text(
            json.dumps(
                {
                    "memsearch-plugins": {
                        "source": {
                            "source": "github",
                            "repo": "zilliztech/memsearch",
                        },
                        "installLocation": str(source),
                    }
                }
            ),
            encoding="utf-8",
        )
        for script in (plugin / "hooks").glob("*.sh"):
            script.chmod(script.stat().st_mode | 0o111)

        with mock.patch.object(Path, "home", return_value=self.home):
            self.assertTrue(onevoke.claude_memsearch_ready())
            readme = plugin / "README.md"
            original = readme.read_bytes()
            readme.write_bytes(original + b"\n")
            self.assertFalse(onevoke.claude_memsearch_ready())
            readme.write_bytes(original)
            common = plugin / "hooks" / "common.sh"
            common.unlink()
            self.assertFalse(onevoke.claude_memsearch_ready())

    def test_welcome_ctrl_c_exits_without_traceback_or_config(self) -> None:
        self.install_fake_environment(tmux=False)
        master, slave = pty.openpty()
        process = subprocess.Popen(
            [sys.executable, str(ONEVOKE), "welcome"],
            env=self.env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
        )
        os.close(slave)
        output = bytearray()
        try:
            while "请选择".encode("utf-8") not in output:
                output.extend(os.read(master, 4096))
            process.send_signal(signal.SIGINT)
            returncode = process.wait(timeout=10)
            while True:
                try:
                    output.extend(os.read(master, 4096))
                except OSError:
                    break
        finally:
            os.close(master)
            if process.poll() is None:
                process.kill()
                process.wait()

        decoded = output.decode("utf-8", "replace")
        self.assertEqual(130, returncode, decoded)
        self.assertIn("用户取消, 配置未更改", decoded)
        self.assertNotIn("Traceback", decoded)
        self.assertFalse(self.config.exists())


if __name__ == "__main__":
    unittest.main()
