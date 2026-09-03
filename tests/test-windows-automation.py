#!/usr/bin/env python3

"""Windows 自动化文档的 PowerShell 路径与 argv 边界测试."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
GIT_RULES = PROJECT_ROOT / "rules" / "GIT-RULES.md"
REVIEW_RULES = PROJECT_ROOT / "rules" / "REVIEW-RULES.md"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh.exe")


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "PowerShell on Windows only")
class WindowsAutomationDocumentationTest(unittest.TestCase):
    def test_published_commands_expand_userprofile_before_native_argv(self) -> None:
        git_rules = GIT_RULES.read_text(encoding="utf-8")
        review_rules = REVIEW_RULES.read_text(encoding="utf-8")
        self.assertIn(
            '& "$env:USERPROFILE\\.local\\bin\\merge-worktree-memory.cmd"',
            git_rules,
        )
        self.assertIn("不得固定假设 `py -3` 可用", git_rules)
        self.assertIn("系统 py.exe -3", git_rules)
        self.assertIn("PATH 中 python.exe", git_rules)
        self.assertIn("PATH 中 python3.exe", git_rules)
        self.assertIn("0 字节 WindowsApps 别名", git_rules)
        self.assertIn(
            '& "$env:USERPROFILE\\.local\\bin\\onevoke-review.cmd"',
            review_rules,
        )
        self.assertIn("进程 API 的 argv 数组", review_rules)
        self.assertIn('Path.home() / ".local/bin/onevoke"', review_rules)
        self.assertNotIn("py -3 -X utf8 ~/.local", git_rules + review_rules)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile & percent % bang ! 空格"
            script = profile / ".local" / "bin" / "merge-worktree-memory.py"
            script.parent.mkdir(parents=True)
            shutil.copy2(
                PROJECT_ROOT / "bin" / "merge-worktree-memory.cmd",
                script.with_suffix(".cmd"),
            )
            script.write_text(
                "import json, os, sys\n"
                "from pathlib import Path\n"
                "Path(os.environ['ONEVOKE_ARGV_LOG']).write_text("
                "json.dumps(sys.argv[1:], ensure_ascii=False), encoding='utf-8')\n",
                encoding="utf-8",
            )
            argument_log = root / "argv.json"
            expected = "ordinary worktree 空格"
            environment = os.environ.copy()
            environment.update(
                {
                    "USERPROFILE": str(profile),
                    "ONEVOKE_ARGV_LOG": str(argument_log),
                    "ONEVOKE_TEST_ARGUMENT": expected,
                    "ONEVOKE_PYTHON": sys.executable,
                }
            )
            command = (
                '& "$env:USERPROFILE\\.local\\bin\\merge-worktree-memory.cmd" '
                '--source $env:ONEVOKE_TEST_ARGUMENT'
            )
            result = subprocess.run(
                [str(POWERSHELL), "-NoProfile", "-Command", command],
                env=environment,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(
                ["--source", expected],
                json.loads(argument_log.read_text(encoding="utf-8")),
            )

            arbitrary = 'goal & value | 50% ! "quoted" tail\\'
            direct = subprocess.run(
                [sys.executable, "-X", "utf8", str(script), arbitrary],
                env=environment,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, direct.returncode, direct.stderr)
            self.assertEqual(
                [arbitrary], json.loads(argument_log.read_text(encoding="utf-8"))
            )


if __name__ == "__main__":
    unittest.main()
