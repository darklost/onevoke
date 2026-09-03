#!/usr/bin/env python3

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = PROJECT_ROOT / "bin"
sys.path.insert(0, str(BIN_DIR))

import onevoke_process as process


class AgentProgramTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_posix_program_uses_native_argv(self) -> None:
        program = process.AgentProgram("/usr/bin/codex")
        invocation = process.process_invocation(
            program,
            ["--model", "model with spaces"],
            {"PATH": "/usr/bin"},
        )
        self.assertEqual(
            ("/usr/bin/codex", "--model", "model with spaces"),
            invocation.argv,
        )
        self.assertEqual("/usr/bin", invocation.environment["PATH"])

    def test_windows_lookup_skips_empty_exe_and_accepts_batch(self) -> None:
        first = self.root / "first"
        second = self.root / "second"
        first.mkdir()
        second.mkdir()
        (first / "claude.exe").write_bytes(b"")
        batch = second / "claude.cmd"
        batch.write_text("@echo off\r\n", encoding="utf-8")
        with (
            mock.patch.object(process, "_is_windows", return_value=True),
            mock.patch.dict(os.environ, {"PATH": f"{first};{second}"}, clear=False),
        ):
            program = process.resolve_agent_program("claude")
        self.assertEqual(process.AgentProgram(str(batch), batch=True), program)

    def test_windows_lookup_prefers_later_native_exe(self) -> None:
        first = self.root / "first"
        second = self.root / "second"
        first.mkdir()
        second.mkdir()
        (first / "codex.cmd").write_text("@echo off\r\n", encoding="utf-8")
        native = second / "codex.exe"
        native.write_bytes(b"MZ")
        with (
            mock.patch.object(process, "_is_windows", return_value=True),
            mock.patch.dict(os.environ, {"PATH": f"{first};{second}"}, clear=False),
        ):
            program = process.resolve_agent_program("codex")
        self.assertEqual(process.AgentProgram(str(native)), program)

    def test_batch_command_contains_only_generated_references(self) -> None:
        program = process.AgentProgram(r"C:\Agents & Tools\codex.cmd", batch=True)
        argument = 'model %NAME%! & echo "unexpected"'
        environment = {"COMSPEC": r"C:\Windows\System32\cmd.exe"}
        with mock.patch.object(process, "_is_windows", return_value=True):
            invocation = process.process_invocation(program, ["--model", argument], environment)

        self.assertEqual(
            (r"C:\Windows\System32\cmd.exe", "/d", "/s", "/v:off", "/c"),
            invocation.argv[:5],
        )
        command = invocation.argv[5]
        self.assertNotIn(program.path, command)
        self.assertNotIn(argument, command)
        self.assertEqual(3, command.count("%") // 2)
        encoded = [
            value
            for key, value in invocation.environment.items()
            if key.startswith("ONEVOKE_CMD_")
        ]
        self.assertEqual(3, len(encoded))
        self.assertIn('"model %NAME%! & echo ""unexpected"""', encoded)

    def test_batch_quote_handles_empty_quotes_and_trailing_backslash(self) -> None:
        self.assertEqual('""', process._quote_windows_batch_argument(""))
        self.assertEqual('"a""b"', process._quote_windows_batch_argument('a"b'))
        self.assertEqual(
            '"hello world\\\\"',
            process._quote_windows_batch_argument("hello world\\"),
        )

    def test_batch_ignores_relative_comspec(self) -> None:
        program = process.AgentProgram(r"C:\Agents\codex.cmd", batch=True)
        environment = {"COMSPEC": "cmd.exe", "SystemRoot": r"D:\Windows"}
        with mock.patch.object(process, "_is_windows", return_value=True):
            invocation = process.process_invocation(program, (), environment)
        self.assertEqual(r"D:\Windows\System32\cmd.exe", invocation.argv[0])

    def test_task_file_contains_body_and_best_effort_delete_instruction(self) -> None:
        path = process.create_task_file("line one\nline two", prefix="onevoke-test-")
        try:
            content = path.read_text(encoding="utf-8")
            self.assertTrue(content.startswith("line one\nline two\n\n"))
            self.assertIn(str(path), content)
            self.assertIn("删除失败或文件遗留不影响任务结果", content)
        finally:
            path.unlink(missing_ok=True)

    def test_task_file_pointer_is_one_short_instruction(self) -> None:
        path = self.root / "task.md"
        instruction = process.task_file_instruction("Execute this task.", path)
        self.assertEqual(2, instruction.count(";"))
        self.assertNotIn("\n", instruction)
        self.assertIn(str(path), instruction)
        self.assertTrue(instruction.endswith("exactly."))


if __name__ == "__main__":
    unittest.main()
