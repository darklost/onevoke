#!/usr/bin/env python3

import argparse
import fcntl
import hashlib
import io
import json
import os
import pty
import re
import runpy
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import unicodedata
import unittest
from datetime import datetime
from pathlib import Path
from typing import Optional
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 默认测当前工作树; 回落到已安装命令会让改动后的代码看起来仍然通过.
COMMAND = Path(
    os.environ.get("KANBAN_COMMAND", PROJECT_ROOT / "bin" / "kanban")
).resolve()
INSTALLER = PROJECT_ROOT / "install.sh"
RULES_DIR = PROJECT_ROOT / "rules"
RULES = RULES_DIR / "KANBAN-RULES.md"
AGENT_RULES = RULES_DIR / "ONEVOKE-AGENTS.md"
STATES = ("backlog", "todo", "working", "done", "archived", "trash")


class KanbanCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.language = mock.patch.dict(os.environ, {"ONEVOKE_LANG": "zh"})
        self.language.start()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for state in STATES:
            (self.root / state).mkdir()
        self.home = self.root / "home"
        rules_dir = self.home / ".agents"
        rules_dir.mkdir(parents=True)
        (rules_dir / "KANBAN-RULES.md").write_bytes(RULES.read_bytes())
        self.env = os.environ.copy()
        self.env["HOME"] = str(self.home)
        self.env["KANBAN_DIR"] = str(self.root)
        self.env.pop("TMUX", None)
        self.env.pop("TMUX_PANE", None)

    def tearDown(self) -> None:
        self.temp.cleanup()
        self.language.stop()

    def run_command(
        self, *args: str, succeeds: bool = True, input_text: Optional[str] = None
    ) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [sys.executable, str(COMMAND), *args],
            env=self.env,
            text=True,
            input=input_text,
            capture_output=True,
            check=False,
        )
        if succeeds and result.returncode != 0:
            self.fail(result.stderr)
        if not succeeds and result.returncode == 0:
            self.fail(f"command unexpectedly succeeded: {' '.join(args)}")
        return result

    @staticmethod
    def make_ready(path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        replacements = ("实现目标", "产生可验证结果", "满足验收", "无额外范围")
        for replacement in replacements:
            text = text.replace("<填写>", replacement, 1)
        path.write_text(text, encoding="utf-8")

    @staticmethod
    def complete(path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        text = text.replace("- 结果:\n", "- 结果: completed\n", 1)
        text = text.replace("<填写>", "验证通过")
        path.write_text(text, encoding="utf-8")

    def make_todo(self, slug: str) -> tuple[str, Path]:
        task_id = f"{datetime.now().strftime('%Y%m%d')}-{slug}-task"
        self.run_command("new", "chore", slug, f"任务 {slug}")
        task = self.root / "backlog" / f"{task_id}.md"
        self.make_ready(task)
        self.run_command("move", task_id, "todo")
        return task_id, self.root / "todo" / task.name

    def install_fake_launchers(self) -> Path:
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        tmux = fake_bin / "tmux"
        tmux.write_text(
            """#!/bin/sh
if [ "$1" = "display-message" ]; then
    printf '%s\\n' '$42'
    exit 0
fi
if [ "$1" = "has-session" ]; then
    for name in ${KANBAN_TMUX_SESSIONS:-}; do
        [ "$name" = "${3#=}" ] && exit 0
    done
    exit 1
fi
if [ "$1" = "show-options" ]; then
    # 真实 tmux 对未设置的用户选项返回非零并报 invalid option.
    [ -n "${KANBAN_TMUX_PROJECT:-}" ] || exit 1
    printf '%s\\n' "$KANBAN_TMUX_PROJECT"
    exit 0
fi
if [ "$1" = "set-option" ]; then
    printf '%s\\n' "$@" > "$KANBAN_TMUX_LOG.setopt"
    exit 0
fi
printf '%s\\n' "$@" > "$KANBAN_TMUX_LOG"
if [ "${KANBAN_TMUX_FAIL:-}" = "1" ]; then
    printf '%s\\n' 'fake tmux failure' >&2
    exit 1
fi
printf '%s\\n' '@9'
""",
            encoding="utf-8",
        )
        tmux.chmod(0o755)
        for name in ("codex", "claude", "grok"):
            agent = fake_bin / name
            agent.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            agent.chmod(0o755)
        self.env["PATH"] = str(fake_bin) + os.pathsep + self.env.get("PATH", "")
        self.env["TMUX"] = "/tmp/fake-tmux,1,0"
        self.env["TMUX_PANE"] = "%7"
        self.env["KANBAN_TMUX_LOG"] = str(self.root / "tmux.log")
        return fake_bin

    def test_locale_selects_chinese_or_english(self) -> None:
        chinese = self.run_command("--help")
        self.assertIn("本地文件看板", chinese.stdout)
        self.assertIn("--lang {cn,en}", chinese.stdout)
        self.assertNotIn("usage:", chinese.stdout)
        self.assertEqual("通过: 0 个任务\n", self.run_command("check").stdout)

        chinese_error = self.run_command("nope", succeeds=False)
        self.assertIn("参数 命令: 无效选择", chinese_error.stderr)
        self.assertNotIn("argument command", chinese_error.stderr)

        self.assertIn("项目路径", self.run_command("init", "--help").stdout)
        self.assertIn("任务", self.run_command("show", "--help").stdout)
        self.assertIn("标题", self.run_command("new", "--help").stdout)
        option_error = self.run_command("list", "--mobile=foo", succeeds=False)
        self.assertIn("不接受显式参数 'foo'", option_error.stderr)
        self.assertNotIn("ignored explicit argument", option_error.stderr)

        self.env["ONEVOKE_LANG"] = "en"
        english = self.run_command("--help")
        self.assertIn("Local file kanban board", english.stdout)
        self.assertNotIn("本地文件看板", english.stdout)

        forced_chinese = self.run_command("--lang", "cn", "--help")
        self.assertIn("本地文件看板", forced_chinese.stdout)
        self.env["ONEVOKE_LANG"] = "zh"
        forced_english = self.run_command("--lang", "en", "--help")
        self.assertIn("Local file kanban board", forced_english.stdout)
        invalid = self.run_command("--lang", "fr", "--help", succeeds=False)
        self.assertIn("无效选择", invalid.stderr)
        missing = self.run_command("--lang", succeeds=False)
        self.assertIn("需要一个参数", missing.stderr)

        self.env["ONEVOKE_LANG"] = "en"
        rejected = self.run_command(
            "new", "chore", "Bad-Slug", "title", succeeds=False
        )
        self.assertIn("slug may contain only lowercase ASCII", rejected.stderr)

        checked = self.run_command("check")
        self.assertEqual("ok: 0 tasks\n", checked.stdout)

    def write_onevoke_config(
        self,
        agent: str,
        launcher: str,
        *,
        welcome_complete: bool = True,
        models: Optional[dict] = None,
    ) -> None:
        config = self.home / ".config" / "onevoke" / "config.json"
        config.parent.mkdir(parents=True)
        payload = {
            "schema_version": 1,
            "welcome_complete": welcome_complete,
            "kanban_agent": agent,
            "launcher": launcher,
            "reviewers": {
                "PM": "codex",
                "CSA": "codex",
                "Hacker": "codex",
                "QA": "codex",
            },
            "memsearch": {"enabled": False},
        }
        if models is not None:
            payload["models"] = models
        config.write_text(json.dumps(payload), encoding="utf-8")

    def test_small_and_large_lifecycle(self) -> None:
        today = datetime.now().strftime("%Y%m%d")
        small_id = f"{today}-small-fix-task"
        self.run_command("new", "bug", "small-fix", "修复小问题")
        small = self.root / "backlog" / f"{small_id}.md"
        self.run_command("move", small_id, "todo", succeeds=False)
        self.make_ready(small)
        self.run_command("move", small_id, "todo")
        self.run_command("move", small_id, "working")
        small = self.root / "working" / f"{small_id}.md"
        self.complete(small)
        self.run_command("move", small_id, "done")

        large_id = f"{today}-large-feature-task"
        self.run_command("new", "--large", "feature", "large-feature", "大型功能")
        spec = self.root / "backlog" / large_id / "spec.md"
        self.make_ready(spec)
        self.run_command("move", large_id, "todo")
        self.run_command("move", large_id, "working")
        spec = self.root / "working" / large_id / "spec.md"
        self.complete(spec)
        spec.write_text(
            spec.read_text(encoding="utf-8").replace("- 完成时间:\n", "", 1),
            encoding="utf-8",
        )
        self.run_command("move", large_id, "done", succeeds=False)
        (spec.parent / "report.md").write_text("# 完成报告\n\n验证通过.\n", encoding="utf-8")
        self.run_command("move", large_id, "done")

        listing = self.run_command("list", "done").stdout
        self.assertIn(small_id, listing)
        self.assertIn(large_id, listing)
        self.assertRegex(listing, r"done\s+small\s+\d{4}-\d{2}-\d{2} \d{2}:\d{2}")
        completed = self.root / "done" / small.name
        self.assertRegex(
            completed.read_text(encoding="utf-8"),
            r"(?m)^- 完成时间: \d{4}-\d{2}-\d{2} \d{2}:\d{2}$",
        )
        self.assertIn(
            "- 完成时间: ",
            (self.root / "done" / large_id / "spec.md").read_text(encoding="utf-8"),
        )
        self.assertEqual("通过: 2 个任务\n", self.run_command("check").stdout)

    def test_new_templates_include_optional_task_group_metadata(self) -> None:
        today = datetime.now().strftime("%Y%m%d")
        self.run_command("new", "chore", "small-group-field", "小任务组字段")
        self.run_command(
            "new", "--large", "chore", "large-group-field", "大任务组字段"
        )

        small = self.root / "backlog" / f"{today}-small-group-field-task.md"
        large = self.root / "backlog" / f"{today}-large-group-field-task" / "spec.md"
        for document in (small, large):
            text = document.read_text(encoding="utf-8")
            self.assertEqual(1, text.count("- 任务组:\n"))
            self.assertIn("- 类型: Chore\n- 任务组:\n- 创建时间:", text)

    def test_pick_moves_only_ready_backlog_task_to_todo(self) -> None:
        task_id = f"{datetime.now().strftime('%Y%m%d')}-pick-task"
        self.run_command("new", "chore", "pick", "挑选任务")
        task = self.root / "backlog" / f"{task_id}.md"

        result = self.run_command("pick", task_id, succeeds=False)
        self.assertIn("任务未满足 todo 条件", result.stderr)
        self.make_ready(task)
        self.run_command("pick", task_id)

        self.assertTrue((self.root / "todo" / task.name).exists())
        result = self.run_command("pick", task_id, succeeds=False)
        self.assertIn("不允许迁移: todo -> todo", result.stderr)

    def test_pick_without_task_prompts_for_backlog_selection(self) -> None:
        first_id = f"{datetime.now().strftime('%Y%m%d')}-alpha-pick-task"
        second_id = f"{datetime.now().strftime('%Y%m%d')}-beta-pick-task"
        self.run_command("new", "chore", "alpha-pick", "第一个任务")
        self.run_command("new", "chore", "beta-pick", "第二个任务")
        first = self.root / "backlog" / f"{first_id}.md"
        second = self.root / "backlog" / f"{second_id}.md"
        self.make_ready(first)
        self.make_ready(second)

        result = self.run_command("pick", input_text="2\n")

        self.assertIn(f"1. {first_id}", result.stdout)
        self.assertIn(f"2. {second_id}", result.stdout)
        self.assertTrue(first.exists())
        self.assertTrue((self.root / "todo" / second.name).exists())

    def test_pick_without_task_rejects_empty_backlog(self) -> None:
        result = self.run_command("pick", succeeds=False)

        self.assertIn("backlog 中没有任务", result.stderr)

    def test_done_metadata_error_keeps_task_in_working(self) -> None:
        task_id, task = self.make_todo("bad-done-metadata")
        self.run_command("move", task_id, "working")
        task = self.root / "working" / task.name
        self.complete(task)
        text = task.read_text(encoding="utf-8").replace(
            "- 完成时间:\n", "- 完成时间:\n- 完成时间:\n", 1
        )
        task.write_text(text, encoding="utf-8")

        result = self.run_command("move", task_id, "done", succeeds=False)

        self.assertIn("缺少唯一元数据字段: 完成时间", result.stderr)
        self.assertTrue(task.exists())
        self.assertFalse((self.root / "done" / task.name).exists())

    def test_done_write_error_restores_working_task_unchanged(self) -> None:
        task_id, task = self.make_todo("done-write-error")
        self.run_command("move", task_id, "working")
        task = self.root / "working" / task.name
        self.complete(task)
        original = task.read_text(encoding="utf-8")
        sys.path.insert(0, str(COMMAND.parent))
        try:
            kanban = runpy.run_path(str(COMMAND), run_name="kanban_test")
        finally:
            sys.path.pop(0)
        entry = kanban["Entry"](task_id, "working", task, task, "small")

        with mock.patch.object(kanban["os"], "replace", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(kanban["KanbanError"], "记录完成时间失败"):
                kanban["move_entry"](entry, self.root, "done")

        self.assertEqual(original, task.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "done" / task.name).exists())

    def test_list_formats_aligned_table(self) -> None:
        task_id = f"{datetime.now().strftime('%Y%m%d')}-list-table-task"
        self.run_command("new", "chore", "list-table", "表格输出")
        large_id = f"{datetime.now().strftime('%Y%m%d')}-list-large-task"
        self.run_command("new", "--large", "chore", "list-large", "大型表格输出")
        self.env.pop("NO_COLOR", None)
        self.env["CLICOLOR_FORCE"] = "1"
        self.env["COLORFGBG"] = "15;0"

        output = self.run_command("list", "backlog").stdout
        plain = re.sub(r"\033\[[0-9;]*m", "", output)

        lines = plain.splitlines()
        self.assertEqual("状态     规模   时间  任务 ID / 标题", lines[0])
        self.assertIn(f"backlog  small  -     {task_id}  表格输出", plain)
        self.assertIn(f"backlog  large  -     {large_id}  大型表格输出", plain)
        self.assertIn("\033[90mbacklog", output)
        self.assertIn("\033[90msmall", output)
        self.assertIn("\033[1;95mlarge", output)
        self.assertIn(f"\033[96m{task_id}", output)
        self.assertIn("\033[95m表格输出", output)
        self.assertNotIn("\t", output)

        def display_width(text: str) -> int:
            return sum(
                0 if unicodedata.combining(char) else
                2 if unicodedata.east_asian_width(char) in "WF" else 1
                for char in text
            )

        row = next(line for line in lines if task_id in line)
        for heading, value in (("规模", "small"), ("时间", "-"), ("任务 ID", task_id)):
            self.assertEqual(
                display_width(lines[0][: lines[0].index(heading)]),
                display_width(row[: row.index(value)]),
            )

    def test_list_mobile_formats_each_task_as_vertical_block(self) -> None:
        task_id = f"{datetime.now().strftime('%Y%m%d')}-list-mobile-task"
        self.run_command("new", "chore", "list-mobile", "手机竖屏输出")

        output = self.run_command("list", "--mobile", "backlog").stdout

        self.assertEqual(
            ["backlog  small  -", task_id, "手机竖屏输出"],
            output.splitlines(),
        )

    def test_list_accepts_empty_state(self) -> None:
        self.assertEqual("", self.run_command("list", "--mobile", "done").stdout)
        self.assertIn("状态", self.run_command("list", "done").stdout)

    def test_list_uses_document_mtime_for_legacy_done_task(self) -> None:
        task_id = f"{datetime.now().strftime('%Y%m%d')}-legacy-done-task"
        task = self.root / "done" / f"{task_id}.md"
        task.write_text("# 历史任务\n", encoding="utf-8")
        modified = datetime(2024, 1, 2, 3, 4).timestamp()
        os.utime(task, (modified, modified))

        output = self.run_command("list", "done").stdout

        self.assertIn("2024-01-02 03:04", output)

    def test_list_groups_states_and_sorts_each_group_by_time_descending(self) -> None:
        today = datetime.now().strftime("%Y%m%d")
        tasks = (
            ("backlog", "backlog-old", "", ""),
            ("backlog", "backlog-new", "", ""),
            ("working", "working-old", "2024-01-01 10:00", ""),
            ("working", "working-new", "2024-01-02 10:00", ""),
            ("done", "done-old", "", "2024-01-03 10:00"),
            ("done", "done-new", "", "2024-01-04 10:00"),
        )
        for state, slug, started, completed in tasks:
            (self.root / state / f"{today}-{slug}-task.md").write_text(
                f"# {slug}\n- 开始时间: {started}\n- 完成时间: {completed}\n",
                encoding="utf-8",
            )

        output = self.run_command("list").stdout

        self.assertEqual(
            [
                f"{today}-backlog-old-task",
                f"{today}-backlog-new-task",
                f"{today}-working-new-task",
                f"{today}-working-old-task",
                f"{today}-done-new-task",
                f"{today}-done-old-task",
            ],
            re.findall(rf"{today}-[a-z-]+-task", output),
        )

    def test_list_adapts_all_colors_to_background(self) -> None:
        today = datetime.now().strftime("%Y%m%d")
        for state in STATES:
            (self.root / state / f"{today}-list-{state}-task.md").write_text(
                f"# 状态 {state}\n", encoding="utf-8"
            )
        self.env.pop("NO_COLOR", None)
        self.env["CLICOLOR_FORCE"] = "1"

        self.env["COLORFGBG"] = "15;0"
        dark = self.run_command("list").stdout
        self.env["COLORFGBG"] = "0;15"
        light = self.run_command("list").stdout

        for state, code in zip(STATES, ("90", "93", "96", "92", "94", "91")):
            self.assertIn(f"\033[{code}m{state}", dark)
        for state, code in zip(STATES, ("30", "33", "34", "32", "35", "31")):
            self.assertIn(f"\033[{code}m{state}", light)
        self.assertIn("\033[90msmall", dark)
        self.assertIn(f"\033[96m{today}", dark)
        self.assertIn("\033[95m状态", dark)
        self.assertIn("\033[30msmall", light)
        self.assertIn(f"\033[34m{today}", light)
        self.assertIn("\033[35m状态", light)

    def test_start_moves_task_and_launches_agent_window(self) -> None:
        task_id, task = self.make_todo("start-direct")
        fake_bin = self.install_fake_launchers()

        result = self.run_command("start", task_id)

        self.assertIn(f"已启动: {task_id}", result.stdout)
        started = self.root / "working" / task.name
        text = started.read_text(encoding="utf-8")
        self.assertIn("- 负责人: codex\n", text)
        started_at = re.search(
            r"(?m)^- 开始时间: (\d{4}-\d{2}-\d{2} \d{2}:\d{2})$", text
        )
        self.assertIsNotNone(started_at)
        self.assertIn(started_at.group(1), self.run_command("list", "working").stdout)
        tmux_args = (self.root / "tmux.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual("new-window", tmux_args[0])
        self.assertEqual("$42:", tmux_args[tmux_args.index("-t") + 1])
        self.assertEqual(str(self.root.resolve().parent), tmux_args[tmux_args.index("-c") + 1])
        self.assertEqual("kb-任务-start-direct", tmux_args[tmux_args.index("-n") + 1])
        self.assertIn(str(fake_bin / "codex"), tmux_args[-1])
        self.assertIn("--model gpt-5.6-sol", tmux_args[-1])
        self.assertIn('model_reasoning_effort="medium"', tmux_args[-1])
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", tmux_args[-1])
        self.assertIn(task_id, tmux_args[-1])

    def test_start_window_name_folds_title_and_truncates(self) -> None:
        task_id, task = self.make_todo("window-name")
        text = task.read_text(encoding="utf-8")
        task.write_text(
            text.replace("# 任务 window-name", f"# 修复 登录  重试 {'长' * 60}", 1),
            encoding="utf-8",
        )
        self.install_fake_launchers()

        self.run_command("start", task_id)

        tmux_args = (self.root / "tmux.log").read_text(encoding="utf-8").splitlines()
        name = tmux_args[tmux_args.index("-n") + 1]
        self.assertEqual(f"kb-修复-登录-重试-{'长' * 60}"[:50], name)
        self.assertEqual(50, len(name))

    def test_start_window_name_falls_back_to_slug_without_title(self) -> None:
        task_id, task = self.make_todo("no-title")
        text = task.read_text(encoding="utf-8")
        task.write_text(text.replace("# 任务 no-title\n", "", 1), encoding="utf-8")
        self.install_fake_launchers()

        self.run_command("start", task_id)

        tmux_args = (self.root / "tmux.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual("kb-no-title", tmux_args[tmux_args.index("-n") + 1])

    def test_start_without_task_prompts_for_todo_selection(self) -> None:
        first_id, first = self.make_todo("alpha")
        second_id, second = self.make_todo("beta")
        self.install_fake_launchers()

        result = self.run_command("start", "--agent", "claude", input_text="2\n")

        self.assertIn(f"1. {first_id}", result.stdout)
        self.assertIn(f"2. {second_id}", result.stdout)
        self.assertTrue(first.exists())
        self.assertTrue((self.root / "working" / second.name).exists())
        self.assertIn("Agent=claude", result.stdout)
        command = (self.root / "tmux.log").read_text(encoding="utf-8").splitlines()[-1]
        self.assertIn("--model opus --effort medium", command)
        self.assertIn("--dangerously-skip-permissions", command)

    def test_start_with_grok_launches_bypass_permission_session(self) -> None:
        task_id, task = self.make_todo("start-grok")
        fake_bin = self.install_fake_launchers()

        result = self.run_command("start", "--agent", "grok", task_id)

        self.assertIn("Agent=grok", result.stdout)
        started = self.root / "working" / task.name
        self.assertIn("- 负责人: grok\n", started.read_text(encoding="utf-8"))
        command = (self.root / "tmux.log").read_text(encoding="utf-8").splitlines()[-1]
        self.assertIn(str(fake_bin / "grok"), command)
        self.assertNotIn("--model", command)
        self.assertIn("--effort high", command)
        self.assertNotIn("--effort xhigh", command)
        self.assertIn("--permission-mode bypassPermissions", command)
        self.assertIn(task_id, command)

    def test_start_uses_configured_models_and_efforts(self) -> None:
        task_id, _ = self.make_todo("custom-model")
        self.install_fake_launchers()
        self.write_onevoke_config(
            "codex",
            "tmux",
            models={"kanban": {"codex": {"model": "gpt-7", "small_effort": "low"}}},
        )

        self.run_command("start", task_id)

        command = (self.root / "tmux.log").read_text(encoding="utf-8").splitlines()[-1]
        self.assertIn("--model gpt-7", command)
        self.assertIn('model_reasoning_effort="low"', command)
        self.assertNotIn("gpt-5.6-sol", command)

    def test_start_omits_model_argument_when_config_model_is_empty(self) -> None:
        task_id, _ = self.make_todo("empty-model")
        self.install_fake_launchers()
        self.write_onevoke_config(
            "claude",
            "tmux",
            models={"kanban": {"claude": {"model": ""}}},
        )

        self.run_command("start", task_id)

        command = (self.root / "tmux.log").read_text(encoding="utf-8").splitlines()[-1]
        self.assertNotIn("--model", command)
        self.assertIn("--effort medium", command)
        self.assertIn("--dangerously-skip-permissions", command)

    def test_start_uses_the_configured_default_agent(self) -> None:
        task_id, _ = self.make_todo("configured-agent")
        fake_bin = self.install_fake_launchers()
        self.write_onevoke_config("grok", "tmux")

        result = self.run_command("start", task_id)

        self.assertIn("Agent=grok", result.stdout)
        command = (self.root / "tmux.log").read_text(encoding="utf-8").splitlines()[-1]
        self.assertIn(str(fake_bin / "grok"), command)
        self.assertIn("--permission-mode bypassPermissions", command)

    def test_start_ignores_unfinished_welcome_selections(self) -> None:
        task_id, _ = self.make_todo("unfinished-config")
        fake_bin = self.install_fake_launchers()
        self.write_onevoke_config("grok", "foreground", welcome_complete=False)

        result = self.run_command("start", task_id)

        self.assertIn("Agent=codex", result.stdout)
        command = (self.root / "tmux.log").read_text(encoding="utf-8").splitlines()[-1]
        self.assertIn(str(fake_bin / "codex"), command)

    def test_foreground_launcher_rejects_a_noninteractive_terminal(self) -> None:
        task_id, task = self.make_todo("foreground-no-tty")
        self.install_fake_launchers()
        self.write_onevoke_config("codex", "foreground")

        result = self.run_command("start", task_id, succeeds=False)

        self.assertIn("前台启动模式需要交互终端", result.stderr)
        self.assertIn("stdin/stdout/stderr 均为 tty", result.stderr)
        self.assertIn("--launcher tmux", result.stderr)
        self.assertTrue(task.exists())
        self.assertFalse((self.root / "working" / task.name).exists())

    def test_foreground_launcher_runs_the_agent_in_the_project(self) -> None:
        task_id, task = self.make_todo("foreground")
        fake_bin = self.install_fake_launchers()
        foreground_log = self.root / "foreground.log"
        (fake_bin / "claude").write_text(
            "#!/bin/sh\npwd > \"$KANBAN_FOREGROUND_LOG\"\n",
            encoding="utf-8",
        )
        (fake_bin / "claude").chmod(0o755)
        self.env["KANBAN_FOREGROUND_LOG"] = str(foreground_log)
        self.write_onevoke_config("claude", "foreground")
        sys.path.insert(0, str(COMMAND.parent))
        try:
            kanban = runpy.run_path(str(COMMAND), run_name="kanban_foreground_test")
        finally:
            sys.path.pop(0)
        args = argparse.Namespace(task=task_id, agent=None, launcher=None)

        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch.object(kanban["sys"].stdin, "isatty", return_value=True):
                with mock.patch.object(kanban["sys"].stdout, "isatty", return_value=True):
                    with mock.patch.object(
                        kanban["sys"].stderr, "isatty", return_value=True
                    ):
                        kanban["command_start"](args, self.root)

        started = self.root / "working" / task.name
        self.assertTrue(started.exists())
        self.assertIn("- 负责人: claude", started.read_text(encoding="utf-8"))
        self.assertEqual(str(self.root.parent), foreground_log.read_text().strip())

    def test_start_launcher_option_overrides_machine_config(self) -> None:
        task_id, task = self.make_todo("launcher-override")
        fake_bin = self.install_fake_launchers()
        foreground_log = self.root / "override.log"
        (fake_bin / "codex").write_text(
            "#!/bin/sh\npwd > \"$KANBAN_FOREGROUND_LOG\"\n", encoding="utf-8"
        )
        (fake_bin / "codex").chmod(0o755)
        self.env["KANBAN_FOREGROUND_LOG"] = str(foreground_log)
        self.write_onevoke_config("codex", "tmux")
        sys.path.insert(0, str(COMMAND.parent))
        try:
            kanban = runpy.run_path(str(COMMAND), run_name="kanban_launcher_override_test")
        finally:
            sys.path.pop(0)
        args = argparse.Namespace(task=task_id, agent=None, launcher="foreground")

        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch.object(kanban["sys"].stdin, "isatty", return_value=True):
                with mock.patch.object(kanban["sys"].stdout, "isatty", return_value=True):
                    with mock.patch.object(
                        kanban["sys"].stderr, "isatty", return_value=True
                    ):
                        kanban["command_start"](args, self.root)

        self.assertTrue((self.root / "working" / task.name).exists())
        self.assertEqual(str(self.root.parent), foreground_log.read_text().strip())

    def test_foreground_spawn_failure_rolls_back_before_started_output(self) -> None:
        task_id, task = self.make_todo("spawn-failure")
        self.install_fake_launchers()
        self.write_onevoke_config("codex", "foreground")
        original = task.read_text(encoding="utf-8")
        sys.path.insert(0, str(COMMAND.parent))
        try:
            kanban = runpy.run_path(str(COMMAND), run_name="kanban_spawn_failure_test")
        finally:
            sys.path.pop(0)
        args = argparse.Namespace(task=task_id, agent=None, launcher=None)
        output = io.StringIO()

        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch.object(kanban["sys"].stdin, "isatty", return_value=True):
                with mock.patch.object(kanban["sys"], "stdout", output):
                    with mock.patch.object(output, "isatty", return_value=True):
                        with mock.patch.object(
                            kanban["sys"].stderr, "isatty", return_value=True
                        ):
                            with mock.patch.object(
                                kanban["subprocess"],
                                "Popen",
                                side_effect=OSError("Exec format error"),
                            ):
                                with self.assertRaisesRegex(
                                    kanban["KanbanError"], "启动 Agent 失败"
                                ):
                                    kanban["command_start"](args, self.root)

        self.assertEqual("", output.getvalue())
        self.assertEqual(original, task.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "working" / task.name).exists())

    def test_start_uses_high_effort_for_large_tasks(self) -> None:
        self.install_fake_launchers()
        for agent, expected in (
            ("codex", '--model gpt-5.6-sol --config \'model_reasoning_effort="high"\''),
            ("claude", "--model opus --effort high"),
            ("grok", "--effort xhigh --permission-mode bypassPermissions"),
        ):
            slug = f"large-{agent}"
            task_id = f"{datetime.now().strftime('%Y%m%d')}-{slug}-task"
            self.run_command("new", "--large", "chore", slug, f"大型任务 {agent}")
            spec = self.root / "backlog" / task_id / "spec.md"
            self.make_ready(spec)
            self.run_command("pick", task_id)

            self.run_command("start", "--agent", agent, task_id)

            command = (self.root / "tmux.log").read_text(encoding="utf-8").splitlines()[-1]
            self.assertIn(expected, command)

    def test_start_failure_restores_todo_and_metadata(self) -> None:
        task_id, task = self.make_todo("rollback")
        original = task.read_text(encoding="utf-8")
        self.install_fake_launchers()
        self.env["KANBAN_TMUX_FAIL"] = "1"

        result = self.run_command("start", task_id, succeeds=False)

        self.assertIn("tmux new-window 失败", result.stderr)
        self.assertEqual(original, task.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "working" / task.name).exists())

    def test_start_outside_tmux_does_not_claim_task(self) -> None:
        task_id, task = self.make_todo("no-tmux")
        self.install_fake_launchers()
        self.env.pop("TMUX")
        self.env.pop("TMUX_PANE")

        result = self.run_command("start", task_id, succeeds=False)

        self.assertIn("当前不在 tmux session", result.stderr)
        self.assertIn("tmux new -A -s onevoke", result.stderr)
        self.assertTrue(task.exists())

    def project_session(self, suffix: str = "") -> str:
        project = self.root.resolve().parent
        digest = hashlib.sha256(str(project).encode("utf-8")).hexdigest()[:8]
        label = project.name[:30]
        return f"kb-{label}-{digest}{suffix}" if label else f"kb-{digest}{suffix}"

    def tmux_arguments(self) -> list[str]:
        return (self.root / "tmux.log").read_text(encoding="utf-8").splitlines()

    def test_tmux_session_launcher_creates_the_project_session(self) -> None:
        task_id, task = self.make_todo("session-create")
        fake_bin = self.install_fake_launchers()
        # 项目专属 session 自己建, 因此不在 tmux 里也能启动.
        self.env.pop("TMUX")
        self.env.pop("TMUX_PANE")

        result = self.run_command("start", "--launcher", "tmux-session", task_id)

        session = self.project_session()
        project = str(self.root.resolve().parent)
        self.assertIn(f"已启动: {task_id}", result.stdout)
        self.assertIn(f"会话={session}", result.stdout)
        self.assertIn(f"tmux attach -t {session}", result.stdout)
        arguments = self.tmux_arguments()
        self.assertEqual("new-session", arguments[0])
        self.assertIn("-d", arguments)
        self.assertEqual(session, arguments[arguments.index("-s") + 1])
        self.assertEqual(project, arguments[arguments.index("-c") + 1])
        self.assertEqual("kb-任务-session-create", arguments[arguments.index("-n") + 1])
        self.assertIn(str(fake_bin / "codex"), arguments[-1])
        self.assertIn(task_id, arguments[-1])
        self.assertEqual(
            ["set-option", "-t", session, "@onevoke_project", project],
            (self.root / "tmux.log.setopt").read_text(encoding="utf-8").splitlines(),
        )
        self.assertTrue((self.root / "working" / task.name).exists())

    def test_tmux_session_launcher_reuses_the_project_session(self) -> None:
        task_id, _ = self.make_todo("session-reuse")
        self.install_fake_launchers()
        session = self.project_session()
        self.env["KANBAN_TMUX_SESSIONS"] = session
        self.env["KANBAN_TMUX_PROJECT"] = str(self.root.resolve().parent)

        result = self.run_command("start", "--launcher", "tmux-session", task_id)

        arguments = self.tmux_arguments()
        self.assertEqual("new-window", arguments[0])
        self.assertEqual(f"{session}:", arguments[arguments.index("-t") + 1])
        self.assertEqual("kb-任务-session-reuse", arguments[arguments.index("-n") + 1])
        self.assertFalse((self.root / "tmux.log.setopt").exists())
        self.assertIn(f"会话={session}", result.stdout)
        # 已在 tmux 里时给切换命令而不是 attach.
        self.assertIn(f"tmux switch-client -t {session}", result.stdout)

    def test_tmux_session_launcher_reuses_an_unmarked_session(self) -> None:
        task_id, _ = self.make_todo("session-unmarked")
        self.install_fake_launchers()
        session = self.project_session()
        self.env["KANBAN_TMUX_SESSIONS"] = session

        self.run_command("start", "--launcher", "tmux-session", task_id)

        arguments = self.tmux_arguments()
        self.assertEqual("new-window", arguments[0])
        self.assertEqual(f"{session}:", arguments[arguments.index("-t") + 1])

    def test_tmux_session_launcher_avoids_another_projects_session(self) -> None:
        task_id, _ = self.make_todo("session-conflict")
        self.install_fake_launchers()
        self.env["KANBAN_TMUX_SESSIONS"] = self.project_session()
        self.env["KANBAN_TMUX_PROJECT"] = "/somewhere/else"

        result = self.run_command("start", "--launcher", "tmux-session", task_id)

        arguments = self.tmux_arguments()
        self.assertEqual("new-session", arguments[0])
        self.assertEqual(self.project_session("-2"), arguments[arguments.index("-s") + 1])
        self.assertIn(f"会话={self.project_session('-2')}", result.stdout)

    def test_tmux_session_launcher_reads_the_machine_config(self) -> None:
        task_id, _ = self.make_todo("session-config")
        self.install_fake_launchers()
        self.write_onevoke_config("codex", "tmux-session")
        self.env.pop("TMUX")
        self.env.pop("TMUX_PANE")

        self.run_command("start", task_id)

        self.assertEqual("new-session", self.tmux_arguments()[0])

    def test_tmux_session_launcher_failure_restores_todo(self) -> None:
        task_id, task = self.make_todo("session-rollback")
        original = task.read_text(encoding="utf-8")
        self.install_fake_launchers()
        self.env["KANBAN_TMUX_FAIL"] = "1"

        result = self.run_command(
            "start", "--launcher", "tmux-session", task_id, succeeds=False
        )

        self.assertIn("tmux new-session 失败", result.stderr)
        self.assertEqual(original, task.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "working" / task.name).exists())

    def test_rejects_invalid_transition_and_duplicate_id(self) -> None:
        task_id = f"{datetime.now().strftime('%Y%m%d')}-duplicate-task"
        self.run_command("new", "chore", "duplicate", "重复检测")
        self.run_command("move", task_id, "working", succeeds=False)
        (self.root / "todo" / task_id).mkdir()
        (self.root / "todo" / task_id / "spec.md").write_text("# 重复\n", encoding="utf-8")
        result = self.run_command("check", succeeds=False)
        self.assertIn("重复任务 ID", result.stderr)

    def test_archive_and_trash_require_results(self) -> None:
        task_id = f"{datetime.now().strftime('%Y%m%d')}-retired-task"
        self.run_command("new", "research", "retired", "终止研究")
        task = self.root / "backlog" / f"{task_id}.md"
        self.run_command("move", task_id, "archived", succeeds=False)
        text = task.read_text(encoding="utf-8").replace(
            "- 结果:\n", "- 结果: cancelled\n", 1
        )
        task.write_text(text, encoding="utf-8")
        self.run_command("move", task_id, "archived")
        task = self.root / "archived" / f"{task_id}.md"
        self.run_command("move", task_id, "trash", succeeds=False)
        text = task.read_text(encoding="utf-8").replace(
            "- 结果: cancelled\n", "- 结果: trashed\n", 1
        )
        task.write_text(text, encoding="utf-8")
        self.run_command("move", task_id, "trash")

    def test_discovers_non_git_project_board_from_child_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            board = project / "kanban"
            nested = project / "src" / "nested"
            nested.mkdir(parents=True)
            for state in STATES:
                (board / state).mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env.pop("KANBAN_DIR", None)
            result = subprocess.run(
                [sys.executable, str(COMMAND), "check"],
                cwd=nested,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("通过: 0 个任务\n", result.stdout)

    def test_init_non_git_project_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            project.mkdir()
            env = os.environ.copy()
            env.pop("KANBAN_DIR", None)
            for _ in range(2):
                result = subprocess.run(
                    [sys.executable, str(COMMAND), "init", str(project)],
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("规则: ", result.stdout)
            for state in STATES:
                self.assertTrue((project / "kanban" / state).is_dir())
            self.assertFalse((project / ".git").exists())

    def test_init_git_project_adds_local_exclude_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            project.mkdir()
            subprocess.run(
                ["git", "init", "-q", str(project)],
                text=True,
                capture_output=True,
                check=True,
            )
            env = os.environ.copy()
            env.pop("KANBAN_DIR", None)
            for _ in range(2):
                result = subprocess.run(
                    [sys.executable, str(COMMAND), "init", str(project)],
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(0, result.returncode, result.stderr)
            exclude = (project / ".git" / "info" / "exclude").read_text(encoding="utf-8")
            self.assertEqual(1, exclude.splitlines().count("/kanban/"))

    def test_rules_are_global_and_do_not_require_a_board(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            env = self.env.copy()
            env.pop("KANBAN_DIR", None)
            result = subprocess.run(
                [sys.executable, str(COMMAND), "rules"],
                cwd=temp,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue(result.stdout.startswith("# 全局文件看板规则\n"))

    def test_stray_file_does_not_break_the_whole_board(self) -> None:
        task_id, _ = self.make_todo("healthy")
        (self.root / "backlog" / "notes.md").write_text("随手记", encoding="utf-8")

        listing = self.run_command("list")

        self.assertIn(task_id, listing.stdout)
        self.assertIn("无效入口", listing.stderr)
        self.assertIn("# 任务 healthy", self.run_command("show", task_id).stdout)
        self.run_command("move", task_id, "working")

    def test_check_reports_invalid_entries_and_fails(self) -> None:
        (self.root / "backlog" / "notes.md").write_text("随手记", encoding="utf-8")

        result = self.run_command("check", succeeds=False)

        self.assertEqual(1, result.returncode)
        self.assertIn("notes.md", result.stderr)
        self.assertIn("已检查: 0 个有效, 1 个无效", result.stdout)
        self.assertNotIn("通过:", result.stdout)

    def test_check_passes_on_a_clean_board(self) -> None:
        self.make_todo("clean")

        result = self.run_command("check")

        self.assertEqual("通过: 1 个任务\n", result.stdout)

    def test_duplicate_task_id_blocks_only_that_task(self) -> None:
        duplicated, todo_path = self.make_todo("dup")
        healthy, _ = self.make_todo("fine")
        (self.root / "working" / todo_path.name).write_bytes(todo_path.read_bytes())

        listing = self.run_command("list")
        blocked = self.run_command("show", duplicated, succeeds=False)

        self.assertNotIn(duplicated, listing.stdout)
        self.assertIn(healthy, listing.stdout)
        self.assertIn("重复任务 ID", blocked.stderr)
        self.run_command("move", healthy, "working")

    def test_large_task_without_spec_blocks_only_that_task(self) -> None:
        healthy, _ = self.make_todo("fine")
        broken = f"{datetime.now().strftime('%Y%m%d')}-broken-task"
        (self.root / "backlog" / broken).mkdir()

        listing = self.run_command("list")
        blocked = self.run_command("show", broken, succeeds=False)

        self.assertIn(healthy, listing.stdout)
        self.assertIn("大任务缺少 spec.md", blocked.stderr)

    def test_symlink_spec_is_rejected_and_outside_bytes_stay_intact(self) -> None:
        healthy, _ = self.make_todo("fine")
        outside = self.root.parent / "outside-secret.md"
        secret = "do-not-touch-external-target\n"
        outside.write_text(secret, encoding="utf-8")
        task_id = f"{datetime.now().strftime('%Y%m%d')}-symlink-spec-task"
        task_dir = self.root / "todo" / task_id
        task_dir.mkdir()
        (task_dir / "spec.md").symlink_to(outside)

        check = self.run_command("check", succeeds=False)
        show = self.run_command("show", task_id, succeeds=False)
        start = self.run_command("start", task_id, succeeds=False)

        self.assertIn("spec.md 不得是符号链接", check.stderr)
        self.assertIn("已检查:", check.stdout)
        self.assertIn("spec.md 不得是符号链接", show.stderr)
        self.assertIn("spec.md 不得是符号链接", start.stderr)
        self.assertIn(healthy, self.run_command("list").stdout)
        self.assertEqual(secret, outside.read_text(encoding="utf-8"))

    def test_document_replaced_with_symlink_is_rejected_on_read(self) -> None:
        # 大任务: 入口目录合法, 仅 spec.md 在 scan 后被换成看板外软链.
        task_id = f"{datetime.now().strftime('%Y%m%d')}-swap-link-task"
        task_dir = self.root / "todo" / task_id
        task_dir.mkdir()
        spec = task_dir / "spec.md"
        contract = """# 任务 swap-link

- 类型: Bug
- 创建时间: 2026-08-11 00:00
- 负责人:
- 开始时间:
- 完成时间:
- 任务分支:
- 结果:

## 任务目标

目标

## 用户决策

N/A

## 预期成果

成果

## 验收条件

- [ ] 条件

## 威胁模型

N/A

## 不在本轮范围

- 无

## 讨论与决策

N/A

## 实施与验证

N/A

## 完成总结

"""
        spec.write_text(contract, encoding="utf-8")
        outside = self.root.parent / "swap-secret.md"
        outside.write_text("external\n", encoding="utf-8")
        self.assertIn("# 任务 swap-link", self.run_command("show", task_id).stdout)
        spec.unlink()
        spec.symlink_to(outside)

        show = self.run_command("show", task_id, succeeds=False)
        self.assertTrue(
            "不得是符号链接" in show.stderr or "符号链接" in show.stderr,
            show.stderr,
        )
        self.assertEqual("external\n", outside.read_text(encoding="utf-8"))

    def test_write_text_atomic_rejects_document_symlink(self) -> None:
        task_id, task = self.make_todo("write-link")
        outside = self.root / "write-secret.md"
        outside.write_text("keep-me\n", encoding="utf-8")
        self.run_command("move", task_id, "working")
        working = self.root / "working" / task.name
        working.unlink()
        working.symlink_to(outside)

        import runpy
        import sys as _sys

        _sys.path.insert(0, str(COMMAND.parent))
        try:
            kanban = runpy.run_path(str(COMMAND), run_name="kanban_write_link_test")
        finally:
            _sys.path.pop(0)
        entry = kanban["Entry"](
            task_id, "working", working, working, "small"
        )
        with self.assertRaises(kanban["KanbanError"]) as raised:
            kanban["write_text_atomic"](working, "# rewritten\n", entry=entry)

        self.assertIn("符号链接", str(raised.exception))
        self.assertEqual("keep-me\n", outside.read_text(encoding="utf-8"))

    def test_state_directory_symlink_is_rejected_by_check_and_start(self) -> None:
        task_id, task = self.make_todo("state-link")
        outside = self.root / "evil-working-outside"
        outside.mkdir()
        working = self.root / "working"
        shutil.rmtree(working)
        working.symlink_to(outside)
        self.install_fake_launchers()

        check = self.run_command("check", succeeds=False)
        start = self.run_command("start", task_id, succeeds=False)

        self.assertIn("状态目录不得是符号链接", check.stderr)
        self.assertIn("状态目录不得是符号链接", start.stderr)
        self.assertTrue(task.exists())
        self.assertEqual([], list(outside.iterdir()))

    def test_task_directory_swapped_for_symlink_is_rejected(self) -> None:
        task_id = f"{datetime.now().strftime('%Y%m%d')}-dir-swap-task"
        task_dir = self.root / "todo" / task_id
        task_dir.mkdir()
        (task_dir / "spec.md").write_text(
            "# 任务 dir-swap\n\n"
            "- 类型: Bug\n- 创建时间: 2026-08-11 00:00\n- 负责人:\n"
            "- 开始时间:\n- 完成时间:\n- 任务分支:\n- 结果:\n\n"
            "## 任务目标\n\n目标\n\n## 用户决策\n\nN/A\n\n"
            "## 预期成果\n\n成果\n\n## 验收条件\n\n- [ ] 条件\n\n"
            "## 威胁模型\n\nN/A\n\n## 不在本轮范围\n\n- 无\n\n"
            "## 讨论与决策\n\nN/A\n\n## 实施与验证\n\nN/A\n\n"
            "## 完成总结\n\n",
            encoding="utf-8",
        )
        outside_dir = self.root / "evil-task-outside"
        outside_dir.mkdir()
        (outside_dir / "spec.md").write_text("# evil\n", encoding="utf-8")
        self.assertIn("# 任务 dir-swap", self.run_command("show", task_id).stdout)
        # 模拟 scan 后把任务目录换成指向外部的软链.
        shutil.rmtree(task_dir)
        task_dir.symlink_to(outside_dir)

        show = self.run_command("show", task_id, succeeds=False)
        self.assertTrue(
            "符号链接" in show.stderr or "无效" in show.stderr or "不存在" in show.stderr,
            show.stderr,
        )
        self.assertEqual("# evil\n", (outside_dir / "spec.md").read_text(encoding="utf-8"))

    def test_symlink_entry_is_rejected_without_blocking_others(self) -> None:
        healthy, todo_path = self.make_todo("fine")
        link = self.root / "backlog" / f"{datetime.now().strftime('%Y%m%d')}-link-task.md"
        link.symlink_to(todo_path)

        result = self.run_command("check", succeeds=False)

        self.assertIn("符号链接", result.stderr)
        self.assertIn(healthy, self.run_command("list").stdout)

    def test_installer_copies_command_and_rules(self) -> None:
        install_home = self.root / "install-home"
        legacy_bin = install_home / ".local" / "bin"
        legacy_bin.mkdir(parents=True)
        for name in ("codex-review.sh", "claude-review.sh", "grok-review.sh"):
            (legacy_bin / name).write_text("legacy\n", encoding="utf-8")
        env = os.environ.copy()
        env["HOME"] = str(install_home)
        result = subprocess.run(
            ["sh", str(INSTALLER)],
            stdin=subprocess.DEVNULL,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("Onevoke installed\n", result.stdout)
        for name in ("codex-review.sh", "claude-review.sh", "grok-review.sh"):
            self.assertEqual(
                "legacy\n",
                (legacy_bin / name).read_text(encoding="utf-8"),
                name,
            )
        self.assertIn("检测到已退役的 Reviewer 脚本", result.stderr)
        self.assertIn("已保留旧 Reviewer 脚本", result.stderr)

        command = install_home / ".local" / "bin" / "kanban"
        for source in sorted((PROJECT_ROOT / "bin").iterdir()):
            if source.is_file():
                installed = install_home / ".local" / "bin" / source.name
                self.assertEqual(source.read_bytes(), installed.read_bytes(), source.name)
                self.assertTrue(os.access(installed, os.X_OK), source.name)
        # rules/ 下每份规则都必须被安装; 新增规则文件时无需改测试.
        for source in sorted(RULES_DIR.glob("*.md")):
            self.assertEqual(
                source.read_bytes(),
                (install_home / ".agents" / source.name).read_bytes(),
                source.name,
            )
        share_dir = install_home / ".local" / "share" / "onevoke" / "kanban-web"
        for source in sorted((PROJECT_ROOT / "share" / "kanban-web").iterdir()):
            if source.is_file():
                self.assertEqual(
                    source.read_bytes(),
                    (share_dir / source.name).read_bytes(),
                    source.name,
                )
        own_rules = install_home / ".agents" / "AGENTS.md"
        self.assertTrue(own_rules.is_symlink())
        self.assertEqual(Path("ONEVOKE-AGENTS.md"), own_rules.readlink())
        self.assertEqual(AGENT_RULES.read_bytes(), own_rules.read_bytes())

        self.assertIn("请在终端运行 onevoke welcome", result.stderr)

        output = subprocess.run(
            [str(command), "rules"],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, output.returncode, output.stderr)
        self.assertEqual(RULES.read_text(encoding="utf-8"), output.stdout)

    def test_installer_deletes_legacy_review_scripts_after_confirmation(self) -> None:
        install_home = self.root / "legacy-confirm-home"
        legacy_bin = install_home / ".local" / "bin"
        legacy_bin.mkdir(parents=True)
        names = ("codex-review.sh", "claude-review.sh", "grok-review.sh")
        for name in names:
            (legacy_bin / name).write_text("legacy\n", encoding="utf-8")

        result = subprocess.run(
            ["sh", str(INSTALLER)],
            input="y\n",
            env={**os.environ, "HOME": str(install_home)},
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("Onevoke installed\n", result.stdout)
        self.assertIn("是否删除这些旧脚本", result.stderr)
        self.assertIn("已删除旧 Reviewer 脚本", result.stderr)
        for name in names:
            self.assertFalse((legacy_bin / name).exists(), name)

    def test_installer_keeps_legacy_review_scripts_when_install_fails(self) -> None:
        install_home = self.root / "legacy-install-failure-home"
        legacy_bin = install_home / ".local" / "bin"
        legacy_bin.mkdir(parents=True)
        names = ("codex-review.sh", "claude-review.sh", "grok-review.sh")
        for name in names:
            (legacy_bin / name).write_text("legacy\n", encoding="utf-8")
        fake_bin = self.root / "failing-install-bin"
        fake_bin.mkdir()
        fake_install = fake_bin / "install"
        fake_install.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        fake_install.chmod(0o755)

        result = subprocess.run(
            ["sh", str(INSTALLER)],
            input="y\n",
            env={
                **os.environ,
                "HOME": str(install_home),
                "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
            },
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("已删除旧 Reviewer 脚本", result.stderr)
        for name in names:
            self.assertEqual(
                "legacy\n",
                (legacy_bin / name).read_text(encoding="utf-8"),
                name,
            )

    def test_installer_skips_non_file_rule_matches(self) -> None:
        project = self.root / "installer-project"
        (project / "bin").mkdir(parents=True)
        (project / "rules" / "ignored.md").mkdir(parents=True)
        (project / "install.sh").write_bytes(INSTALLER.read_bytes())
        (project / "bin" / "onevoke").write_text(
            "#!/bin/sh\nexit 0\n", encoding="utf-8"
        )
        (project / "bin" / "onevoke").chmod(0o755)
        (project / "rules" / "REAL.md").write_text("# real\n", encoding="utf-8")
        install_home = self.root / "non-file-rule-home"

        result = subprocess.run(
            ["sh", str(project / "install.sh")],
            stdin=subprocess.DEVNULL,
            env={**os.environ, "HOME": str(install_home)},
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue((install_home / ".agents" / "REAL.md").is_file())
        self.assertFalse((install_home / ".agents" / "ignored.md").exists())

    def test_installer_preserves_existing_agent_rules(self) -> None:
        install_home = self.root / "user-home"
        env = os.environ.copy()
        env["HOME"] = str(install_home)
        own_rules = install_home / ".agents" / "AGENTS.md"
        own_rules.parent.mkdir(parents=True)
        own_rules.write_text("本机自定规则\n", encoding="utf-8")

        for _ in range(2):
            result = subprocess.run(
                ["sh", str(INSTALLER)],
                stdin=subprocess.DEVNULL,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("本机自定规则\n", own_rules.read_text(encoding="utf-8"))
            self.assertNotIn(str(own_rules), result.stderr)

        self.assertEqual(
            AGENT_RULES.read_bytes(),
            (install_home / ".agents" / "ONEVOKE-AGENTS.md").read_bytes(),
        )

    def test_installer_preserves_agent_rules_directory(self) -> None:
        install_home = self.root / "rules-directory-home"
        env = os.environ.copy()
        env["HOME"] = str(install_home)
        own_rules = install_home / ".agents" / "AGENTS.md"
        own_rules.mkdir(parents=True)
        marker = own_rules / "keep"
        marker.write_text("preserved\n", encoding="utf-8")

        result = subprocess.run(
            ["sh", str(INSTALLER)],
            stdin=subprocess.DEVNULL,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(own_rules.is_dir())
        self.assertEqual("preserved\n", marker.read_text(encoding="utf-8"))

    def test_installer_preserves_dangling_agent_rules_symlink(self) -> None:
        install_home = self.root / "dangling-rules-home"
        env = os.environ.copy()
        env["HOME"] = str(install_home)
        own_rules = install_home / ".agents" / "AGENTS.md"
        own_rules.parent.mkdir(parents=True)
        own_rules.symlink_to("missing-user-rules.md")

        result = subprocess.run(
            ["sh", str(INSTALLER)],
            stdin=subprocess.DEVNULL,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(own_rules.is_symlink())
        self.assertEqual(Path("missing-user-rules.md"), own_rules.readlink())

    def test_installer_reports_welcome_failure_without_undoing_install(self) -> None:
        install_home = self.root / "welcome-failure-home"
        config = install_home / ".config" / "onevoke" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text("not json\n", encoding="utf-8")

        result = subprocess.run(
            ["sh", str(INSTALLER)],
            stdin=subprocess.DEVNULL,
            env={**os.environ, "HOME": str(install_home)},
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("Onevoke installed\n", result.stdout)
        self.assertIn("welcome 未完成", result.stderr)
        self.assertTrue((install_home / ".local" / "bin" / "onevoke").exists())

    def test_installer_always_overwrites_the_entry_rule(self) -> None:
        install_home = self.root / "overwrite-home"
        entry = install_home / ".agents" / "ONEVOKE-AGENTS.md"
        entry.parent.mkdir(parents=True)
        entry.write_text("用户旧配置\n", encoding="utf-8")

        result = subprocess.run(
            ["sh", str(INSTALLER)],
            stdin=subprocess.DEVNULL,
            env={**os.environ, "HOME": str(install_home)},
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(AGENT_RULES.read_bytes(), entry.read_bytes())

    def test_installer_rejects_a_directory_at_a_file_target(self) -> None:
        install_home = self.root / "bad-target-home"
        entry = install_home / ".agents" / "ONEVOKE-AGENTS.md"
        entry.mkdir(parents=True)

        result = subprocess.run(
            ["sh", str(INSTALLER)],
            stdin=subprocess.DEVNULL,
            env={**os.environ, "HOME": str(install_home)},
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(1, result.returncode)
        self.assertIn("安装目标是目录", result.stderr)
        self.assertFalse((install_home / ".local" / "bin").exists())
        self.assertTrue(entry.is_dir())

    def test_installer_rejects_a_directory_at_a_legacy_review_target(self) -> None:
        install_home = self.root / "legacy-directory-home"
        legacy_target = install_home / ".local" / "bin" / "codex-review.sh"
        legacy_target.mkdir(parents=True)

        result = subprocess.run(
            ["sh", str(INSTALLER)],
            stdin=subprocess.DEVNULL,
            env={**os.environ, "HOME": str(install_home)},
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(1, result.returncode)
        self.assertIn("旧版安装目标是目录", result.stderr)
        self.assertFalse((install_home / ".local" / "bin" / "onevoke").exists())
        self.assertTrue(legacy_target.is_dir())

    def test_installer_rejects_arguments(self) -> None:
        chinese_help = subprocess.run(
            ["sh", str(INSTALLER), "--lang", "cn", "--help"],
            env={
                **os.environ,
                "HOME": str(self.root / "help-home-cn"),
                "ONEVOKE_LANG": "en",
            },
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, chinese_help.returncode, chinese_help.stderr)
        self.assertIn("用法: install.sh", chinese_help.stdout)

        help_result = subprocess.run(
            ["sh", str(INSTALLER), "--lang", "en", "--help"],
            env={**os.environ, "HOME": str(self.root / "help-home")},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, help_result.returncode, help_result.stderr)
        self.assertIn("--lang {cn,en}", help_result.stdout)

        result = subprocess.run(
            ["sh", str(INSTALLER), "--force"],
            stdin=subprocess.DEVNULL,
            env={**os.environ, "HOME": str(self.root / "arg-home")},
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("用法: install.sh", result.stderr)

        english = subprocess.run(
            ["sh", str(INSTALLER), "--force"],
            stdin=subprocess.DEVNULL,
            env={
                **os.environ,
                "HOME": str(self.root / "arg-home-en"),
                "ONEVOKE_LANG": "en",
                "LC_ALL": "zh_CN.UTF-8",
            },
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(2, english.returncode)
        self.assertIn("usage: install.sh", english.stderr)
        self.assertNotIn("用法", english.stderr)

        fallbacks = (
            {"ONEVOKE_LANG": "zh", "LC_ALL": "en"},
            {"ONEVOKE_LANG": "", "LC_ALL": "zh", "LC_MESSAGES": "en"},
            {"ONEVOKE_LANG": "", "LC_ALL": "", "LC_MESSAGES": "zh", "LANG": "en"},
            {"ONEVOKE_LANG": "", "LC_ALL": "", "LC_MESSAGES": "", "LANG": "zh"},
        )
        for index, locale_env in enumerate(fallbacks):
            localized = subprocess.run(
                ["sh", str(INSTALLER), "--force"],
                stdin=subprocess.DEVNULL,
                env={
                    **os.environ,
                    "HOME": str(self.root / f"arg-home-locale-{index}"),
                    **locale_env,
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(2, localized.returncode)
            self.assertIn("用法: install.sh", localized.stderr)

        missing = subprocess.run(
            ["sh", str(INSTALLER), "--lang"],
            env={**os.environ, "HOME": str(self.root / "arg-home-missing")},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(2, missing.returncode)
        self.assertIn("用法: install.sh", missing.stderr)

        invalid = subprocess.run(
            ["sh", str(INSTALLER), "--lang", "fr"],
            env={
                **os.environ,
                "HOME": str(self.root / "arg-home-invalid"),
                "ONEVOKE_LANG": "en",
            },
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(2, invalid.returncode)
        self.assertIn("--lang must be cn or en", invalid.stderr)

    def test_installer_passes_explicit_language_to_welcome(self) -> None:
        project = self.root / "lang-installer-project"
        (project / "bin").mkdir(parents=True)
        (project / "install.sh").write_bytes(INSTALLER.read_bytes())
        (project / "bin" / "onevoke").write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$WELCOME_ARGS\"\n",
            encoding="utf-8",
        )
        (project / "bin" / "onevoke").chmod(0o755)
        welcome_args = self.root / "welcome-args"

        result = subprocess.run(
            ["sh", str(project / "install.sh"), "--lang", "cn"],
            env={
                **os.environ,
                "HOME": str(self.root / "lang-install-home"),
                "WELCOME_ARGS": str(welcome_args),
            },
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("--lang\ncn\nwelcome\n", welcome_args.read_text(encoding="utf-8"))

    def test_web_help_and_invalid_refresh(self) -> None:
        help_text = self.run_command("web", "--help").stdout
        self.assertIn("--host", help_text)
        self.assertIn("--port", help_text)
        self.assertIn("--refresh", help_text)
        self.assertIn("默认 60", help_text)
        bad = self.run_command("web", "--refresh", "0", succeeds=False)
        self.assertIn("扫描间隔", bad.stderr)

    def test_tui_help_and_rejects_invalid_or_noninteractive_use(self) -> None:
        help_text = self.run_command("tui", "--help").stdout
        self.assertIn("--single", help_text)
        self.assertIn("--refresh", help_text)
        self.assertIn("默认 30", help_text)
        self.assertIn("--theme", help_text)

        bad_refresh = self.run_command("tui", "--refresh", "0", succeeds=False)
        self.assertIn("刷新间隔", bad_refresh.stderr)
        bad_theme = self.run_command("tui", "--theme", "sepia", succeeds=False)
        self.assertIn("--theme", bad_theme.stderr)
        self.assertIn("sepia", bad_theme.stderr)
        noninteractive = self.run_command("tui", succeeds=False)
        self.assertIn("TUI 需要交互终端", noninteractive.stderr)
        self.assertIn("stdin/stdout 均为 tty", noninteractive.stderr)

    def test_tui_model_filters_web_fields_and_navigates_columns(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)

        model = kanban_tui.BoardModel(single=True)
        model.set_board({
            "generated_at": "2026-08-20 22:30:00",
            "tasks": [
                {
                    "task_id": "20260820-first-task",
                    "title": "第一项",
                    "state": "todo",
                    "task_group": "20260820-terminal-group",
                    "type": "Feature",
                    "assignee": "Codex",
                },
                {
                    "task_id": "20260820-second-task",
                    "title": "Second item",
                    "state": "todo",
                    "task_group": "",
                    "type": "Bug",
                    "assignee": "",
                },
                {
                    "task_id": "20260820-old-task",
                    "title": "Old item",
                    "state": "archived",
                    "task_group": "",
                    "type": "Chore",
                    "assignee": "QA",
                },
            ],
        })

        self.assertTrue(model.single)
        self.assertEqual(kanban_tui.ACTIVE_STATES, model.states)
        model.column_index = model.states.index("todo")
        self.assertEqual("20260820-first-task", model.selected_task()["task_id"])
        model.move_task(1)
        self.assertEqual("20260820-second-task", model.selected_task()["task_id"])

        model.query = "terminal-group"
        model.normalize()
        self.assertEqual(["20260820-first-task"], [
            task["task_id"] for task in model.tasks_for("todo")
        ])
        model.query = "qa"
        model.normalize()
        self.assertEqual([], model.tasks_for("todo"))
        self.assertEqual(["20260820-old-task"], [
            task["task_id"] for task in model.tasks_for("archived")
        ])

        model.query = ""
        model.toggle_archived()
        self.assertEqual(kanban_tui.ALL_STATES, model.states)
        model.column_index = 0
        model.move_column(-1)
        self.assertEqual("trash", model.current_state)
        model.toggle_archived()
        self.assertEqual("done", model.current_state)

    def test_tui_mouse_selects_columns_tasks_and_scrolls_detail(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)
        import curses
        from unittest import mock

        class FakeScreen:
            def getmaxyx(self):
                return 24, 120

            def bkgd(self, _char, attr=0):
                pass

        board = {
            "generated_at": "2026-08-21 18:00:00",
            "tasks": [
                {
                    "task_id": "20260821-one-task",
                    "title": "一号",
                    "state": "backlog",
                    "type": "Feature",
                    "kind": "small",
                    "assignee": "",
                    "time": "-",
                },
                {
                    "task_id": "20260821-two-task",
                    "title": "二号",
                    "state": "backlog",
                    "type": "Feature",
                    "kind": "small",
                    "assignee": "",
                    "time": "-",
                },
                {
                    "task_id": "20260821-todo-task",
                    "title": "待办",
                    "state": "todo",
                    "type": "Feature",
                    "kind": "small",
                    "assignee": "",
                    "time": "-",
                },
            ],
        }
        detail_docs = {
            "20260821-one-task": {
                "task_id": "20260821-one-task",
                "title": "一号",
                "state": "backlog",
                "document": "line1\nline2\nline3\nline4\nline5\nline6\n",
            },
            "20260821-two-task": {
                "task_id": "20260821-two-task",
                "title": "二号",
                "state": "backlog",
                "document": "line1\nline2\nline3\nline4\nline5\nline6\n",
            },
        }
        tui = kanban_tui.KanbanTui(
            FakeScreen(),
            single=False,
            refresh_interval=60,
            context={},
            get_board=lambda: board,
            get_task=lambda task_id: detail_docs[task_id],
            theme="dark",
        )
        tui.mouse_enabled = True
        tui.model.set_board(board)
        self.assertEqual("backlog", tui.model.current_state)

        self.assertEqual(-1, kanban_tui.mouse_wheel_delta(curses.BUTTON4_PRESSED))
        self.assertEqual(1, kanban_tui.mouse_wheel_delta(curses.BUTTON5_PRESSED))
        self.assertTrue(kanban_tui.mouse_left_clicked(curses.BUTTON1_CLICKED))
        self.assertTrue(kanban_tui.mouse_left_double_clicked(curses.BUTTON1_DOUBLE_CLICKED))

        # 点 todo 栏标题切换栏目.
        layout = tui._visible_column_layout()
        self.assertGreaterEqual(len(layout), 2)
        todo_state, todo_x, _todo_w = layout[1]
        self.assertEqual("todo", todo_state)
        with mock.patch.object(
            kanban_tui.curses,
            "getmouse",
            return_value=(0, todo_x + 1, 2, 0, curses.BUTTON1_CLICKED),
        ):
            tui._handle_mouse()
        self.assertEqual("todo", tui.model.current_state)

        # 点 backlog 第二张卡选中.
        backlog_state, backlog_x, _bw = layout[0]
        card_y = kanban_tui.BODY_TOP + kanban_tui.CARD_HEIGHT
        with mock.patch.object(
            kanban_tui.curses,
            "getmouse",
            return_value=(0, backlog_x + 2, card_y, 0, curses.BUTTON1_CLICKED),
        ):
            tui._handle_mouse()
        self.assertEqual("backlog", tui.model.current_state)
        self.assertEqual("20260821-two-task", tui.model.selected_ids["backlog"])

        # 双击打开详情.
        with mock.patch.object(
            kanban_tui.curses,
            "getmouse",
            return_value=(
                0,
                backlog_x + 2,
                card_y,
                0,
                curses.BUTTON1_DOUBLE_CLICKED,
            ),
        ):
            tui._handle_mouse()
        self.assertIsNotNone(tui.detail)
        self.assertEqual("20260821-two-task", tui.detail["task_id"])

        # 详情页滚轮下滚.
        before = tui.detail_scroll
        with mock.patch.object(
            kanban_tui.curses,
            "getmouse",
            return_value=(0, 1, 10, 0, curses.BUTTON5_PRESSED),
        ):
            tui._handle_mouse()
        self.assertEqual(before + kanban_tui.MOUSE_SCROLL_STEP, tui.detail_scroll)

        # 单击搜索行进入搜索.
        tui._close_detail()
        with mock.patch.object(
            kanban_tui.curses,
            "getmouse",
            return_value=(0, 2, 1, 0, curses.BUTTON1_CLICKED),
        ):
            tui._handle_mouse()
        self.assertTrue(tui.searching)

        # 单栏标题左右箭头切栏.
        tui.searching = False
        tui.model.single = True
        tui.model.column_index = 0
        with mock.patch.object(
            kanban_tui.curses,
            "getmouse",
            return_value=(0, 0, 2, 0, curses.BUTTON1_CLICKED),
        ):
            tui._handle_mouse()
        self.assertEqual("done", tui.model.current_state)

        # 鼠标不可用时忽略事件.
        tui.mouse_enabled = False
        tui.model.column_index = 0
        with mock.patch.object(
            kanban_tui.curses,
            "getmouse",
            return_value=(0, todo_x + 1, 2, 0, curses.BUTTON1_CLICKED),
        ):
            tui._handle_mouse()
        self.assertEqual("backlog", tui.model.current_state)

        model_focus = kanban_tui.BoardModel()
        model_focus.set_board(board)
        self.assertTrue(model_focus.focus_state("todo"))
        self.assertTrue(model_focus.select_task_index("backlog", 1))
        self.assertEqual("20260821-two-task", model_focus.selected_ids["backlog"])
        self.assertEqual("backlog", model_focus.current_state)

    def test_tui_fits_visible_columns_and_keeps_focus_on_screen(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)

        self.assertEqual(1, kanban_tui.visible_column_count(20, 4))
        self.assertEqual(1, kanban_tui.visible_column_count(39, 4))
        self.assertEqual(1, kanban_tui.visible_column_count(40, 4))
        self.assertEqual(1, kanban_tui.visible_column_count(80, 4))
        self.assertEqual(2, kanban_tui.visible_column_count(81, 4))
        self.assertEqual(3, kanban_tui.visible_column_count(122, 4))
        self.assertEqual(4, kanban_tui.visible_column_count(163, 4))
        self.assertEqual(1, kanban_tui.visible_column_count(160, 4, single=True))
        for width, count in ((81, 2), (122, 3), (163, 4)):
            layout = kanban_tui.column_geometry(width, count)
            self.assertEqual(count, len(layout))
            self.assertTrue(all(column_width >= 40 for _x, column_width, _sep in layout))
            last_x, last_width, last_sep = layout[-1]
            self.assertFalse(last_sep)
            self.assertEqual(width, last_x + last_width)

        model = kanban_tui.BoardModel()
        self.assertEqual(("backlog", "todo", "working"), model.visible_states(3))
        model.move_column(1)
        self.assertEqual("todo", model.current_state)
        self.assertEqual(("backlog", "todo", "working"), model.visible_states(3))
        model.move_column(2)
        self.assertEqual("done", model.current_state)
        self.assertEqual(("todo", "working", "done"), model.visible_states(3))
        self.assertIn(model.current_state, model.visible_states(3))
        model.move_column(1)
        self.assertEqual("backlog", model.current_state)
        self.assertEqual(("backlog", "todo", "working"), model.visible_states(3))
        model.move_column(-1)
        self.assertEqual("done", model.current_state)
        self.assertEqual(("todo", "working", "done"), model.visible_states(3))

        model.toggle_archived()
        model.column_index = model.states.index("trash")
        self.assertEqual(("done", "archived", "trash"), model.visible_states(3))
        model.toggle_archived()
        self.assertEqual("done", model.current_state)
        self.assertEqual(("todo", "working", "done"), model.visible_states(3))

        class FakeScreen:
            def __init__(self, width: int) -> None:
                self.width = width
                self.writes = []

            def getmaxyx(self):
                return 24, self.width

            def addstr(self, y, x, text, attr=0):
                self.writes.append((y, x, text))

            def move(self, y, x):
                pass

        context = {
            "state_labels": {state: state for state in STATES},
            "size_labels": {"small": "small", "large": "large"},
            "empty": "No tasks",
            "too_small": "Terminal is too small.",
        }
        board = {
            "generated_at": "2026-08-21 00:00:00",
            "tasks": [
                {
                    "task_id": f"20260821-{state}-task",
                    "title": state,
                    "state": state,
                }
                for state in ("backlog", "todo", "working", "done")
            ],
        }

        narrow = FakeScreen(122)
        tui = kanban_tui.KanbanTui(
            narrow,
            single=False,
            refresh_interval=30,
            context=context,
            get_board=lambda: board,
            get_task=lambda _task_id: {},
        )
        tui.model.set_board(board)
        tui._render_board()
        headings = " ".join(text for y, _x, text in narrow.writes if y == 2)
        self.assertIn("backlog", headings)
        self.assertIn("todo", headings)
        self.assertIn("working", headings)
        self.assertNotIn("done", headings)
        self.assertEqual(("backlog", "todo", "working"), tui.model.visible_states(3))
        self.assertNotIn("too narrow", headings.lower())
        self.assertNotIn("use --single", " ".join(text for _y, _x, text in narrow.writes))

        narrow.writes.clear()
        tui.model.move_column(3)
        tui._render_board()
        focused_headings = " ".join(text for y, _x, text in narrow.writes if y == 2)
        self.assertIn("done", focused_headings)
        self.assertNotIn("backlog", focused_headings)
        self.assertIn(tui.model.current_state, tui.model.visible_states(3))
        self.assertEqual(("todo", "working", "done"), tui.model.visible_states(3))

        one_column = FakeScreen(20)
        tui = kanban_tui.KanbanTui(
            one_column,
            single=False,
            refresh_interval=30,
            context=context,
            get_board=lambda: board,
            get_task=lambda _task_id: {},
        )
        tui.model.set_board(board)
        tui._render_board()
        self.assertFalse(
            any("too small" in text.lower() for _y, _x, text in one_column.writes)
        )
        single_headings = [text for y, _x, text in one_column.writes if y == 2]
        self.assertEqual(1, len(single_headings))
        self.assertIn("backlog", single_headings[0])
        self.assertEqual([(0, 20, False)], kanban_tui.column_geometry(20, 1))

        wide = FakeScreen(80)
        tui = kanban_tui.KanbanTui(
            wide,
            single=True,
            refresh_interval=30,
            context=context,
            get_board=lambda: board,
            get_task=lambda _task_id: {},
        )
        tui.model.set_board(board)
        tui.model.move_column(2)
        tui._render_board()
        forced = [text for y, _x, text in wide.writes if y == 2]
        self.assertEqual(1, len(forced))
        self.assertIn("working", forced[0])
        self.assertNotIn("todo", forced[0])

    def test_tui_column_width_keys_adjust_and_persist(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)

        config_dir = self.home / ".config" / "onevoke"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "config.json"
        prefs = config_dir / "tui.json"
        self.env["ONEVOKE_CONFIG"] = str(config_path)
        self.assertFalse(prefs.exists())
        with mock.patch.dict(os.environ, {"ONEVOKE_CONFIG": str(config_path)}):
            self.assertEqual(40, kanban_tui.load_column_width())
            self.assertEqual(2, kanban_tui.visible_column_count(100, 4, column_width=40))
            self.assertEqual(3, kanban_tui.visible_column_count(100, 4, column_width=30))
            self.assertEqual(4, kanban_tui.visible_column_count(100, 4, column_width=20))
            self.assertEqual(1, kanban_tui.visible_column_count(80, 4, column_width=40))

            kanban_tui.save_column_width(35)
            self.assertEqual(35, kanban_tui.load_column_width())
            self.assertTrue(prefs.is_file())
            self.assertEqual(0o600, prefs.stat().st_mode & 0o777)
            prefs.write_text('{"column_width": 999}\n', encoding="utf-8")
            self.assertEqual(40, kanban_tui.load_column_width())
            prefs.write_text('{"column_width": 0}\n', encoding="utf-8")
            self.assertEqual(40, kanban_tui.load_column_width())
            prefs.write_text('{"column_width": "wide"}\n', encoding="utf-8")
            self.assertEqual(40, kanban_tui.load_column_width())
            prefs.write_text(
                '{"column_width": ' + ("9" * 5000) + "}\n",
                encoding="utf-8",
            )
            self.assertEqual(40, kanban_tui.load_column_width())

        class FakeScreen:
            def getmaxyx(self):
                return 24, 120

        saved = []
        tui = kanban_tui.KanbanTui(
            FakeScreen(),
            single=False,
            refresh_interval=30,
            context={},
            get_board=lambda: {"tasks": []},
            get_task=lambda _task_id: {},
            column_width=40,
            persist_column_width=saved.append,
        )
        tui._handle_board_key("-")
        self.assertEqual(35, tui.column_width)
        self.assertEqual([35], saved)
        tui._handle_board_key("=")
        self.assertEqual(40, tui.column_width)
        self.assertEqual([35, 40], saved)
        tui._handle_board_key("+")
        self.assertEqual(45, tui.column_width)
        tui._handle_board_key("_")
        self.assertEqual(40, tui.column_width)

        tui.column_width = kanban_tui.MIN_COLUMN_WIDTH
        tui._handle_board_key("-")
        self.assertEqual(kanban_tui.MIN_COLUMN_WIDTH, tui.column_width)
        tui.column_width = kanban_tui.MAX_COLUMN_WIDTH
        before = len(saved)
        tui._handle_board_key("=")
        self.assertEqual(kanban_tui.MAX_COLUMN_WIDTH, tui.column_width)
        self.assertEqual(before, len(saved))

        def fail_persist(_width: int) -> None:
            raise OSError("disk full")

        failing = kanban_tui.KanbanTui(
            FakeScreen(),
            single=False,
            refresh_interval=30,
            context={},
            get_board=lambda: {"tasks": []},
            get_task=lambda _task_id: {},
            column_width=40,
            persist_column_width=fail_persist,
        )
        failing._handle_board_key("-")
        self.assertEqual(35, failing.column_width)
        self.assertIn("disk full", failing.prefs_error)
        self.assertIn("disk full", failing._status_error())
        failing.model.refresh_error = "board unavailable"
        status = failing._status_error()
        self.assertIn("disk full", status)
        self.assertIn("board unavailable", status)
        self.assertLess(status.index("board unavailable"), status.index("disk full"))

        class FooterScreen:
            def __init__(self, width: int) -> None:
                self.width = width
                self.writes = []

            def getmaxyx(self):
                return 24, self.width

            def addstr(self, y, x, text, attr=0):
                self.writes.append((y, x, text, attr))

            def move(self, y, x):
                pass

        narrow = FooterScreen(20)
        visible = kanban_tui.KanbanTui(
            narrow,
            single=False,
            refresh_interval=30,
            context={
                "title": "Task Board",
                "search": "Search",
                "theme": "Theme",
                "theme_labels": {"auto": "auto"},
                "width": "Width",
                "active": "active",
                "updated": "Updated",
                "state_labels": {state: state for state in STATES},
                "size_labels": {"small": "small", "large": "large"},
                "empty": "No tasks",
                "error": "Error",
            },
            get_board=lambda: {"tasks": []},
            get_task=lambda _task_id: {},
            column_width=40,
        )
        visible.model.set_board({"generated_at": "-", "tasks": []})
        visible._render_board()
        toolbar = " ".join(text for y, _x, text, _attr in narrow.writes if y == 1)
        self.assertIn("40", toolbar)

        wide_value = FooterScreen(20)
        visible.screen = wide_value
        visible.column_width = 120
        wide_value.writes.clear()
        visible._render_board()
        wide_toolbar = " ".join(text for y, _x, text, _attr in wide_value.writes if y == 1)
        self.assertIn("120", wide_toolbar)

        chinese = FooterScreen(20)
        visible.screen = chinese
        visible.column_width = 40
        visible.context = {
            **visible.context,
            "search": "搜索",
            "width": "栏宽",
            "theme": "主题",
            "theme_labels": {"auto": "自动"},
            "active": "活跃栏目",
            "updated": "更新于",
        }
        chinese.writes.clear()
        visible._render_board()
        chinese_toolbar = " ".join(text for y, _x, text, _attr in chinese.writes if y == 1)
        self.assertIn("40", chinese_toolbar)

        footer_screen = FooterScreen(80)
        both = kanban_tui.KanbanTui(
            footer_screen,
            single=False,
            refresh_interval=30,
            context={
                "error": "Error",
                "state_labels": {state: state for state in STATES},
                "size_labels": {"small": "small", "large": "large"},
            },
            get_board=lambda: {"tasks": []},
            get_task=lambda _task_id: {},
            column_width=40,
        )
        both.prefs_error = (
            f"[Errno 13] Permission denied: '{self.home / '.config' / 'onevoke' / 'tui.json'}'"
        )
        both.model.refresh_error = "board unavailable"
        both._render_footer(24, 80)
        footer = next(text for y, _x, text, _attr in footer_screen.writes if y == 23)
        self.assertIn("board unavailable", footer)
        self.assertTrue(footer.index("board unavailable") < footer.index("Permission"))

    def test_tui_page_keys_move_selection_by_page_and_clamp(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)
        import curses

        class FakeScreen:
            def getmaxyx(self):
                return 24, 100

        tui = kanban_tui.KanbanTui(
            FakeScreen(),
            single=True,
            refresh_interval=60,
            context={},
            get_board=lambda: {"tasks": []},
            get_task=lambda _task_id: {},
        )
        tui.model.set_board({
            "tasks": [
                {"task_id": f"20260820-page-{index:02d}-task", "state": "todo"}
                for index in range(10)
            ],
        })
        tui.model.column_index = tui.model.states.index("todo")
        page = tui._page_size()
        self.assertEqual((24 - 4) // kanban_tui.CARD_HEIGHT, page)

        tui._handle_board_key(curses.KEY_NPAGE)
        self.assertEqual(
            f"20260820-page-{page:02d}-task", tui.model.selected_task()["task_id"]
        )
        self.assertEqual(page, tui.model.scrolls["todo"])
        for _ in range(5):
            tui._handle_board_key(curses.KEY_NPAGE)
        self.assertEqual(
            "20260820-page-09-task", tui.model.selected_task()["task_id"]
        )
        self.assertEqual(10 - page, tui.model.scrolls["todo"])
        tui._handle_board_key(curses.KEY_PPAGE)
        self.assertEqual(
            f"20260820-page-{9 - page:02d}-task",
            tui.model.selected_task()["task_id"],
        )
        self.assertEqual(10 - 2 * page, tui.model.scrolls["todo"])
        for _ in range(5):
            tui._handle_board_key(curses.KEY_PPAGE)
        self.assertEqual(
            "20260820-page-00-task", tui.model.selected_task()["task_id"]
        )
        self.assertEqual(0, tui.model.scrolls["todo"])

    def test_tui_detail_vim_paging_and_document_search(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)

        class FakeScreen:
            def __init__(self, height: int, width: int) -> None:
                self.height = height
                self.width = width
                self.writes = []

            def getmaxyx(self):
                return self.height, self.width

            def addstr(self, y, x, text, attr=0):
                self.writes.append((y, x, text, attr))

            def move(self, y, x):
                self.cursor = (y, x)

        document = "\n".join(
            f"line-{index:02d} needle" if index in {3, 12, 20} else f"line-{index:02d}"
            for index in range(30)
        )
        screen = FakeScreen(14, 40)
        tui = kanban_tui.KanbanTui(
            screen,
            single=True,
            refresh_interval=60,
            context={
                "search": "Search",
                "search_help": "Enter apply | Esc clear",
                "detail_help": "detail keys",
                "no_match": "no match",
                "state_labels": {"todo": "todo"},
                "size_labels": {"small": "small"},
            },
            get_board=lambda: {"tasks": []},
            get_task=lambda _task_id: {},
        )
        tui.detail = {
            "task_id": "20260821-detail-search-task",
            "title": "Detail search",
            "state": "todo",
            "kind": "small",
            "document": document,
        }
        body = tui._detail_body_height()
        self.assertEqual(10, body)
        half = max(1, body // 2)

        tui._handle_detail_key("\x04")  # Ctrl-d
        self.assertEqual(half, tui.detail_scroll)
        tui._handle_detail_key("\x06")  # Ctrl-f
        self.assertEqual(half + body, tui.detail_scroll)
        tui._handle_detail_key("\x15")  # Ctrl-u
        self.assertEqual(half + body - half, tui.detail_scroll)
        tui._handle_detail_key("\x02")  # Ctrl-b
        self.assertEqual(max(0, half + body - half - body), tui.detail_scroll)
        tui._handle_detail_key("G")
        tui._render_detail()
        self.assertEqual(max(0, 30 - body), tui.detail_scroll)
        tui._handle_detail_key("g")
        tui._handle_detail_key("g")
        self.assertEqual(0, tui.detail_scroll)

        tui._handle_detail_key("/")
        self.assertTrue(tui.detail_searching)
        for char in "needle":
            tui._handle_detail_key(char)
        self.assertEqual("needle", tui.detail_query)
        tui._handle_detail_key("\n")
        self.assertFalse(tui.detail_searching)
        self.assertEqual([3, 12, 20], tui._detail_matches())
        self.assertEqual(0, tui.detail_match_index)
        self.assertEqual(max(0, 3 - body // 3), tui.detail_scroll)

        tui._handle_detail_key("n")
        self.assertEqual(1, tui.detail_match_index)
        self.assertEqual(max(0, 12 - body // 3), tui.detail_scroll)
        tui._handle_detail_key("N")
        self.assertEqual(0, tui.detail_match_index)

        tui._handle_detail_key("/")
        tui._handle_detail_key("\x1b")
        self.assertEqual("", tui.detail_query)
        self.assertFalse(tui.detail_searching)

        tui.detail_query = "missing"
        tui._apply_detail_search()
        screen.writes = []
        tui._render_detail()
        footer = next(write for write in screen.writes if write[0] == 13)
        self.assertIn("no match", footer[2])

        self.assertEqual([0, 2], kanban_tui.line_match_indexes(["Alpha", "beta", "ALPHA"], "alpha"))
        self.assertEqual([(2, 5)], kanban_tui.match_spans("xxABCyy", "abc"))

    def test_tui_theme_key_cycles_and_run_rejects_unknown_theme(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)

        class FakeScreen:
            def getmaxyx(self):
                return 24, 100

        tui = kanban_tui.KanbanTui(
            FakeScreen(),
            single=True,
            refresh_interval=60,
            context={},
            get_board=lambda: {"tasks": []},
            get_task=lambda _task_id: {},
        )
        self.assertEqual("auto", tui.theme)
        seen = []
        for _ in kanban_tui.THEMES:
            tui._handle_board_key("t")
            seen.append(tui.theme)
        self.assertEqual(["light", "dark", "auto"], seen)

        with self.assertRaises(kanban_tui.KanbanTuiError) as raised:
            kanban_tui.run(
                single=False,
                refresh_interval=60,
                context={},
                get_board=lambda: {"tasks": []},
                get_task=lambda _task_id: {},
                theme="sepia",
            )
        self.assertIn("sepia", str(raised.exception))

    def test_tui_apply_theme_sets_palette_background_and_auto_fallback(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)
        import curses
        from unittest import mock

        class FakeScreen:
            def __init__(self):
                self.background = None

            def getmaxyx(self):
                return 24, 100

            def bkgd(self, _char, attr=0):
                self.background = attr

        pairs = {}
        screen = FakeScreen()
        tui = kanban_tui.KanbanTui(
            screen,
            single=True,
            refresh_interval=60,
            context={},
            get_board=lambda: {"tasks": []},
            get_task=lambda _task_id: {},
            theme="light",
        )
        with mock.patch.object(kanban_tui.curses, "has_colors", return_value=True), \
                mock.patch.object(kanban_tui.curses, "use_default_colors"), \
                mock.patch.object(
                    kanban_tui.curses,
                    "init_pair",
                    side_effect=lambda index, fg, bg: pairs.__setitem__(
                        index, (fg, bg)
                    ),
                ), \
                mock.patch.object(
                    kanban_tui.curses,
                    "color_pair",
                    side_effect=lambda index: index << 8,
                ), \
                mock.patch.dict(kanban_tui.os.environ, {"COLORFGBG": ""}):
            tui._init_style()
            light = kanban_tui.THEME_PALETTES["light"]
            self.assertTrue(
                all(bg == curses.COLOR_WHITE for _fg, bg in pairs.values())
            )
            self.assertEqual(light["backlog"], pairs[2][0])
            self.assertEqual(light["working"], pairs[4][0])
            self.assertEqual(tui.colors["text"], screen.background)
            self.assertNotEqual(0, tui.colors["error"])
            footer = tui._footer_attr()
            self.assertEqual(
                tui.colors["accent"] | curses.A_REVERSE, footer
            )
            self.assertEqual({}, tui.highlight_colors)

            pairs.clear()
            tui._handle_board_key("t")
            self.assertEqual("dark", tui.theme)
            self.assertTrue(
                all(bg == curses.COLOR_BLACK for _fg, bg in pairs.values())
            )
            self.assertEqual(
                kanban_tui.THEME_PALETTES["dark"]["working"], pairs[4][0]
            )
            self.assertEqual(tui.colors["text"], screen.background)

            pairs.clear()
            tui._handle_board_key("t")
            self.assertEqual("auto", tui.theme)
            base_pairs = {
                index: value
                for index, value in pairs.items()
                if index <= len(kanban_tui.COLOR_NAMES)
            }
            highlight_pairs = {
                index: value
                for index, value in pairs.items()
                if len(kanban_tui.COLOR_NAMES)
                < index
                <= 2 * len(kanban_tui.COLOR_NAMES)
            }
            self.assertTrue(all(bg == -1 for _fg, bg in base_pairs.values()))
            self.assertTrue(
                all(bg == curses.COLOR_BLACK for _fg, bg in highlight_pairs.values())
            )
            # 8 色 auto 深底: 分隔线弱化色用亮黑, 背景沿用终端默认.
            muted_index = 1 + 2 * len(kanban_tui.COLOR_NAMES)
            self.assertEqual((curses.COLOR_BLACK, -1), pairs[muted_index])
            self.assertEqual(
                (muted_index << 8) | curses.A_BOLD, tui.colors["muted"]
            )
            # 背景未知时仅正文回退到终端默认前景色; backlog 保持色相.
            self.assertEqual(-1, pairs[1][0])
            self.assertEqual(
                kanban_tui.THEME_PALETTES["dark"]["backlog"], pairs[2][0]
            )
            self.assertEqual(
                kanban_tui.THEME_PALETTES["dark"]["backlog"],
                pairs[2 + len(kanban_tui.COLOR_NAMES)][0],
            )
            self.assertEqual(
                curses.COLOR_WHITE, pairs[1 + len(kanban_tui.COLOR_NAMES)][0]
            )
            self.assertEqual(0, screen.background)
            self.assertEqual(
                tui.highlight_colors["backlog"] | curses.A_REVERSE | curses.A_BOLD,
                tui._highlight_attr("backlog", curses.A_BOLD),
            )

    def test_tui_auto_theme_light_terminal_uses_readable_selection(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)
        import curses
        from unittest import mock

        class FakeScreen:
            def getmaxyx(self):
                return 24, 100

            def bkgd(self, _char, attr=0):
                pass

        pairs = {}
        tui = kanban_tui.KanbanTui(
            FakeScreen(),
            single=True,
            refresh_interval=60,
            context={},
            get_board=lambda: {"tasks": []},
            get_task=lambda _task_id: {},
            theme="auto",
        )
        with mock.patch.object(kanban_tui.curses, "has_colors", return_value=True), \
                mock.patch.object(kanban_tui.curses, "use_default_colors"), \
                mock.patch.object(
                    kanban_tui.curses,
                    "init_pair",
                    side_effect=lambda index, fg, bg: pairs.__setitem__(
                        index, (fg, bg)
                    ),
                ), \
                mock.patch.object(
                    kanban_tui.curses,
                    "color_pair",
                    side_effect=lambda index: index << 8,
                ), \
                mock.patch.dict(kanban_tui.os.environ, {"COLORFGBG": "0;15"}):
            tui._init_style()
            light = kanban_tui.THEME_PALETTES["light"]
            self.assertEqual(light["backlog"], pairs[2][0])
            self.assertEqual(-1, pairs[2][1])
            highlight_backlog = pairs[2 + len(kanban_tui.COLOR_NAMES)]
            self.assertEqual(light["backlog"], highlight_backlog[0])
            self.assertEqual(curses.COLOR_WHITE, highlight_backlog[1])
            self.assertEqual(
                tui.highlight_colors["backlog"] | curses.A_REVERSE,
                tui._highlight_attr("backlog"),
            )

    def test_tui_theme_survives_terminal_with_few_color_pairs(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)
        import curses
        from unittest import mock

        class FakeScreen:
            def getmaxyx(self):
                return 24, 100

            def bkgd(self, _char, attr=0):
                pass

        def limited_init_pair(index, _fg, _bg):
            # 模拟 COLOR_PAIRS=8 的终端: 合法颜色对只有 1..7.
            if index > 7:
                raise ValueError(
                    "Color pair is greater than COLOR_PAIRS-1 (7)."
                )

        tui = kanban_tui.KanbanTui(
            FakeScreen(),
            single=True,
            refresh_interval=60,
            context={},
            get_board=lambda: {"tasks": []},
            get_task=lambda _task_id: {},
            theme="dark",
        )
        with mock.patch.object(kanban_tui.curses, "has_colors", return_value=True), \
                mock.patch.object(kanban_tui.curses, "use_default_colors"), \
                mock.patch.object(
                    kanban_tui.curses, "init_pair", side_effect=limited_init_pair
                ), \
                mock.patch.object(
                    kanban_tui.curses,
                    "color_pair",
                    side_effect=lambda index: index << 8,
                ):
            tui._init_style()
        # 第 8 对 (accent) 创建失败, 已建的状态色保留, accent 回退到属性 0.
        self.assertNotIn("accent", tui.colors)
        self.assertNotEqual(0, tui.colors["trash"])
        self.assertEqual(curses.A_REVERSE, tui._footer_attr())

    def test_tui_explicit_theme_works_without_default_colors(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)
        import curses
        from unittest import mock

        class FakeScreen:
            def __init__(self):
                self.background = None

            def getmaxyx(self):
                return 24, 100

            def bkgd(self, _char, attr=0):
                self.background = attr

        pairs = {}
        screen = FakeScreen()
        tui = kanban_tui.KanbanTui(
            screen,
            single=True,
            refresh_interval=60,
            context={},
            get_board=lambda: {"tasks": []},
            get_task=lambda _task_id: {},
            theme="dark",
        )
        with mock.patch.object(kanban_tui.curses, "has_colors", return_value=True), \
                mock.patch.object(
                    kanban_tui.curses,
                    "use_default_colors",
                    side_effect=curses.error("default colors unsupported"),
                ), \
                mock.patch.object(
                    kanban_tui.curses,
                    "init_pair",
                    side_effect=lambda index, fg, bg: pairs.__setitem__(
                        index, (fg, bg)
                    ),
                ), \
                mock.patch.object(
                    kanban_tui.curses,
                    "color_pair",
                    side_effect=lambda index: index << 8,
                ):
            tui._init_style()
            # 显式主题不依赖默认色扩展, 固定背景色照常初始化.
            self.assertFalse(tui.has_default_colors)
            self.assertNotEqual(0, tui.colors["working"])
            self.assertTrue(
                all(bg == curses.COLOR_BLACK for _fg, bg in pairs.values())
            )
            self.assertEqual(tui.colors["text"], screen.background)

            # auto 需要默认色扩展, 不可用时回退纯属性渲染并清除窗口背景.
            pairs.clear()
            tui.theme = "auto"
            tui._apply_theme()
            self.assertEqual({}, tui.colors)
            self.assertEqual({}, pairs)
            self.assertEqual(0, screen.background)

    def test_tui_board_height_boundary_keeps_footer_off_cards(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)

        class FakeScreen:
            def __init__(self, height, width):
                self.height = height
                self.width = width
                self.writes = []

            def getmaxyx(self):
                return self.height, self.width

            def addstr(self, y, x, text, attr=0):
                self.writes.append((y, x, text))

            def move(self, y, x):
                pass

        context = {
            "too_small": "Terminal is too small.",
            "state_labels": {state: state for state in STATES},
            "size_labels": {"small": "small", "large": "large"},
        }
        board = {
            "tasks": [
                {
                    "task_id": "20260820-boundary-task",
                    "title": "Boundary",
                    "state": "backlog",
                    "type": "chore",
                    "kind": "small",
                    "assignee": "codex",
                    "time": "08-20 23:00",
                }
            ],
        }

        short = FakeScreen(7, 40)
        tui = kanban_tui.KanbanTui(
            short,
            single=True,
            refresh_interval=60,
            context=context,
            get_board=lambda: board,
            get_task=lambda _task_id: {},
        )
        tui.model.set_board(board)
        tui._render_board()
        self.assertTrue(
            any("Terminal is too small." in text for _y, _x, text in short.writes)
        )

        tall = FakeScreen(8, 40)
        tui = kanban_tui.KanbanTui(
            tall,
            single=True,
            refresh_interval=60,
            context=context,
            get_board=lambda: board,
            get_task=lambda _task_id: {},
        )
        tui.model.set_board(board)
        tui._render_board()
        card_rows = [y for y, _x, text in tall.writes if "codex" in text]
        footer_rows = [y for y, _x, text in tall.writes if y == 7]
        # 8 行时卡片末行 (元信息) 在第 6 行, 页脚独占第 7 行.
        self.assertEqual([6], card_rows)
        self.assertTrue(footer_rows)
        self.assertFalse(any(y == 7 for y in card_rows))

    def test_tui_text_helpers_handle_wide_characters(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)

        self.assertEqual(4, kanban_tui.display_width("任务"))
        self.assertEqual("任...", kanban_tui.clip_text("任务标题", 5))
        self.assertEqual("08-21 18:00", kanban_tui.compact_time("2026-08-21 18:00"))
        self.assertEqual("-", kanban_tui.compact_time("-"))
        self.assertEqual("08-21 18:00", kanban_tui.compact_time("08-21 18:00"))
        self.assertEqual(
            "terminal-group", kanban_tui.compact_group("20260820-terminal-group")
        )
        self.assertEqual("custom-group", kanban_tui.compact_group("custom-group"))
        self.assertEqual("A B C", kanban_tui.clip_text("A\rB\x1bC", 10))
        self.assertEqual(["任务", "标题"], kanban_tui.wrap_text("任务标题", 4))
        task = {
            "title": "Alpha",
            "task_id": "id",
            "task_group": "group-one",
            "type": "Feature",
            "assignee": "Codex",
            "state": "todo",
        }
        for keyword in ("alpha", "ID", "group-one", "feature", "codex", "todo"):
            self.assertTrue(kanban_tui.task_matches(task, keyword))
        self.assertFalse(kanban_tui.task_matches(task, "missing"))

    def test_tui_narrow_toolbar_and_detail_keep_errors_visible(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)

        class FakeScreen:
            def __init__(self, height: int, width: int) -> None:
                self.height = height
                self.width = width
                self.writes = []

            def getmaxyx(self):
                return self.height, self.width

            def addstr(self, y, x, text, attr=0):
                self.writes.append((y, x, text, attr))

            def move(self, y, x):
                self.cursor = (y, x)

        context = {
            "title": "Task Board",
            "search": "Search",
            "active": "active columns",
            "updated": "Updated",
            "error": "Load failed",
            "state_labels": {state: state for state in STATES},
            "size_labels": {"small": "small", "large": "large"},
        }
        for width in (40, 50):
            screen = FakeScreen(24, width)
            tui = kanban_tui.KanbanTui(
                screen,
                single=True,
                refresh_interval=60,
                context=context,
                get_board=lambda: {"tasks": []},
                get_task=lambda _task_id: {},
            )
            tui.model.set_board({
                "generated_at": "2026-08-20 22:30:00",
                "tasks": [],
            })
            tui.model.query = "needle"
            tui.searching = True
            tui._render_board()
            toolbar_writes = [write for write in screen.writes if write[0] == 1]
            search_write, status_write = toolbar_writes
            self.assertIn("n", search_write[2])
            self.assertLessEqual(
                search_write[1] + kanban_tui.display_width(search_write[2]),
                status_write[1],
            )

        detail_screen = FakeScreen(12, 40)
        tui = kanban_tui.KanbanTui(
            detail_screen,
            single=True,
            refresh_interval=60,
            context=context,
            get_board=lambda: {"tasks": []},
            get_task=lambda _task_id: {},
        )
        tui.detail = {
            "task_id": "20260820-detail-task",
            "title": "Detail",
            "state": "todo",
            "kind": "small",
            "document": "# Detail\n\nBody",
        }
        tui.model.refresh_error = "board unavailable"
        tui._render_detail()
        footer = next(write for write in detail_screen.writes if write[0] == 11)
        self.assertIn("Load failed: board unavailable", footer[2])

        controller = kanban_tui.KanbanTui(
            detail_screen,
            single=True,
            refresh_interval=60,
            context=context,
            get_board=lambda: (_ for _ in ()).throw(
                UnicodeError("other card is invalid UTF-8")
            ),
            get_task=lambda task_id: {
                "task_id": task_id,
                "title": "Readable task",
                "document": "# Readable task",
            },
        )
        controller.model.set_board({
            "tasks": [
                {
                    "task_id": "20260820-readable-task",
                    "title": "Readable task",
                    "state": "backlog",
                }
            ],
        })
        controller._refresh()
        controller._open_detail()
        self.assertEqual(
            "other card is invalid UTF-8", controller.model.refresh_error
        )
        self.assertEqual(
            "other card is invalid UTF-8", controller.model.error
        )
        controller.get_board = lambda: {"tasks": []}
        controller._refresh()
        self.assertEqual("", controller.model.refresh_error)
        self.assertEqual("", controller.model.error)

    def test_tui_refresh_keeps_selection_scroll_and_updates_detail_inplace(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)

        def make_tasks(task_ids: list[str], *, title_prefix: str = "") -> list[dict]:
            return [
                {
                    "task_id": task_id,
                    "title": f"{title_prefix}{task_id}",
                    "state": "todo",
                }
                for task_id in task_ids
            ]

        task_ids = [f"20260820-keep-{index:02d}-task" for index in range(8)]
        model = kanban_tui.BoardModel()
        self.assertTrue(
            model.set_board({"generated_at": "t1", "tasks": make_tasks(task_ids)})
        )
        model.column_index = model.states.index("todo")
        model.selected_ids["todo"] = task_ids[4]
        model.selected_indexes["todo"] = 4
        model.scrolls["todo"] = 2

        self.assertTrue(
            model.set_board({
                "generated_at": "t2",
                "tasks": make_tasks(task_ids, title_prefix="updated-"),
            })
        )
        self.assertEqual(task_ids[4], model.selected_ids["todo"])
        self.assertEqual(4, model.selected_indexes["todo"])
        self.assertEqual(2, model.scrolls["todo"])

        remaining = task_ids[:4] + task_ids[5:]
        self.assertTrue(model.set_board({
            "generated_at": "t3",
            "tasks": make_tasks(remaining),
        }))
        self.assertEqual(task_ids[5], model.selected_ids["todo"])
        self.assertEqual(4, model.selected_indexes["todo"])
        self.assertEqual(2, model.scrolls["todo"])

        self.assertFalse(model.set_board({
            "generated_at": "t4",
            "tasks": make_tasks(remaining),
        }))
        self.assertEqual("t4", model.generated_at)
        self.assertFalse(model.set_board({
            "generated_at": "t4",
            "tasks": make_tasks(remaining),
        }))
        self.assertEqual(task_ids[5], model.selected_ids["todo"])
        self.assertEqual(2, model.scrolls["todo"])

        class FakeScreen:
            def getmaxyx(self):
                return 24, 80

        details = [
            {
                "task_id": "20260820-keep-task",
                "title": "Keep",
                "document": "one\n" * 8,
                "state": "todo",
            },
            {
                "task_id": "20260820-keep-task",
                "title": "Keep",
                "document": "two\n" * 8,
                "state": "todo",
            },
        ]
        boards = [
            {
                "generated_at": "d1",
                "tasks": [{
                    "task_id": "20260820-keep-task",
                    "title": "Keep",
                    "state": "todo",
                }],
            },
            {
                "generated_at": "d2",
                "tasks": [{
                    "task_id": "20260820-keep-task",
                    "title": "Keep changed",
                    "state": "todo",
                }],
            },
        ]
        tui = kanban_tui.KanbanTui(
            FakeScreen(),
            single=True,
            refresh_interval=30,
            context={},
            get_board=lambda: boards[1],
            get_task=lambda _task_id: details[1],
        )
        tui.model.set_board(boards[0])
        tui.detail = details[0]
        tui.detail_scroll = 3
        self.assertTrue(tui._refresh())
        self.assertEqual("two\n" * 8, tui.detail["document"])
        self.assertEqual(3, tui.detail_scroll)
        self.assertEqual("20260820-keep-task", tui.model.selected_ids["todo"])

    def test_tui_render_updates_inplace_without_erase(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)

        class FakeScreen:
            def __init__(self) -> None:
                self.writes = []
                self.erased = 0

            def getmaxyx(self):
                return 24, 80

            def erase(self):
                self.erased += 1

            def addstr(self, y, x, text, attr=0):
                self.writes.append((y, x, text, attr))

            def refresh(self):
                pass

            def move(self, y, x):
                pass

        context = {
            "title": "Task Board",
            "search": "Search",
            "active": "active",
            "updated": "Updated",
            "state_labels": {state: state for state in STATES},
            "size_labels": {"small": "small", "large": "large"},
            "help": "help",
        }
        first_board = {
            "generated_at": "2026-08-21 00:00:00",
            "tasks": [{
                "task_id": "20260821-inplace-task",
                "title": "Old title",
                "state": "backlog",
                "type": "chore",
                "kind": "small",
                "assignee": "codex",
                "time": "08-21 00:00",
            }],
        }
        screen = FakeScreen()
        tui = kanban_tui.KanbanTui(
            screen,
            single=True,
            refresh_interval=30,
            context=context,
            get_board=lambda: first_board,
            get_task=lambda _task_id: {},
        )
        tui.model.set_board(first_board)
        tui._render(force=True)
        self.assertEqual(0, screen.erased)
        first_writes = list(screen.writes)
        self.assertTrue(any("Old title" in text for _y, _x, text, _attr in first_writes))

        screen.writes.clear()
        tui._render()
        self.assertEqual([], screen.writes)
        self.assertEqual(0, screen.erased)

        next_board = {
            "generated_at": "2026-08-21 00:00:30",
            "tasks": [{
                **first_board["tasks"][0],
                "title": "New title",
            }],
        }
        self.assertTrue(tui.model.set_board(next_board))
        tui._render()
        self.assertEqual(0, screen.erased)
        self.assertTrue(any("New" in text for _y, _x, text, _attr in screen.writes))
        self.assertFalse(any("Old" in text for _y, _x, text, _attr in screen.writes))
        self.assertLess(len(screen.writes), len(first_writes))

    def test_tui_auto_refresh_runs_after_interval(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)
        import curses

        class FakeScreen:
            def __init__(self) -> None:
                self.keys = [curses.error("timeout"), "q"]

            def getmaxyx(self):
                return 24, 80

            def keypad(self, _enabled):
                pass

            def timeout(self, _milliseconds):
                pass

            def get_wch(self):
                key = self.keys.pop(0)
                if isinstance(key, Exception):
                    raise key
                return key

            def addstr(self, y, x, text, attr=0):
                pass

            def refresh(self):
                pass

            def move(self, y, x):
                pass

            def bkgd(self, *_args):
                pass

        calls = {"board": 0}

        def get_board() -> dict:
            calls["board"] += 1
            return {"generated_at": str(calls["board"]), "tasks": []}

        tui = kanban_tui.KanbanTui(
            FakeScreen(),
            single=True,
            refresh_interval=30,
            context={"title": "Task Board"},
            get_board=get_board,
            get_task=lambda _task_id: {},
        )
        tui.last_refresh = 0
        renders = {"count": 0}
        original_render = tui._render

        def counted_render(*, force: bool = False) -> None:
            renders["count"] += 1
            original_render(force=force)

        with mock.patch.object(kanban_tui.KanbanTui, "_init_style"), mock.patch.object(
            tui, "_render", side_effect=counted_render
        ), mock.patch.object(
            kanban_tui.time, "monotonic", side_effect=[30, 30, 31]
        ):
            tui.run({"generated_at": "0", "tasks": []})
        self.assertEqual(1, calls["board"])
        self.assertEqual(1, renders["count"])
        self.assertFalse(tui.running)

    def test_tui_single_mode_searches_opens_detail_and_quits_on_a_pty(self) -> None:
        self.make_todo("tui-pty")
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
        process = subprocess.Popen(
            [
                sys.executable,
                str(COMMAND),
                "tui",
                "--single",
                "--refresh",
                "1",
            ],
            env={**self.env, "TERM": "xterm-256color"},
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
        )
        os.close(slave)
        try:
            os.write(master, b"l/tui-pty\n\njqarq")
            returncode = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
            self.fail("TUI did not exit after q")
        finally:
            os.close(master)
        self.assertEqual(0, returncode)

    def test_web_task_group_supports_new_legacy_and_missing_metadata(self) -> None:
        task_id, task = self.make_todo("web-task-group")
        sys.path.insert(0, str(COMMAND.parent))
        try:
            kanban = runpy.run_path(str(COMMAND), run_name="kanban_web_group_test")
        finally:
            sys.path.pop(0)

        current = task.read_text(encoding="utf-8")
        missing = current.replace("- 任务组:\n", "", 1)
        task.write_text(missing, encoding="utf-8")
        payload = kanban["web_board_payload"](self.root)
        self.assertEqual("", payload["tasks"][0]["task_group"])

        legacy_group = "20260820-legacy-web-group"
        legacy = missing.replace(
            "## 讨论与决策\n\n",
            f"## 讨论与决策\n\n任务组: {legacy_group}\n前置任务: N/A\n\n",
            1,
        )
        task.write_text(legacy, encoding="utf-8")
        payload = kanban["web_task_payload"](self.root, task_id)
        self.assertEqual(legacy_group, payload["task_group"])

        current_group = "20260820-current-web-group"
        current_with_legacy = current.replace(
            "- 任务组:\n", f"- 任务组: {current_group}\n", 1
        ).replace(
            "## 讨论与决策\n\n",
            f"## 讨论与决策\n\n任务组: {legacy_group}\n前置任务: N/A\n\n",
            1,
        )
        task.write_text(current_with_legacy, encoding="utf-8")
        payload = kanban["web_task_payload"](self.root, task_id)
        self.assertEqual(current_group, payload["task_group"])

    def test_web_task_group_card_renders_as_badge_after_task_id(self) -> None:
        script = (PROJECT_ROOT / "share" / "kanban-web" / "board.js").read_text(
            encoding="utf-8"
        )
        css = (PROJECT_ROOT / "share" / "kanban-web" / "board.css").read_text(
            encoding="utf-8"
        )
        title_at = script.index('makeElement("p", "task-title")')
        task_id_at = script.index('makeElement("p", "task-id")')
        group_at = script.index('makeElement("span", "badge task-group")')
        self.assertLess(title_at, task_id_at)
        self.assertLess(task_id_at, group_at)
        self.assertIn("taskGroup.hidden = !task.task_group", script)
        self.assertIn("task.task_group,", script)

        group_style = re.search(r"\.badge\.task-group\s*\{([^}]+)\}", css)
        self.assertIsNotNone(group_style)
        group_css = group_style.group(1)
        self.assertIn("overflow-wrap: anywhere", group_css)
        self.assertIn("max-width: 100%", group_css)
        self.assertIn("width: fit-content", group_css)
        self.assertIn("[hidden]", css)
        self.assertIn("display: none !important", css)
        # MD2 chips 用 stadium 形状; 任务组徽章保留小圆角以容纳换行.
        self.assertIn("border-radius: 999px", css)
        self.assertIn("border-radius: var(--radius)", group_css)

    def test_web_sse_only_publishes_content_changes(self) -> None:
        import queue

        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_web
        finally:
            sys.path.pop(0)

        state = {"generated": 0, "title": "first"}

        def get_board():
            state["generated"] += 1
            return {
                "generated_at": str(state["generated"]),
                "tasks": [{"task_id": "task", "title": state["title"]}],
            }

        server = kanban_web.KanbanWebServer(
            ("127.0.0.1", 0),
            PROJECT_ROOT / "share" / "kanban-web",
            {},
            get_board,
            lambda _task_id: {},
        )
        subscriber = None
        try:
            server._refresh_board(force=True)
            subscriber = server.subscribe()
            event_name, revision, initial = subscriber.get_nowait()
            self.assertEqual(("board", 1, "first"), (
                event_name,
                revision,
                initial["tasks"][0]["title"],
            ))

            server._refresh_board()
            self.assertEqual("2", server.current_board()["generated_at"])
            with self.assertRaises(queue.Empty):
                subscriber.get_nowait()

            state["title"] = "second"
            server._refresh_board()
            event_name, revision, changed = subscriber.get_nowait()
            self.assertEqual(("board", 2, "second"), (
                event_name,
                revision,
                changed["tasks"][0]["title"],
            ))
        finally:
            if subscriber is not None:
                server.unsubscribe(subscriber)
            server.server_close()

    def test_web_serves_board_and_refreshes_task_state(self) -> None:
        import json
        import signal
        import socket
        import time
        import urllib.error
        import urllib.request

        def read_sse_event(response):
            event_name = ""
            data_lines = []
            while True:
                raw_line = response.readline()
                self.assertTrue(raw_line, "SSE stream closed before an event")
                line = raw_line.decode("utf-8").rstrip("\r\n")
                if not line:
                    if data_lines:
                        return event_name, json.loads("\n".join(data_lines))
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    event_name = line.partition(":")[2].strip()
                elif line.startswith("data:"):
                    data_lines.append(line.partition(":")[2].lstrip())

        task_id, _path = self.make_todo("web-board")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]

        process = subprocess.Popen(
            [
                sys.executable,
                str(COMMAND),
                "web",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--refresh",
                "1",
                "--assets",
                str(PROJECT_ROOT / "share" / "kanban-web"),
            ],
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.time() + 5
            started = False
            while time.time() < deadline:
                if process.poll() is not None:
                    err = process.stderr.read() if process.stderr else ""
                    self.fail(err or "web exited early")
                line = process.stdout.readline() if process.stdout else ""
                if not line:
                    time.sleep(0.05)
                    continue
                if f"http://127.0.0.1:{port}/" in line:
                    started = True
                    break
            self.assertTrue(started, "web server did not print listen URL")

            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                html = response.read().decode("utf-8")
            self.assertIn("任务看板", html)
            self.assertIn("SSE", html)

            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/static/board.js", timeout=2
            ) as response:
                script = response.read().decode("utf-8")
            self.assertIn("/api/board", script)
            self.assertIn('new EventSource("/api/events")', script)
            self.assertIn("insertBefore", script)
            self.assertNotIn("boardEl.innerHTML", script)
            self.assertNotIn("setInterval", script)
            self.assertIn("KanbanMarkdown.renderMarkdown", script)
            self.assertIn('makeElement("span", "badge task-group")', script)
            self.assertIn("taskGroup.hidden = !task.task_group", script)

            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/static/markdown.js", timeout=2
            ) as response:
                markdown_js = response.read().decode("utf-8")
            self.assertIn("function renderMarkdown", markdown_js)

            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                page = response.read().decode("utf-8")
            self.assertIn("/static/markdown.js", page)

            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/board", timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(1, len(payload["tasks"]))
            self.assertEqual(task_id, payload["tasks"][0]["task_id"])
            self.assertEqual("todo", payload["tasks"][0]["state"])

            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/events", timeout=4
            ) as events:
                event_name, initial = read_sse_event(events)
                self.assertEqual("board", event_name)
                self.assertEqual("todo", initial["tasks"][0]["state"])

                self.run_command("move", task_id, "working")
                event_name, refreshed = read_sse_event(events)
                self.assertEqual("board", event_name)
                self.assertEqual("working", refreshed["tasks"][0]["state"])

                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/tasks/{task_id}", timeout=2
                ) as response:
                    detail = json.loads(response.read().decode("utf-8"))
                self.assertEqual(task_id, detail["task_id"])
                self.assertIn("# ", detail["document"])

                task_path = self.root / "working" / f"{task_id}.md"
                task_path.write_bytes(b"\xff")
                event_name, error_event = read_sse_event(events)
                self.assertEqual("board-error", event_name)
                self.assertIn("UTF-8", error_event["error"])

                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/board", timeout=2
                    )
                error_response = caught.exception
                try:
                    self.assertEqual(400, error_response.code)
                    error_payload = json.loads(error_response.read().decode("utf-8"))
                    self.assertIn("UTF-8", error_payload["error"])
                finally:
                    error_response.close()
        finally:
            process.send_signal(signal.SIGINT)
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=3)

    def test_web_rejects_invalid_port(self) -> None:
        result = self.run_command("web", "--port", "70000", succeeds=False)
        self.assertRegex(result.stderr.lower(), r"invalid port|无效|port")


if __name__ == "__main__":
    unittest.main()
