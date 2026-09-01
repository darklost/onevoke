#!/usr/bin/env python3

"""Windows console launcher 的领取、启动与失败回滚测试."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = PROJECT_ROOT / "bin"
KANBAN = BIN_DIR / "kanban"


def load_kanban_module():
    name = "kanban_windows_console_under_test"
    loader = importlib.machinery.SourceFileLoader(name, str(KANBAN))
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None:
        raise RuntimeError("无法加载 kanban 测试模块")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    sys.path.insert(0, str(BIN_DIR))
    try:
        loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


@unittest.skipUnless(os.name == "nt", "Windows console launcher only")
class WindowsConsoleLauncherTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.kanban = load_kanban_module()
        sys.path.insert(0, str(BIN_DIR))
        try:
            import onevoke_config
        finally:
            sys.path.pop(0)
        cls.onevoke_config = onevoke_config

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name) / "project"
        self.root = self.project / "kanban"
        for state in self.kanban.STATES:
            (self.root / state).mkdir(parents=True, exist_ok=True)
        self.task_id = "20260823-console-task"
        self.document = self.root / "todo" / f"{self.task_id}.md"
        self.original = (
            "# Windows console\n\n"
            "- 类型: Chore\n- 任务组:\n- 创建时间: 2026-08-23 00:00\n"
            "- 负责人:\n- 开始时间:\n- 完成时间:\n- 任务分支:\n- 结果:\n\n"
            "## 任务目标\n\ngoal\n\n## 用户决策\n\nN/A\n\n"
            "## 预期成果\n\nresult\n\n## 验收条件\n\n- [ ] accepted\n\n"
            "## 威胁模型\n\nN/A\n\n## 不在本轮范围\n\n- none\n\n"
            "## 讨论与决策\n\nN/A\n\n## 实施与验证\n\nN/A\n\n"
            "## 完成总结\n\nN/A\n"
        )
        self.document.write_text(self.original, encoding="utf-8")
        self.config = self.onevoke_config.default_config()
        self.config["welcome_complete"] = True
        self.config["kanban_agent"] = "codex"
        self.config["launcher"] = "console"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def start(self, popen) -> str:
        output = io.StringIO()
        with (
            mock.patch.object(self.kanban, "load_config", return_value=self.config),
            mock.patch.object(self.kanban.shutil, "which", return_value=r"C:\fake\codex.exe"),
            mock.patch.object(self.kanban.subprocess, "Popen", popen),
            redirect_stdout(output),
        ):
            self.kanban.command_start(
                Namespace(task=self.task_id, agent=None, launcher=None), self.root
            )
        return output.getvalue()

    def test_console_claims_task_and_returns_pid(self) -> None:
        process = mock.Mock(pid=4242)
        popen = mock.Mock(return_value=process)

        output = self.start(popen)

        working = self.root / "working" / self.document.name
        self.assertFalse(self.document.exists())
        self.assertTrue(working.is_file())
        text = working.read_text(encoding="utf-8")
        self.assertIn("- 负责人: codex", text)
        self.assertRegex(text, r"(?m)^- 开始时间: \d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        self.assertIn("启动方式=console", output)
        self.assertIn("PID=4242", output)
        arguments, options = popen.call_args
        self.assertEqual(self.project, options["cwd"])
        self.assertEqual(
            self.kanban.subprocess.CREATE_NEW_CONSOLE
            | self.kanban.subprocess.CREATE_NEW_PROCESS_GROUP,
            options["creationflags"],
        )
        self.assertEqual(r"C:\fake\codex.exe", arguments[0][0])

    def test_notify_payload_is_private_at_creation(self) -> None:
        path = self.kanban.write_notify_message("sensitive finding")
        try:
            self.assertEqual("sensitive finding\n", path.read_text(encoding="utf-8"))
            for target in (path.parent, path):
                acl = subprocess.run(
                    ["icacls.exe", str(target)],
                    text=True,
                    capture_output=True,
                    check=False,
                    encoding="utf-8",
                    errors="replace",
                )
                self.assertEqual(0, acl.returncode, acl.stderr)
                self.assertNotIn("(I)", acl.stdout)
                self.assertEqual(1, acl.stdout.count("(F)"), acl.stdout)
        finally:
            self.kanban.remove_notify_message(path)

    def test_notify_payload_rejects_reparse_temporary_root(self) -> None:
        actual = self.project / "actual-temp"
        actual.mkdir()
        junction = self.project / "junction-temp"
        linked = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(actual)],
            text=True,
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(0, linked.returncode, linked.stderr)
        try:
            with mock.patch.object(self.kanban, "notification_temp_root", return_value=junction):
                with self.assertRaises(self.kanban.UnsafePathError):
                    self.kanban.write_notify_message("sensitive finding")
            self.assertEqual([], list(actual.iterdir()))
        finally:
            os.rmdir(junction)

    def test_console_creation_failure_restores_todo_and_document(self) -> None:
        popen = mock.Mock(side_effect=OSError("cannot create console"))

        with self.assertRaises(self.kanban.KanbanError) as raised:
            self.start(popen)

        self.assertIn("cannot create console", str(raised.exception))
        self.assertTrue(self.document.is_file())
        self.assertEqual(self.original, self.document.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "working" / self.document.name).exists())

    def test_console_rejects_batch_agent_before_claiming_task(self) -> None:
        popen = mock.Mock()
        with (
            mock.patch.object(self.kanban, "load_config", return_value=self.config),
            mock.patch.object(self.kanban.shutil, "which", return_value=r"C:\fake\codex.cmd"),
            mock.patch.object(self.kanban.subprocess, "Popen", popen),
        ):
            with self.assertRaises(self.kanban.KanbanError) as raised:
                self.kanban.command_start(
                    Namespace(task=self.task_id, agent=None, launcher=None), self.root
                )

        self.assertIn(".exe", str(raised.exception))
        popen.assert_not_called()
        self.assertTrue(self.document.is_file())
        self.assertFalse((self.root / "working" / self.document.name).exists())

    def assert_tmux_launcher_rejected(self, launcher: str | None) -> None:
        popen = mock.Mock()
        which = mock.Mock(return_value=r"C:\fake\codex.exe")
        with (
            mock.patch.object(self.kanban, "load_config", return_value=self.config),
            mock.patch.object(self.kanban.shutil, "which", which),
            mock.patch.object(self.kanban.subprocess, "Popen", popen),
        ):
            with self.assertRaises(self.kanban.KanbanError) as raised:
                self.kanban.command_start(
                    Namespace(task=self.task_id, agent=None, launcher=launcher), self.root
                )

        self.assertIn("Windows", str(raised.exception))
        self.assertIn("console", str(raised.exception))
        which.assert_not_called()
        popen.assert_not_called()
        self.assertTrue(self.document.is_file())
        self.assertEqual(self.original, self.document.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "working" / self.document.name).exists())

    def test_windows_rejects_explicit_tmux_before_claiming_task(self) -> None:
        self.assert_tmux_launcher_rejected("tmux")

    def test_windows_rejects_configured_tmux_session_before_claiming_task(self) -> None:
        self.config["launcher"] = "tmux-session"
        self.assert_tmux_launcher_rejected(None)

    def test_windows_rejects_explicit_herdr_before_claiming_task(self) -> None:
        self.assert_tmux_launcher_rejected("herdr")

    def test_windows_rejects_configured_herdr_before_claiming_task(self) -> None:
        self.config["launcher"] = "herdr"
        self.assert_tmux_launcher_rejected(None)

    def test_windows_rejects_explicit_auto_before_claiming_task(self) -> None:
        self.assert_tmux_launcher_rejected("auto")

    def test_windows_rejects_configured_auto_before_claiming_task(self) -> None:
        self.config["launcher"] = "auto"
        self.assert_tmux_launcher_rejected(None)


if __name__ == "__main__":
    unittest.main()
