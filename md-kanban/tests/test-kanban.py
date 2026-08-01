#!/usr/bin/env python3

import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Optional


COMMAND = Path(
    os.environ.get("KANBAN_COMMAND", Path.home() / ".local" / "bin" / "kanban")
).resolve()
PROJECT_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = PROJECT_ROOT / "install.sh"
RULES = PROJECT_ROOT / "KANBAN-RULES.md"
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
        for name in ("codex", "claude"):
            agent = fake_bin / name
            agent.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            agent.chmod(0o755)
        self.env["PATH"] = str(fake_bin) + os.pathsep + self.env.get("PATH", "")
        self.env["TMUX"] = "/tmp/fake-tmux,1,0"
        self.env["TMUX_PANE"] = "%7"
        self.env["KANBAN_TMUX_LOG"] = str(self.root / "tmux.log")
        return fake_bin

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
        self.run_command("move", large_id, "done", succeeds=False)
        (spec.parent / "report.md").write_text("# 完成报告\n\n验证通过.\n", encoding="utf-8")
        self.run_command("move", large_id, "done")

        listing = self.run_command("list", "done").stdout
        self.assertIn(small_id, listing)
        self.assertIn(large_id, listing)
        self.assertEqual("ok: 2 tasks\n", self.run_command("check").stdout)

    def test_start_moves_task_and_launches_agent_window(self) -> None:
        task_id, task = self.make_todo("start-direct")
        fake_bin = self.install_fake_launchers()

        result = self.run_command("start", task_id)

        self.assertIn(f"started: {task_id}", result.stdout)
        started = self.root / "working" / task.name
        text = started.read_text(encoding="utf-8")
        self.assertIn("- 负责人: codex\n", text)
        self.assertRegex(text, r"(?m)^- 开始时间: \d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        tmux_args = (self.root / "tmux.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual("new-window", tmux_args[0])
        self.assertEqual("$42:", tmux_args[tmux_args.index("-t") + 1])
        self.assertEqual(str(self.root.resolve().parent), tmux_args[tmux_args.index("-c") + 1])
        self.assertEqual("kb-start-direct", tmux_args[tmux_args.index("-n") + 1])
        self.assertIn(str(fake_bin / "codex"), tmux_args[-1])
        self.assertIn(task_id, tmux_args[-1])

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

    def test_installer_copies_command_and_rules(self) -> None:
        install_home = self.root / "install-home"
        env = os.environ.copy()
        env["HOME"] = str(install_home)
        result = subprocess.run(
            ["sh", str(INSTALLER)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

        command = install_home / ".local" / "bin" / "kanban"
        rules = install_home / ".agents" / "KANBAN-RULES.md"
        self.assertTrue(os.access(command, os.X_OK))
        self.assertEqual(RULES.read_bytes(), rules.read_bytes())

        output = subprocess.run(
            [str(command), "rules"],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, output.returncode, output.stderr)
        self.assertEqual(RULES.read_text(encoding="utf-8"), output.stdout)


if __name__ == "__main__":
    unittest.main()
