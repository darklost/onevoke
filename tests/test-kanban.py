#!/usr/bin/env python3

import argparse
import io
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
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

    def write_onevoke_config(
        self, agent: str, launcher: str, *, welcome_complete: bool = True
    ) -> None:
        config = self.home / ".config" / "onevoke" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {
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
            ),
            encoding="utf-8",
        )

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
        self.assertEqual("ok: 2 tasks\n", self.run_command("check").stdout)

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

        self.assertEqual("STATE    SIZE   TIME  TASK ID / TITLE", plain.splitlines()[0])
        self.assertIn(f"backlog  small  -     {task_id}  表格输出", plain)
        self.assertIn(f"backlog  large  -     {large_id}  大型表格输出", plain)
        self.assertIn("\033[90mbacklog", output)
        self.assertIn("\033[90msmall", output)
        self.assertIn("\033[1;95mlarge", output)
        self.assertIn(f"\033[96m{task_id}", output)
        self.assertIn("\033[95m表格输出", output)
        self.assertNotIn("\t", output)

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
        self.assertIn("STATE", self.run_command("list", "done").stdout)

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

        self.assertIn(f"started: {task_id}", result.stdout)
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
        self.assertIn("agent=claude", result.stdout)
        command = (self.root / "tmux.log").read_text(encoding="utf-8").splitlines()[-1]
        self.assertIn("--model opus --effort medium", command)
        self.assertIn("--dangerously-skip-permissions", command)

    def test_start_with_grok_launches_bypass_permission_session(self) -> None:
        task_id, task = self.make_todo("start-grok")
        fake_bin = self.install_fake_launchers()

        result = self.run_command("start", "--agent", "grok", task_id)

        self.assertIn("agent=grok", result.stdout)
        started = self.root / "working" / task.name
        self.assertIn("- 负责人: grok\n", started.read_text(encoding="utf-8"))
        command = (self.root / "tmux.log").read_text(encoding="utf-8").splitlines()[-1]
        self.assertIn(str(fake_bin / "grok"), command)
        self.assertNotIn("--model", command)
        self.assertNotIn("effort", command)
        self.assertIn("--permission-mode bypassPermissions", command)
        self.assertIn(task_id, command)

        # 大任务同样不传推理强度: Grok CLI 不接受这个参数.
        large_id = f"{datetime.now().strftime('%Y%m%d')}-large-grok-task"
        self.run_command("new", "--large", "chore", "large-grok", "大型任务 grok")
        self.make_ready(self.root / "backlog" / large_id / "spec.md")
        self.run_command("pick", large_id)

        self.run_command("start", "--agent", "grok", large_id)

        large_command = (self.root / "tmux.log").read_text(encoding="utf-8").splitlines()[-1]
        self.assertNotIn("effort", large_command)
        self.assertIn("--permission-mode bypassPermissions", large_command)

    def test_start_uses_the_configured_default_agent(self) -> None:
        task_id, _ = self.make_todo("configured-agent")
        fake_bin = self.install_fake_launchers()
        self.write_onevoke_config("grok", "tmux")

        result = self.run_command("start", task_id)

        self.assertIn("agent=grok", result.stdout)
        command = (self.root / "tmux.log").read_text(encoding="utf-8").splitlines()[-1]
        self.assertIn(str(fake_bin / "grok"), command)
        self.assertIn("--permission-mode bypassPermissions", command)

    def test_start_ignores_unfinished_welcome_selections(self) -> None:
        task_id, _ = self.make_todo("unfinished-config")
        fake_bin = self.install_fake_launchers()
        self.write_onevoke_config("grok", "foreground", welcome_complete=False)

        result = self.run_command("start", task_id)

        self.assertIn("agent=codex", result.stdout)
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
            self.assertEqual("ok: 0 tasks\n", result.stdout)

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
                self.assertIn("rules: ", result.stdout)
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
        self.assertIn("checked: 0 valid, 1 invalid", result.stdout)
        self.assertNotIn("ok:", result.stdout)

    def test_check_passes_on_a_clean_board(self) -> None:
        self.make_todo("clean")

        result = self.run_command("check")

        self.assertEqual("ok: 1 tasks\n", result.stdout)

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
        self.assertIn("checked:", check.stdout)
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
        self.assertIn("onevoke 不在 PATH", result.stderr)
        self.assertIn("请在终端运行 onevoke welcome", result.stderr)

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

        output = subprocess.run(
            [str(command), "rules"],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, output.returncode, output.stderr)
        self.assertEqual(RULES.read_text(encoding="utf-8"), output.stdout)

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

    def test_installer_never_touches_the_users_own_agent_rules(self) -> None:
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

    def test_installer_rejects_arguments(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
