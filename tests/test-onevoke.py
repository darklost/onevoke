#!/usr/bin/env python3

import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

if os.name == "posix":
    import pty
else:
    pty = None


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


@unittest.skipUnless(os.name == "posix", "PTY welcome tests require POSIX")
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
        for name in (
            "HERDR_ENV",
            "HERDR_WORKSPACE_ID",
            "HERDR_TAB_ID",
            "HERDR_PANE_ID",
        ):
            self.env.pop(name, None)

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

    def install_fake_environment(self, *, tmux: bool) -> None:
        for name in (
            "onevoke",
            "kanban",
            "onevoke-review.sh",
            "merge-worktree-memory.py",
            "codex",
            "claude",
            "grok",
        ):
            self.fake_command(name)
        if tmux:
            self.fake_command("tmux")

    def install_fake_memsearch_tools(
        self,
        *, git_exit: int = 0,
    ) -> tuple[Path, Path]:
        git_log = self.root / "git.log"
        bash_log = self.root / "bash.log"
        self.env.update(
            {
                "GIT_LOG": str(git_log),
                "BASH_LOG": str(bash_log),
                "ONEVOKE_MEMSEARCH_SOURCE": str(self.root / "memsearch-source"),
            }
        )
        self.fake_command(
            "git",
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" > \"$GIT_LOG\"\n"
            f"exit {git_exit}\n",
        )
        self.fake_command("bash", "#!/bin/sh\nprintf '%s\\n' \"$*\" > \"$BASH_LOG\"\n")
        return git_log, bash_log

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
        self.assertIn("--lang", chinese.stdout)
        self.assertIn("{cn,en}", chinese.stdout)
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

        for name in ("ONEVOKE_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
            self.env.pop(name, None)
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

        returncode, output = self.run_on_tty("\n", "welcome")

        self.assertEqual(0, returncode, output)
        self.assertIn("Current configuration", output)
        self.assertIn("Press Enter to save", output)
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

        # 只修改三个 Reviewer 和 MemSearch, 其余选项保留当前值.
        returncode, output = self.run_on_tty(
            "2\n2\n3\n3\n5\n2\n8\nyes\n\n", "welcome"
        )

        self.assertEqual(0, returncode, output)
        self.assertIn("\033[1;31m[!] 未安装 tmux", output)
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual("codex", config["kanban_agent"])
        self.assertEqual("foreground", config["launcher"])
        self.assertEqual(
            {"PM": "claude", "CSA": "grok", "Hacker": "codex", "QA": "claude"},
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
        self.install_fake_environment(tmux=True)
        answers = "1\n\n8\n\n\n"
        prompts = (
            "当前配置",
            "kanban 默认用哪个 Agent 执行任务?",
            "使用 MemSearch?",
        )

        returncode, output = self.run_on_tty(answers, "welcome")

        self.assertEqual(0, returncode, output)
        for prompt in prompts:
            self.assertIn(f"\033[1;36m{prompt}\033[0m", output)
        self.assertNotIn("  1. Yes", output)
        self.assertIn("[y/N]", output)

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

        # 进入启动方式菜单, 选择安装 tmux, 回车沿用当前 session 模式, 再回车保存.
        returncode, output = self.run_on_tty("6\n2\n\n\n", "welcome")

        self.assertEqual(0, returncode, output)
        self.assertEqual("install tmux", brew_log.read_text(encoding="utf-8").strip())
        self.assertIn("tmux 已就绪, 用哪种 tmux 启动方式?", output)
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual("tmux", config["launcher"])

    def test_memsearch_install_only_clones_and_runs_codex_script(self) -> None:
        git_log, bash_log = self.install_fake_memsearch_tools()
        onevoke = load_onevoke_module()

        with mock.patch.dict(os.environ, self.env, clear=True):
            onevoke.install_memsearch_for("codex")

        source = Path(self.env["ONEVOKE_MEMSEARCH_SOURCE"])
        self.assertEqual(
            f"clone https://github.com/zilliztech/memsearch.git {source}",
            git_log.read_text(encoding="utf-8").strip(),
        )
        self.assertEqual(
            str(source / "plugins" / "codex" / "scripts" / "install.sh"),
            bash_log.read_text(encoding="utf-8").strip(),
        )

    def test_memsearch_clone_failure_does_not_block_welcome(self) -> None:
        self.install_fake_environment(tmux=True)
        _, bash_log = self.install_fake_memsearch_tools(git_exit=1)

        returncode, output = self.run_on_tty("8\ny\n\n", "welcome")

        self.assertEqual(0, returncode, output)
        self.assertIn("MemSearch 安装命令无法执行", output)
        self.assertFalse(bash_log.exists())
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertTrue(config["memsearch"]["enabled"])

    def test_memsearch_destination_failure_does_not_block_welcome(self) -> None:
        self.install_fake_environment(tmux=True)
        _, bash_log = self.install_fake_memsearch_tools()
        blocked_parent = self.root / "blocked"
        blocked_parent.write_text("not a directory\n", encoding="utf-8")
        self.env["ONEVOKE_MEMSEARCH_SOURCE"] = str(blocked_parent / "memsearch")

        returncode, output = self.run_on_tty("8\nyes\n\n", "welcome")

        self.assertEqual(0, returncode, output)
        self.assertIn("MemSearch 安装命令无法执行", output)
        self.assertFalse(bash_log.exists())
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertTrue(config["memsearch"]["enabled"])

    def test_doctor_fails_without_any_agent_or_reviewer(self) -> None:
        for name in (
            "onevoke",
            "kanban",
            "onevoke-review.sh",
            "merge-worktree-memory.py",
        ):
            self.fake_command(name)

        result = self.run_command("doctor")

        self.assertEqual(1, result.returncode)
        self.assertIn("没有发现可执行 Agent", result.stderr)
        self.assertIn("没有发现 Reviewer", result.stderr)

    def test_invalid_config_is_reported_without_fallback(self) -> None:
        self.config.parent.mkdir(parents=True)
        self.config.write_text("not json\n", encoding="utf-8")

        result = self.run_command("config")

        self.assertEqual(1, result.returncode)
        self.assertIn("读取配置失败", result.stderr)

    def test_config_defaults_to_human_output_and_json_is_explicit(self) -> None:
        human = self.run_command("config")
        machine = self.run_command("config", "--json")

        self.assertIn("引导: 未完成", human.stdout)
        self.assertIn("看板 Agent: codex", human.stdout)
        self.assertIn("默认语言: 中文", human.stdout)
        self.assertFalse(human.stdout.lstrip().startswith("{"))
        self.assertFalse(json.loads(machine.stdout)["welcome_complete"])
        self.assertEqual("cn", json.loads(machine.stdout)["language"])

    def test_config_language_overrides_env_without_cli(self) -> None:
        config = {
            "schema_version": 1,
            "welcome_complete": True,
            "kanban_agent": "codex",
            "launcher": "tmux",
            "language": "en",
            "reviewers": {role: "codex" for role in ROLES},
            "memsearch": {"enabled": False},
        }
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(config), encoding="utf-8")
        self.env["ONEVOKE_LANG"] = "zh"

        status = self.run_command("config")

        self.assertEqual(0, status.returncode, status.stderr)
        self.assertIn("welcome: complete", status.stdout)
        self.assertIn("language: English", status.stdout)

    def test_legacy_config_without_language_respects_env(self) -> None:
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
        self.env["ONEVOKE_LANG"] = "en"

        status = self.run_command("config")

        self.assertEqual(0, status.returncode, status.stderr)
        self.assertIn("welcome: complete", status.stdout)
        self.assertIn("language: English", status.stdout)

    def test_cli_lang_overrides_config_language(self) -> None:
        config = {
            "schema_version": 1,
            "welcome_complete": True,
            "kanban_agent": "codex",
            "launcher": "tmux",
            "language": "en",
            "reviewers": {role: "codex" for role in ROLES},
            "memsearch": {"enabled": False},
        }
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(config), encoding="utf-8")
        self.env["ONEVOKE_LANG"] = "en"

        status = self.run_command("--lang", "cn", "config")

        self.assertEqual(0, status.returncode, status.stderr)
        self.assertIn("引导: 已完成", status.stdout)
        self.assertIn("默认语言: 英文", status.stdout)

    def test_welcome_configures_language(self) -> None:
        self.install_fake_environment(tmux=False)

        returncode, output = self.run_on_tty("10\n2\n\n", "welcome")

        self.assertEqual(0, returncode, output)
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual("en", config["language"])
        self.assertIn("默认输出语言?", output)

    def test_welcome_reset_uses_env_language_when_config_invalid(self) -> None:
        self.install_fake_environment(tmux=False)
        self.config.parent.mkdir(parents=True)
        self.config.write_text("not json\n", encoding="utf-8")
        self.env["ONEVOKE_LANG"] = "en"

        returncode, output = self.run_on_tty("\n", "welcome", "--reset")

        self.assertEqual(0, returncode, output)
        self.assertIn("Current configuration", output)

    def test_welcome_reset_uses_env_language_when_config_schema_invalid(self) -> None:
        self.install_fake_environment(tmux=False)
        self.config.parent.mkdir(parents=True)
        self.config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "welcome_complete": True,
                    "kanban_agent": "invalid",
                    "launcher": "tmux",
                    "language": "cn",
                    "reviewers": {role: "codex" for role in ROLES},
                    "memsearch": {"enabled": False},
                }
            ),
            encoding="utf-8",
        )
        self.env["ONEVOKE_LANG"] = "en"

        returncode, output = self.run_on_tty("\n", "welcome", "--reset")

        self.assertEqual(0, returncode, output)
        self.assertIn("Current configuration", output)

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

        returncode, output = self.run_on_tty("q\n", "welcome", "--reset")

        self.assertEqual(1, returncode, output)
        self.assertIn("用户取消, 配置未更改", output)
        self.assertEqual(before, self.config.read_bytes())

    def test_welcome_customizes_models_only_for_agents_in_use(self) -> None:
        self.install_fake_environment(tmux=True)

        # 三次进入模型菜单, 分别只改一个字段; 未选择的值保持不变.
        returncode, output = self.run_on_tty(
            "7\n1\ngpt-7\n7\n3\nlow\n7\n4\ngpt-7-mini\n\n", "welcome"
        )

        self.assertEqual(0, returncode, output)
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(
            {"model": "gpt-7", "large_effort": "high", "small_effort": "low"},
            config["models"]["kanban"]["codex"],
        )
        self.assertEqual(
            {"model": "gpt-7-mini", "effort": "high"},
            config["models"]["review"]["codex"],
        )
        # 未被本次配置使用的 Agent 保持默认, 不被询问.
        self.assertEqual(
            {"model": "", "large_effort": "xhigh", "small_effort": "high"},
            config["models"]["kanban"]["grok"],
        )
        self.assertNotIn("审核 Grok", output)

    def test_welcome_configures_review_stages(self) -> None:
        self.install_fake_environment(tmux=True)

        returncode, output = self.run_on_tty("9\n2\n2\n\n", "welcome")

        self.assertEqual(0, returncode, output)
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual("skip", config["review_stages"]["CSA"])
        for role in ROLES:
            if role == "CSA":
                continue
            self.assertEqual("auto", config["review_stages"][role], role)

    def test_welcome_reset_changes_one_item_and_keeps_other_values(self) -> None:
        self.install_fake_environment(tmux=True)
        existing = {
            "schema_version": 1,
            "welcome_complete": True,
            "kanban_agent": "claude",
            "launcher": "foreground",
            "reviewers": {role: "grok" for role in ROLES},
            "review_stages": {
                "PM": "required",
                "CSA": "skip",
                "Hacker": "skip",
                "QA": "auto",
            },
            "models": {
                "kanban": {"claude": {"model": "custom-task", "large_effort": "max"}},
                "review": {"grok": {"model": "custom-review", "effort": "xhigh"}},
            },
            "memsearch": {"enabled": True},
        }
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(existing), encoding="utf-8")

        # 只把 PM Reviewer 改成 Codex, 然后直接回车保存.
        returncode, output = self.run_on_tty("2\n1\n\n", "welcome", "--reset")

        self.assertEqual(0, returncode, output)
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual("codex", config["reviewers"]["PM"])
        self.assertEqual({role: "grok" for role in ("CSA", "Hacker", "QA")}, {
            role: config["reviewers"][role] for role in ("CSA", "Hacker", "QA")
        })
        self.assertEqual("claude", config["kanban_agent"])
        self.assertEqual("foreground", config["launcher"])
        self.assertEqual("custom-task", config["models"]["kanban"]["claude"]["model"])
        self.assertEqual("custom-review", config["models"]["review"]["grok"]["model"])
        self.assertTrue(config["memsearch"]["enabled"])
        self.assertEqual(
            {
                "PM": "required",
                "CSA": "skip",
                "Hacker": "skip",
                "QA": "auto",
            },
            config["review_stages"],
        )

    def test_welcome_reset_enter_preserves_unavailable_current_values(self) -> None:
        self.install_fake_environment(tmux=False)
        self.fake_command("grok", "#!/bin/sh\nexit 1\n")
        existing = {
            "schema_version": 1,
            "welcome_complete": True,
            "kanban_agent": "grok",
            "launcher": "tmux",
            "reviewers": {role: "grok" for role in ROLES},
            "memsearch": {"enabled": True},
        }
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(existing), encoding="utf-8")

        # 即使进入当前不可用的 Agent、Reviewer 和 launcher, 空回车也保留当前值.
        returncode, output = self.run_on_tty(
            "1\n\n2\n\n6\n\n\n", "welcome", "--reset"
        )

        self.assertEqual(0, returncode, output)
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual("grok", config["kanban_agent"])
        self.assertEqual({role: "grok" for role in ROLES}, config["reviewers"])
        self.assertEqual("tmux", config["launcher"])
        self.assertTrue(config["memsearch"]["enabled"])
        self.assertIn("Grok (当前不可用) (当前)", output)
        self.assertIn("tmux 当前 session 新窗口 (当前未安装) (当前)", output)

    def test_welcome_failed_tmux_install_keeps_current_launcher(self) -> None:
        self.install_fake_environment(tmux=False)
        existing = {
            "schema_version": 1,
            "welcome_complete": True,
            "kanban_agent": "codex",
            "launcher": "tmux",
            "reviewers": {role: "codex" for role in ROLES},
            "memsearch": {"enabled": False},
        }
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(existing), encoding="utf-8")

        # 无包管理器时安装失败; 第三个 launcher 选项是安装 tmux.
        returncode, output = self.run_on_tty("6\n3\n\n", "welcome", "--reset")

        self.assertEqual(0, returncode, output)
        self.assertIn("没有找到受支持的包管理器", output)
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual("tmux", config["launcher"])

    def test_welcome_reset_works_when_all_agents_are_unavailable(self) -> None:
        self.install_fake_environment(tmux=True)
        for agent in ("codex", "claude", "grok"):
            self.fake_command(agent, "#!/bin/sh\nexit 1\n")
        existing = {
            "schema_version": 1,
            "welcome_complete": True,
            "kanban_agent": "claude",
            "launcher": "tmux",
            "reviewers": {
                "PM": "codex",
                "CSA": "claude",
                "Hacker": "grok",
                "QA": "codex",
            },
            "memsearch": {"enabled": True},
        }
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(existing), encoding="utf-8")

        # 所有 Agent 暂不可用时仍可只把 launcher 改成 foreground.
        returncode, output = self.run_on_tty("6\n3\n\n", "welcome", "--reset")

        self.assertEqual(0, returncode, output)
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual("foreground", config["launcher"])
        self.assertEqual("claude", config["kanban_agent"])
        self.assertEqual(existing["reviewers"], config["reviewers"])
        self.assertTrue(config["memsearch"]["enabled"])
        self.assertIn("不可作为新选择", output)

    def test_first_welcome_still_rejects_when_all_agents_are_unavailable(self) -> None:
        self.install_fake_environment(tmux=True)
        for agent in ("codex", "claude", "grok"):
            self.fake_command(agent, "#!/bin/sh\nexit 1\n")

        returncode, output = self.run_on_tty("", "welcome")

        self.assertEqual(1, returncode, output)
        self.assertIn("没有发现可用的 Agent", output)
        self.assertFalse(self.config.exists())

    def test_yes_no_uses_text_input_and_enter_uses_default(self) -> None:
        onevoke = load_onevoke_module()
        stderr = io.StringIO()
        with mock.patch.object(onevoke.sys, "stdin", io.StringIO("1\nyes\n")):
            with mock.patch.object(onevoke.sys, "stderr", stderr):
                self.assertTrue(onevoke.ask_yes_no("Continue?", default=False))
        output = stderr.getvalue()
        self.assertIn("[y/N]", output)
        self.assertIn("请输入 yes 或 no", output)
        self.assertNotIn("1. Yes", output)

        with mock.patch.object(onevoke.sys, "stdin", io.StringIO("\n")):
            with mock.patch.object(onevoke.sys, "stderr", io.StringIO()):
                self.assertTrue(onevoke.ask_yes_no("Continue?", default=True))

    def test_config_cli_prints_review_model_from_effective_config(self) -> None:
        config_module = str(PROJECT_ROOT / "bin" / "onevoke_config.py")

        def query(agent: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                [sys.executable, config_module, "review-model", agent],
                env=self.env,
                text=True,
                capture_output=True,
                check=False,
            )

        missing = query("codex")
        self.assertEqual(0, missing.returncode, missing.stderr)
        self.assertEqual("gpt-5.6-sol\nhigh", missing.stdout.strip("\n"))

        config = {
            "schema_version": 1,
            "welcome_complete": True,
            "kanban_agent": "codex",
            "launcher": "tmux",
            "reviewers": {role: "codex" for role in ROLES},
            "models": {"review": {"codex": {"model": "custom-model", "effort": "medium"}}},
            "memsearch": {"enabled": False},
        }
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(config), encoding="utf-8")

        custom = query("codex")
        self.assertEqual(0, custom.returncode, custom.stderr)
        self.assertEqual("custom-model\nmedium", custom.stdout.strip("\n"))
        # 空 model 输出空首行; 未覆盖的 agent 仍用默认.
        self.assertEqual("\nhigh", query("grok").stdout.rstrip("\n"))
        self.assertEqual("opus\nhigh", query("claude").stdout.strip("\n"))
        config["welcome_complete"] = False
        self.config.write_text(json.dumps(config), encoding="utf-8")
        self.assertEqual("gpt-5.6-sol\nhigh", query("codex").stdout.strip("\n"))

        rejected = query("other")
        self.assertEqual(2, rejected.returncode)

    def test_review_stages_defaults_and_validation(self) -> None:
        sys.path.insert(0, str(ONEVOKE.parent))
        try:
            import onevoke_config
        finally:
            sys.path.pop(0)

        self.assertEqual(
            {role: "auto" for role in ROLES},
            onevoke_config.default_review_stages(),
        )
        validated = onevoke_config.validate_config({
            "schema_version": 1,
            "welcome_complete": True,
            "kanban_agent": "codex",
            "launcher": "tmux",
            "reviewers": {role: "codex" for role in ROLES},
            "memsearch": {"enabled": False},
        })
        self.assertEqual({role: "auto" for role in ROLES}, validated["review_stages"])

        validated = onevoke_config.validate_config({
            "schema_version": 1,
            "welcome_complete": True,
            "kanban_agent": "codex",
            "launcher": "tmux",
            "reviewers": {role: "codex" for role in ROLES},
            "review_stages": {
                "PM": "required",
                "CSA": "skip",
                "Hacker": "skip",
                "QA": "auto",
            },
            "memsearch": {"enabled": False},
        })
        self.assertEqual("skip", validated["review_stages"]["CSA"])

        with self.assertRaises(onevoke_config.ConfigError):
            onevoke_config.validate_config({
                "schema_version": 1,
                "welcome_complete": True,
                "kanban_agent": "codex",
                "launcher": "tmux",
                "reviewers": {role: "codex" for role in ROLES},
                "review_stages": {"PM": "always"},
                "memsearch": {"enabled": False},
            })

        for invalid in (None, "auto", []):
            with self.subTest(review_stages=invalid):
                with self.assertRaises(onevoke_config.ConfigError):
                    onevoke_config.validate_config({
                        "schema_version": 1,
                        "welcome_complete": True,
                        "kanban_agent": "codex",
                        "launcher": "tmux",
                        "reviewers": {role: "codex" for role in ROLES},
                        "review_stages": invalid,
                        "memsearch": {"enabled": False},
                    })

        with self.assertRaises(onevoke_config.ConfigError):
            onevoke_config.validate_config({
                "schema_version": 1,
                "welcome_complete": True,
                "kanban_agent": "codex",
                "launcher": "tmux",
                "language": "fr",
                "reviewers": {role: "codex" for role in ROLES},
                "memsearch": {"enabled": False},
            })

    def test_config_cli_prints_review_stages(self) -> None:
        config_module = ONEVOKE.parent / "onevoke_config.py"

        def query() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, config_module, "review-stages"],
                env=self.env,
                text=True,
                capture_output=True,
                check=False,
            )

        config = {
            "schema_version": 1,
            "welcome_complete": True,
            "kanban_agent": "codex",
            "launcher": "tmux",
            "reviewers": {role: "codex" for role in ROLES},
            "review_stages": {
                "PM": "required",
                "CSA": "skip",
                "Hacker": "skip",
                "QA": "auto",
            },
            "memsearch": {"enabled": False},
        }
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(config), encoding="utf-8")

        result = query()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            ["required", "skip", "skip", "auto"],
            result.stdout.strip().splitlines(),
        )

        status = self.run_command("config")
        self.assertEqual(0, status.returncode, status.stderr)
        self.assertIn("审核环节: PM=required CSA=skip Hacker=skip QA=auto", status.stdout)

    def test_config_rejects_invalid_models_section(self) -> None:
        base = {
            "schema_version": 1,
            "welcome_complete": True,
            "kanban_agent": "codex",
            "launcher": "tmux",
            "reviewers": {role: "codex" for role in ROLES},
            "memsearch": {"enabled": False},
        }
        self.config.parent.mkdir(parents=True)
        cases = (
            ({"review": {"codex": {"model": "x", "effort": ""}}}, "models.review.codex.effort"),
            ({"review": {"codex": {"model": "a\nb"}}}, "models.review.codex.model"),
            ({"review": {"codex": {"effort": "hi\rgh"}}}, "models.review.codex.effort"),
            ({"review": {"codex": {"model": "a\x00b"}}}, "models.review.codex.model"),
            ({"kanban": {"codex": {"large_effort": "hi\x00gh"}}}, "models.kanban.codex.large_effort"),
            (None, "models 必须是 JSON object"),
            ({"review": None}, "models.review 必须是 JSON object"),
            ({"review": {"other": {}}}, "models.review 含未知 agent"),
            ({"extra": {}}, "models 含未知键"),
        )
        for models, expected in cases:
            with self.subTest(models=models):
                self.config.write_text(
                    json.dumps({**base, "models": models}), encoding="utf-8"
                )
                result = self.run_command("config")
                self.assertEqual(1, result.returncode)
                self.assertIn(expected, result.stderr)

    def test_review_dispatches_role_and_agent_to_shared_entrypoint(self) -> None:
        log = self.root / "review.log"
        review_command = self.fake_command(
            "onevoke-review.sh",
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$REVIEW_LOG\"\n",
        )
        self.assertTrue(review_command.exists())
        self.env["REVIEW_LOG"] = str(log)
        config = {
            "schema_version": 1,
            "welcome_complete": True,
            "kanban_agent": "codex",
            "launcher": "tmux",
            "reviewers": {role: "codex" for role in ROLES},
            "memsearch": {"enabled": False},
        }
        config["reviewers"]["QA"] = "claude"
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
            ["claude", "/worktree", "base", "commit", "QA", "目标"],
            log.read_text(encoding="utf-8").splitlines(),
        )

    def test_review_ignores_unfinished_welcome_selections(self) -> None:
        log = self.root / "review.log"
        self.fake_command(
            "onevoke-review.sh",
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$REVIEW_LOG\"\n",
        )
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
        self.assertEqual(
            ["codex", "/worktree", "base", "commit", "QA", "目标"],
            log.read_text(encoding="utf-8").splitlines(),
        )

    def test_review_rejects_missing_shared_entrypoint(self) -> None:
        result = self.run_command(
            "review", "/worktree", "base", "commit", "QA", "目标"
        )

        self.assertEqual(1, result.returncode)
        self.assertIn("onevoke-review.sh 不在 PATH", result.stderr)

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

        # 仅 Grok 可用, 直接保存自动选出的当前值.
        returncode, output = self.run_on_tty("\n", "welcome")

        self.assertEqual(0, returncode, output)
        self.assertIn("--version 失败", output)
        self.assertIn("不可作为新选择", output)
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

    def test_doctor_validates_configured_agents_review_entrypoint_and_launcher(self) -> None:
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
        # Remove the configured execution agent and the single review entrypoint.
        (self.fake_bin / "claude").unlink()
        (self.fake_bin / "onevoke-review.sh").unlink()

        result = self.run_command("doctor")

        self.assertEqual(1, result.returncode)
        self.assertIn("配置的执行 Agent 不可用: claude", result.stderr)
        self.assertIn("onevoke-review.sh 不在 PATH", result.stderr)

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

    def test_doctor_rejects_missing_review_entrypoint(self) -> None:
        self.install_fake_environment(tmux=True)
        (self.fake_bin / "onevoke-review.sh").unlink()
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
        self.assertIn("onevoke-review.sh 不在 PATH", result.stderr)

    def test_welcome_reports_single_tmux_session_hint(self) -> None:
        """tmux 已装但当前不在 session 时, welcome/doctor 只给一次准确命令."""
        self.install_fake_environment(tmux=True)
        self.env.pop("TMUX", None)

        returncode, output = self.run_on_tty("\n", "welcome")

        self.assertEqual(0, returncode, output)
        self.assertIn("已安装但当前不在 session", output)
        self.assertEqual(1, output.count("tmux new -A -s onevoke"))

    def test_welcome_saves_the_per_project_tmux_session_launcher(self) -> None:
        self.install_fake_environment(tmux=True)

        # 启动方式菜单的第二项是项目专属 session.
        returncode, output = self.run_on_tty("6\n2\n\n", "welcome")

        self.assertEqual(0, returncode, output)
        self.assertIn("tmux 项目专属 session 新窗口", output)
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual("tmux-session", config["launcher"])
        self.assertEqual("tmux-session", self.run_command("config").stdout.splitlines()[2].split(": ")[1])

    def test_doctor_accepts_tmux_session_launcher_outside_tmux(self) -> None:
        self.install_fake_environment(tmux=True)
        self.env.pop("TMUX", None)
        config = {
            "schema_version": 1,
            "welcome_complete": True,
            "kanban_agent": "codex",
            "launcher": "tmux-session",
            "reviewers": {role: "codex" for role in ROLES},
            "memsearch": {"enabled": False},
        }
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(config), encoding="utf-8")

        result = self.run_command("doctor")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("按项目自动新建或复用专属 session", result.stderr)
        self.assertNotIn("需要先进入", result.stderr)
        self.assertNotIn("已安装但当前不在 session", result.stderr)

    def test_doctor_rejects_tmux_session_launcher_when_tmux_missing(self) -> None:
        self.install_fake_environment(tmux=False)
        config = {
            "schema_version": 1,
            "welcome_complete": True,
            "kanban_agent": "codex",
            "launcher": "tmux-session",
            "reviewers": {role: "codex" for role in ROLES},
            "memsearch": {"enabled": False},
        }
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(config), encoding="utf-8")

        result = self.run_command("doctor")

        self.assertEqual(1, result.returncode)
        self.assertIn("配置的 launcher 是 tmux-session", result.stderr)

    def test_welcome_saves_the_herdr_launcher_after_tmux_session(self) -> None:
        self.install_fake_environment(tmux=True)
        self.fake_command("herdr")

        # tmux 已装时菜单为 tmux, tmux-session, herdr, foreground.
        returncode, output = self.run_on_tty("6\n3\n\n", "welcome")

        self.assertEqual(0, returncode, output)
        session_idx = output.index("tmux 项目专属 session 新窗口")
        herdr_idx = output.index("herdr 当前 workspace 新标签")
        foreground_idx = output.index("当前终端前台运行")
        self.assertLess(session_idx, herdr_idx)
        self.assertLess(herdr_idx, foreground_idx)
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual("herdr", config["launcher"])
        self.assertEqual("herdr", self.run_command("config").stdout.splitlines()[2].split(": ")[1])

    def test_welcome_lists_unavailable_herdr_when_currently_selected(self) -> None:
        self.install_fake_environment(tmux=True)
        existing = {
            "schema_version": 1,
            "welcome_complete": True,
            "kanban_agent": "codex",
            "launcher": "herdr",
            "reviewers": {role: "codex" for role in ROLES},
            "memsearch": {"enabled": False},
        }
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(existing), encoding="utf-8")

        returncode, output = self.run_on_tty("6\n\n\n", "welcome", "--reset")

        self.assertEqual(0, returncode, output)
        self.assertIn("herdr 当前 workspace 新标签 (当前未安装) (当前)", output)
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual("herdr", config["launcher"])

    def test_welcome_omits_herdr_when_uninstalled_and_not_selected(self) -> None:
        self.install_fake_environment(tmux=True)

        returncode, output = self.run_on_tty("6\n\n\n", "welcome")

        self.assertEqual(0, returncode, output)
        self.assertNotIn("herdr 当前 workspace", output)
        self.assertIn("当前终端前台运行", output)

    def test_doctor_accepts_herdr_launcher_inside_herdr(self) -> None:
        self.install_fake_environment(tmux=True)
        self.fake_command("herdr")
        self.env["HERDR_ENV"] = "1"
        self.env["HERDR_WORKSPACE_ID"] = "w1"
        config = {
            "schema_version": 1,
            "welcome_complete": True,
            "kanban_agent": "codex",
            "launcher": "herdr",
            "reviewers": {role: "codex" for role in ROLES},
            "memsearch": {"enabled": False},
        }
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(config), encoding="utf-8")

        result = self.run_command("doctor")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("launcher=herdr 会在当前 workspace 新建 tab", result.stderr)
        self.assertNotIn("需要当前处于 herdr", result.stderr)

    def test_doctor_hints_herdr_launcher_requires_herdr_env(self) -> None:
        self.install_fake_environment(tmux=True)
        self.fake_command("herdr")
        config = {
            "schema_version": 1,
            "welcome_complete": True,
            "kanban_agent": "codex",
            "launcher": "herdr",
            "reviewers": {role: "codex" for role in ROLES},
            "memsearch": {"enabled": False},
        }
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(config), encoding="utf-8")

        result = self.run_command("doctor")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("launcher=herdr 需要当前处于 herdr", result.stderr)

    def test_doctor_rejects_herdr_launcher_without_workspace_id(self) -> None:
        self.install_fake_environment(tmux=True)
        self.fake_command("herdr")
        self.env["HERDR_ENV"] = "1"
        config = {
            "schema_version": 1,
            "welcome_complete": True,
            "kanban_agent": "codex",
            "launcher": "herdr",
            "reviewers": {role: "codex" for role in ROLES},
            "memsearch": {"enabled": False},
        }
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(config), encoding="utf-8")

        result = self.run_command("doctor")

        self.assertEqual(1, result.returncode)
        self.assertIn("缺少 HERDR_WORKSPACE_ID", result.stderr)

    def test_doctor_rejects_herdr_launcher_when_herdr_missing(self) -> None:
        self.install_fake_environment(tmux=True)
        config = {
            "schema_version": 1,
            "welcome_complete": True,
            "kanban_agent": "codex",
            "launcher": "herdr",
            "reviewers": {role: "codex" for role in ROLES},
            "memsearch": {"enabled": False},
        }
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(config), encoding="utf-8")

        result = self.run_command("doctor")

        self.assertEqual(1, result.returncode)
        self.assertIn("配置的 launcher 是 herdr", result.stderr)
        self.assertIn("herdr 不在 PATH", result.stderr)

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

    def test_project_rules_integration_rejects_global_onevoke_entry(self) -> None:
        onevoke = load_onevoke_module()
        project = self.root / "app"
        rules_dir = project / ".onevoke" / "rules"
        rules_dir.mkdir(parents=True)
        entry = rules_dir / "ONEVOKE-AGENTS.md"
        entry.write_text("# project Onevoke entry\n", encoding="utf-8")
        global_entry = self.home / ".agents" / "ONEVOKE-AGENTS.md"
        global_entry.parent.mkdir(parents=True)
        global_entry.write_text("# global Onevoke entry\n", encoding="utf-8")
        paths = onevoke.InstallPaths(
            mode="project",
            config_path=project / ".onevoke" / "config.json",
            rules_dir=rules_dir,
            bin_dir=project / ".onevoke" / "bin",
            share_dir=project / ".onevoke" / "share",
            project_root=project,
            install_root=project / ".onevoke",
        )
        claude = self.home / ".claude" / "CLAUDE.md"
        claude.parent.mkdir(parents=True)
        with mock.patch.object(onevoke, "install_paths", return_value=paths):
            with mock.patch.object(Path, "home", return_value=self.home):
                claude.write_text("@~/.agents/ONEVOKE-AGENTS.md\n", encoding="utf-8")
                ok, _ = onevoke.rules_integration("claude")
                self.assertFalse(ok)
                claude.write_text(f"@{entry}\n", encoding="utf-8")
                ok, detail = onevoke.rules_integration("claude")
                self.assertTrue(ok, detail)
                self.assertEqual(str(claude), detail)
                source = self.root / "dotfiles" / "CLAUDE.md"
                source.parent.mkdir(parents=True)
                source.write_text(f"@{entry}\n", encoding="utf-8")
                claude.unlink()
                claude.symlink_to(source)
                ok, detail = onevoke.rules_integration("claude")
                self.assertTrue(ok, detail)
                source.write_text("@~/.agents/ONEVOKE-AGENTS.md\n", encoding="utf-8")
                ok, _ = onevoke.rules_integration("claude")
                self.assertFalse(ok)
                claude.unlink()

                body = "# shared Onevoke entry\n"
                entry.write_text(body, encoding="utf-8")
                global_entry.write_text(body, encoding="utf-8")
                codex = self.home / ".codex" / "AGENTS.md"
                codex.parent.mkdir(parents=True)
                if codex.exists() or codex.is_symlink():
                    codex.unlink()
                codex.symlink_to(global_entry)
                ok, _ = onevoke.rules_integration("codex")
                self.assertFalse(ok)
                codex.unlink()
                codex.symlink_to(entry)
                ok, detail = onevoke.rules_integration("codex")
                self.assertTrue(ok, detail)
                codex.unlink()
                codex.write_text("# global Onevoke entry\n\n## extra\n", encoding="utf-8")
                ok, _ = onevoke.rules_integration("codex")
                self.assertFalse(ok)
                codex.write_text(body + "\n## extra\n", encoding="utf-8")
                ok, detail = onevoke.rules_integration("codex")
                self.assertTrue(ok, detail)


class ProjectOnevokeRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.language = mock.patch.dict(os.environ, {"ONEVOKE_LANG": "zh"})
        self.language.start()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.global_config = self.home / ".config" / "onevoke" / "config.json"
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        self.project = self.root / "app"
        self.env = os.environ.copy()
        self.env["HOME"] = str(self.home)
        self.env["USERPROFILE"] = str(self.home)
        self.env.pop("ONEVOKE_CONFIG", None)
        self.env["PATH"] = str(self.fake_bin) + os.pathsep + os.environ.get("PATH", "")
        self.env.pop("NO_COLOR", None)
        self.env["ONEVOKE_LANG"] = "zh"

    def tearDown(self) -> None:
        self.temp.cleanup()
        self.language.stop()

    def init_git_repo(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "-q", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(path), "config", "user.email", "onevoke@example.com"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(path), "config", "user.name", "Onevoke Test"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(path), "commit", "--allow-empty", "-q", "-m", "init"],
            check=True,
            capture_output=True,
            text=True,
        )
        return path

    def fake_command(self, name: str, body: str | None = None) -> Path:
        command = self.fake_bin / name
        command.write_text(
            body or f"#!/bin/sh\nprintf '%s\\n' '{name} test-version'\n",
            encoding="utf-8",
        )
        command.chmod(0o755)
        return command

    def write_config(self, path: Path, **overrides: object) -> None:
        payload: dict[str, object] = {
            "schema_version": 1,
            "welcome_complete": True,
            "kanban_agent": "codex",
            "launcher": "tmux",
            "reviewers": {role: "codex" for role in ROLES},
            "memsearch": {"enabled": False},
        }
        payload.update(overrides)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def install_project_onevoke(self, *, review_body: str | None = None, commands: bool = True) -> Path:
        project = self.init_git_repo(self.project)
        bin_dir = project / ".onevoke" / "bin"
        bin_dir.mkdir(parents=True)
        rules_dir = project / ".onevoke" / "rules"
        rules_dir.mkdir(parents=True)
        for name in ("onevoke", "onevoke_config.py", "onevoke_fs.py"):
            shutil.copy2(PROJECT_ROOT / "bin" / name, bin_dir / name)
        (bin_dir / "onevoke").chmod(0o755)
        (rules_dir / "ONEVOKE-AGENTS.md").write_text(
            "# Onevoke 全局工作流规则\n\n项目规则入口\n",
            encoding="utf-8",
        )
        if commands:
            for name in ("kanban", "merge-worktree-memory.py"):
                command = bin_dir / name
                command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                command.chmod(0o755)
            if os.name == "nt":
                review_name = "onevoke-review.cmd"
                body = review_body or "@echo off\r\nexit /b 0\r\n"
            else:
                review_name = "onevoke-review.sh"
                body = review_body or "#!/bin/sh\nexit 0\n"
            review = bin_dir / review_name
            review.write_text(body, encoding="utf-8")
            review.chmod(0o755)
        return project

    def run_project(self, *args: str) -> subprocess.CompletedProcess:
        project_onevoke = self.project / ".onevoke" / "bin" / "onevoke"
        return subprocess.run(
            [sys.executable, str(project_onevoke), *args],
            env=self.env,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_project_config_reads_and_writes_project_file_not_global(self) -> None:
        self.install_project_onevoke()
        self.write_config(self.global_config, kanban_agent="grok")

        missing = self.run_project("config")
        self.assertEqual(0, missing.returncode, missing.stderr)
        self.assertIn("引导: 未完成", missing.stdout)
        self.assertIn("看板 Agent: codex", missing.stdout)
        self.assertEqual(
            "grok",
            json.loads(self.global_config.read_text(encoding="utf-8"))["kanban_agent"],
        )

        project_config = self.project / ".onevoke" / "config.json"
        self.write_config(project_config, kanban_agent="claude")
        present = self.run_project("config")
        self.assertEqual(0, present.returncode, present.stderr)
        self.assertIn("看板 Agent: claude", present.stdout)
        self.assertEqual(
            "grok",
            json.loads(self.global_config.read_text(encoding="utf-8"))["kanban_agent"],
        )
        self.assertNotIn(str(self.global_config), present.stdout + present.stderr)

    def test_project_doctor_uses_local_paths_and_ignores_global_commands(self) -> None:
        self.install_project_onevoke()
        self.write_config(self.project / ".onevoke" / "config.json")
        for name in ("codex", "claude", "grok", "tmux"):
            self.fake_command(name)
        trap = self.fake_command("onevoke-review.sh")
        trap_kanban = self.fake_command("kanban")

        result = self.run_project("doctor")
        install_root = (self.project / ".onevoke").resolve()

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("安装模式: 项目", result.stderr)
        self.assertIn(str(install_root), result.stderr)
        self.assertIn(str(install_root / "rules" / "ONEVOKE-AGENTS.md"), result.stderr)
        self.assertIn(str(install_root / "bin" / "kanban"), result.stderr)
        self.assertIn(str(install_root / "config.json"), result.stderr)
        self.assertNotIn(str(trap), result.stderr)
        self.assertNotIn(str(trap_kanban), result.stderr)
        self.assertNotIn("~/.local/bin", result.stderr)
        self.assertNotIn(".agents", result.stderr)
        self.assertNotIn(".config/onevoke", result.stderr)

    def test_project_doctor_missing_command_does_not_point_to_global_path(self) -> None:
        self.install_project_onevoke()
        (self.project / ".onevoke" / "bin" / "kanban").unlink()
        self.write_config(self.project / ".onevoke" / "config.json")
        self.fake_command("codex")
        self.fake_command("kanban")

        result = self.run_project("doctor")

        self.assertEqual(1, result.returncode)
        expected = (self.project / ".onevoke" / "bin" / "kanban").resolve()
        self.assertIn(f"kanban 不在项目命令根: {expected}", result.stderr)
        self.assertNotIn("~/.local/bin", result.stderr)
        self.assertNotIn("不在 PATH", result.stderr)

    @unittest.skipUnless(os.name == "posix", "POSIX review dispatch uses onevoke-review.sh")
    def test_project_review_uses_local_gate_not_path(self) -> None:
        log = self.root / "review.log"
        self.env["REVIEW_LOG"] = str(log)
        self.install_project_onevoke(
            review_body="#!/bin/sh\nprintf 'PROJECT %s\\n' \"$*\" > \"$REVIEW_LOG\"\n"
        )
        self.write_config(
            self.project / ".onevoke" / "config.json",
            reviewers={role: "codex" for role in ROLES} | {"QA": "claude"},
        )
        self.write_config(self.global_config, reviewers={role: "grok" for role in ROLES})
        self.fake_command(
            "onevoke-review.sh",
            "#!/bin/sh\nprintf 'GLOBAL %s\\n' \"$*\" > \"$REVIEW_LOG\"\n",
        )

        result = self.run_project(
            "review", "/worktree", "base", "commit", "qa", "目标"
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            "PROJECT claude /worktree base commit QA 目标\n",
            log.read_text(encoding="utf-8"),
        )

    @unittest.skipUnless(os.name == "posix", "POSIX review dispatch uses onevoke-review.sh")
    def test_project_review_missing_gate_does_not_fallback_to_path(self) -> None:
        log = self.root / "review.log"
        self.env["REVIEW_LOG"] = str(log)
        self.install_project_onevoke()
        (self.project / ".onevoke" / "bin" / "onevoke-review.sh").unlink()
        self.write_config(self.project / ".onevoke" / "config.json")
        self.fake_command(
            "onevoke-review.sh",
            "#!/bin/sh\nprintf 'GLOBAL %s\\n' \"$*\" > \"$REVIEW_LOG\"\n",
        )

        result = self.run_project(
            "review", "/worktree", "base", "commit", "QA", "目标"
        )

        expected = (self.project / ".onevoke" / "bin" / "onevoke-review.sh").resolve()
        self.assertEqual(1, result.returncode)
        self.assertIn(f"审核入口不存在: {expected}", result.stderr)
        self.assertNotIn("不在 PATH", result.stderr)
        self.assertFalse(log.exists())

    def test_project_entry_without_git_does_not_fallback(self) -> None:
        project = self.root / "not-git"
        bin_dir = project / ".onevoke" / "bin"
        bin_dir.mkdir(parents=True)
        for name in ("onevoke", "onevoke_config.py", "onevoke_fs.py"):
            shutil.copy2(PROJECT_ROOT / "bin" / name, bin_dir / name)
        self.write_config(self.global_config, kanban_agent="grok")

        result = subprocess.run(
            [sys.executable, str(bin_dir / "onevoke"), "config"],
            env=self.env,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(1, result.returncode)
        self.assertIn("项目不是 Git 仓库", result.stderr)
        self.assertNotIn("grok", result.stdout)
        self.assertNotIn("看板 Agent", result.stdout)

    @unittest.skipUnless(os.name == "posix", "PTY welcome tests require POSIX")
    def test_project_welcome_saves_project_config_and_points_to_project_rules(self) -> None:
        self.install_project_onevoke()
        self.write_config(self.global_config, kanban_agent="grok")
        for name in ("codex", "claude", "grok"):
            self.fake_command(name)
        self.fake_command("tmux")
        project_config = self.project / ".onevoke" / "config.json"
        rules_entry = (self.project / ".onevoke" / "rules" / "ONEVOKE-AGENTS.md").resolve()

        master, slave = pty.openpty()
        process = subprocess.Popen(
            [sys.executable, str(self.project / ".onevoke" / "bin" / "onevoke"), "welcome"],
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
            os.write(master, b"\n")
            returncode = process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise
        finally:
            os.close(master)
            reader.join(timeout=5)
        output = b"".join(seen).decode("utf-8", "replace")

        self.assertEqual(0, returncode, output)
        self.assertTrue(project_config.is_file())
        saved = json.loads(project_config.read_text(encoding="utf-8"))
        self.assertTrue(saved["welcome_complete"])
        self.assertEqual("codex", saved["kanban_agent"])
        self.assertEqual(
            "grok",
            json.loads(self.global_config.read_text(encoding="utf-8"))["kanban_agent"],
        )
        self.assertIn(str(rules_entry), output)
        self.assertNotIn("全局规则文件", output)
        self.assertNotIn("~/.local/bin", output)
        self.assertNotIn(".agents/ONEVOKE-AGENTS.md", output)


class LanguageTextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.saved = {
            name: os.environ.get(name)
            for name in ("ONEVOKE_LANG", "LC_ALL", "LC_MESSAGES", "LANG", "ONEVOKE_CONFIG")
        }
        for name in self.saved:
            os.environ.pop(name, None)
        self.temp = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp.name) / "config.json"
        os.environ["ONEVOKE_CONFIG"] = str(self.config_path)
        sys.path.insert(0, str(PROJECT_ROOT / "bin"))
        import onevoke_config

        self.config = onevoke_config
        self._reset_language_state()

    def tearDown(self) -> None:
        self.temp.cleanup()
        sys.path.pop(0)
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _reset_language_state(self) -> None:
        self.config._cli_language_override = None
        self.config._config_language = None

    def _minimal_config(self, **overrides: object) -> dict:
        payload = {
            "schema_version": 1,
            "welcome_complete": True,
            "kanban_agent": "codex",
            "launcher": "tmux",
            "language": "cn",
            "reviewers": {role: "codex" for role in ROLES},
            "memsearch": {"enabled": False},
        }
        payload.update(overrides)
        return payload

    def test_defaults_to_chinese_without_locale(self) -> None:
        self.assertTrue(self.config.language_is_chinese())
        self.assertEqual("中文", self.config.language_text("中文", "English"))

    def test_english_only_when_locale_is_explicit(self) -> None:
        os.environ["LANG"] = "en_US.UTF-8"
        self.assertFalse(self.config.language_is_chinese())
        self.assertEqual("English", self.config.language_text("中文", "English"))

    def test_non_english_locale_stays_chinese(self) -> None:
        os.environ["LANG"] = "de_DE.UTF-8"
        self.assertTrue(self.config.language_is_chinese())

    def test_config_cli_help_defaults_to_chinese_without_locale(self) -> None:
        locale_vars = ("ONEVOKE_LANG", "LC_ALL", "LC_MESSAGES", "LANG")
        env = {key: value for key, value in os.environ.items() if key not in locale_vars}
        env["ONEVOKE_CONFIG"] = str(self.config_path)
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "bin" / "onevoke_config.py"), "--help"],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("用法:", result.stdout)
        self.assertNotIn("usage:", result.stdout.lower())

    def test_config_language_overrides_env(self) -> None:
        self._reset_language_state()
        self.config_path.write_text(
            json.dumps(self._minimal_config(language="en")),
            encoding="utf-8",
        )
        os.environ["ONEVOKE_LANG"] = "zh"
        self.config.bind_effective_language()
        self.assertFalse(self.config.language_is_chinese())

    def test_cli_lang_overrides_config_language(self) -> None:
        self._reset_language_state()
        self.config_path.write_text(
            json.dumps(self._minimal_config(language="en")),
            encoding="utf-8",
        )
        os.environ["ONEVOKE_LANG"] = "en"
        self.config.apply_language_argument(["--lang", "cn"])
        self.config.bind_effective_language()
        self.assertTrue(self.config.language_is_chinese())
        self.assertEqual("1", os.environ.get("ONEVOKE_LANG_CLI"))

    def test_configured_language_subcommand(self) -> None:
        missing = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "bin" / "onevoke_config.py"), "configured-language"],
            env={**os.environ, "ONEVOKE_CONFIG": str(self.config_path)},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, missing.returncode, missing.stderr)
        self.assertEqual("", missing.stdout)

        self.config_path.write_text(
            json.dumps(self._minimal_config(language="en")),
            encoding="utf-8",
        )
        present = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "bin" / "onevoke_config.py"), "configured-language"],
            env={**os.environ, "ONEVOKE_CONFIG": str(self.config_path)},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, present.returncode, present.stderr)
        self.assertEqual("en\n", present.stdout)

        legacy = self._minimal_config()
        legacy.pop("language", None)
        self.config_path.write_text(json.dumps(legacy), encoding="utf-8")
        legacy_result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "bin" / "onevoke_config.py"), "configured-language"],
            env={**os.environ, "ONEVOKE_CONFIG": str(self.config_path)},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, legacy_result.returncode, legacy_result.stderr)
        self.assertEqual("", legacy_result.stdout)

    @unittest.skipUnless(os.name == "posix", "POSIX config mode regression")
    def test_save_config_replaces_public_file_with_private_mode(self) -> None:
        self.config_path.write_text(
            json.dumps(self._minimal_config()),
            encoding="utf-8",
        )
        self.config_path.chmod(0o644)

        self.config.save_config(self._minimal_config(language="en"))

        self.assertEqual(0o600, self.config_path.stat().st_mode & 0o777)
        self.assertEqual("en", self.config.load_config()["language"])

    def test_invalid_language_rejected(self) -> None:
        with self.assertRaises(self.config.ConfigError):
            self.config.validate_config(self._minimal_config(language="fr"))


if __name__ == "__main__":
    unittest.main()
