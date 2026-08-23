#!/usr/bin/env python3

"""原生 Windows Reviewer 门禁与 .cmd 入口测试."""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
REVIEWER = PROJECT_ROOT / "bin" / "onevoke-review.cmd"
REVIEW_IMPLEMENTATION = PROJECT_ROOT / "bin" / "onevoke_review.py"
ONEVOKE = PROJECT_ROOT / "bin" / "onevoke"

FAKE_REVIEWER = r'''from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

arguments = sys.argv[1:]
agent = os.environ["FAKE_AGENT"]
Path(os.environ["FAKE_ARGV"]).write_text(json.dumps(arguments), encoding="utf-8")
Path(os.environ["FAKE_CWD"]).write_text(str(Path.cwd()), encoding="utf-8")
Path(os.environ["FAKE_STATE_HOME"]).write_text(
    os.environ[{"codex": "CODEX_HOME", "claude": "CLAUDE_CONFIG_DIR", "grok": "GROK_HOME"}[agent]],
    encoding="utf-8",
)
prompt = sys.stdin.read()
if "--prompt-file" in arguments:
    prompt = Path(arguments[arguments.index("--prompt-file") + 1]).read_text(encoding="utf-8")
Path(os.environ["FAKE_PROMPT"]).write_text(prompt, encoding="utf-8")

tamper = os.environ.get("FAKE_TAMPER")
if tamper:
    Path(tamper).write_text("tampered\n", encoding="utf-8")
if os.environ.get("FAKE_SLEEP"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    Path(os.environ["FAKE_CHILD_PID"]).write_text(str(child.pid), encoding="ascii")
    time.sleep(60)
delayed_tamper = os.environ.get("FAKE_DELAYED_TAMPER")
if delayed_tamper:
    child = subprocess.Popen([
        sys.executable,
        "-c",
        (
            "import sys, time; from pathlib import Path; "
            "time.sleep(1); Path(sys.argv[1]).write_text('escaped\\n', encoding='utf-8')"
        ),
        delayed_tamper,
    ])
    Path(os.environ["FAKE_CHILD_PID"]).write_text(str(child.pid), encoding="ascii")

report = os.environ.get("FAKE_REPORT", "REPORT BODY")
if agent == "codex":
    output = Path(arguments[arguments.index("--output-last-message") + 1])
    output.write_text(report + "\n", encoding="utf-8")
elif agent == "claude":
    print(json.dumps({
        "type": "result", "subtype": "success", "is_error": False, "result": report
    }, ensure_ascii=False))
else:
    print(json.dumps({"stopReason": "end_turn", "text": report}, ensure_ascii=False))
'''

FAKE_EXE_PROXY = r'''using System;
using System.Diagnostics;
using System.Text;

public static class FakeReviewerProxy
{
    private static string Quote(string value)
    {
        StringBuilder result = new StringBuilder();
        result.Append('"');
        int backslashes = 0;
        foreach (char character in value)
        {
            if (character == '\\')
            {
                backslashes++;
            }
            else if (character == '"')
            {
                result.Append('\\', backslashes * 2 + 1);
                result.Append('"');
                backslashes = 0;
            }
            else
            {
                result.Append('\\', backslashes);
                result.Append(character);
                backslashes = 0;
            }
        }
        result.Append('\\', backslashes * 2);
        result.Append('"');
        return result.ToString();
    }

    public static int Main(string[] arguments)
    {
        string python = Environment.GetEnvironmentVariable("FAKE_REVIEW_PYTHON");
        string script = Environment.GetEnvironmentVariable("FAKE_REVIEW_SCRIPT");
        StringBuilder commandLine = new StringBuilder(Quote(script));
        foreach (string argument in arguments)
        {
            commandLine.Append(' ');
            commandLine.Append(Quote(argument));
        }
        ProcessStartInfo start = new ProcessStartInfo(python, commandLine.ToString());
        start.UseShellExecute = false;
        Process process = Process.Start(start);
        if (process == null)
        {
            return 127;
        }
        process.WaitForExit();
        return process.ExitCode;
    }
}
'''


@unittest.skipUnless(os.name == "nt", "仅在原生 Windows 运行")
class WindowsReviewGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if powershell is None:
            raise unittest.SkipTest("PowerShell is required to compile the transparent test proxy")
        cls.proxy_temporary = tempfile.TemporaryDirectory()
        proxy_root = Path(cls.proxy_temporary.name)
        source = proxy_root / "fake-reviewer-proxy.cs"
        cls.fake_executable = proxy_root / "fake-reviewer.exe"
        source.write_text(FAKE_EXE_PROXY, encoding="utf-8")
        environment = {
            **os.environ,
            "ONEVOKE_PROXY_SOURCE": str(source),
            "ONEVOKE_PROXY_OUTPUT": str(cls.fake_executable),
        }
        subprocess.run(
            [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                (
                    "$source = [IO.File]::ReadAllText($env:ONEVOKE_PROXY_SOURCE); "
                    "Add-Type -TypeDefinition $source -Language CSharp "
                    "-OutputAssembly $env:ONEVOKE_PROXY_OUTPUT -OutputType ConsoleApplication"
                ),
            ],
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.proxy_temporary.cleanup()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo R&D"
        self.runtime = self.root / "runtime"
        self.homes = {agent: self.root / agent for agent in ("codex", "claude", "grok")}
        self.repo.mkdir()
        self.runtime.mkdir()
        for home in self.homes.values():
            home.mkdir()

        self.fake_script = self.root / "fake-reviewer.py"
        self.fake_script.write_text(FAKE_REVIEWER, encoding="utf-8")
        self.fake_command = self.fake_executable
        self.argv_log = self.root / "argv.json"
        self.prompt_log = self.root / "prompt.txt"
        self.cwd_log = self.root / "cwd.txt"
        self.state_home_log = self.root / "state-home.txt"
        self.child_pid_log = self.root / "child.pid"

        self.git("init", "-q", "-b", "main")
        self.base = self.commit("base.txt", "base\n", "base")
        self.head = self.commit("head.txt", "head\n", "head")

        self.environment = os.environ.copy()
        self.environment.update(
            {
                "GIT_CEILING_DIRECTORIES": str(self.root),
                "TMPDIR": str(self.runtime),
                "TEMP": str(self.runtime),
                "TMP": str(self.runtime),
                "ONEVOKE_CONFIG": str(self.root / "config.json"),
                "ONEVOKE_LANG": "en",
                "ONEVOKE_PYTHON": sys.executable,
                "FAKE_REVIEW_PYTHON": sys.executable,
                "FAKE_REVIEW_SCRIPT": str(self.fake_script),
                "FAKE_ARGV": str(self.argv_log),
                "FAKE_PROMPT": str(self.prompt_log),
                "FAKE_CWD": str(self.cwd_log),
                "FAKE_STATE_HOME": str(self.state_home_log),
                "FAKE_CHILD_PID": str(self.child_pid_log),
                "CODEX_HOME": str(self.homes["codex"]),
                "CLAUDE_CONFIG_DIR": str(self.homes["claude"]),
                "GROK_HOME": str(self.homes["grok"]),
                "CODEX_REVIEW_BIN": str(self.fake_command),
                "CLAUDE_REVIEW_BIN": str(self.fake_command),
                "GROK_REVIEW_BIN": str(self.fake_command),
                "CODEX_REVIEW_CHECK_INTERVAL_SECONDS": "1",
                "CLAUDE_REVIEW_CHECK_INTERVAL_SECONDS": "1",
                "GROK_REVIEW_CHECK_INTERVAL_SECONDS": "1",
                "CODEX_REVIEW_MAX_RUNTIME_SECONDS": "30",
                "CLAUDE_REVIEW_MAX_RUNTIME_SECONDS": "30",
                "GROK_REVIEW_MAX_RUNTIME_SECONDS": "30",
            }
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(self, *arguments: str) -> str:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "-c",
                "user.name=test",
                "-c",
                "user.email=test@example.com",
                "-c",
                "commit.gpgsign=false",
                *arguments,
            ],
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
        )
        return result.stdout.strip()

    def commit(self, name: str, body: str, message: str) -> str:
        (self.repo / name).write_text(body, encoding="utf-8")
        self.git("add", name)
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def review(self, agent: str, **overrides: str) -> subprocess.CompletedProcess[str]:
        environment = {**self.environment, "FAKE_AGENT": agent, **overrides}
        return subprocess.run(
            [
                str(REVIEWER),
                agent,
                str(self.repo),
                self.base,
                self.head,
                "QA",
                "确认 Windows 审核正确",
            ],
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )

    def test_cmd_entry_runs_each_reviewer_with_isolation_contract(self) -> None:
        for agent in ("codex", "claude", "grok"):
            with self.subTest(agent=agent):
                result = self.review(agent, FAKE_REPORT=f"{agent} 审核完成")
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn(f"{agent} 审核完成", result.stdout)
                arguments = json.loads(self.argv_log.read_text(encoding="utf-8"))
                prompt = self.prompt_log.read_text(encoding="utf-8")
                self.assertIn("You are the QA review agent", prompt)
                self.assertIn("Authoritative task goal: 确认 Windows 审核正确", prompt)
                self.assertEqual(str(self.homes[agent]), self.state_home_log.read_text(encoding="utf-8"))
                if agent == "codex":
                    self.assertEqual("read-only", arguments[arguments.index("--sandbox") + 1])
                    self.assertIn("--ephemeral", arguments)
                    self.assertEqual(str(self.repo.resolve()), arguments[arguments.index("--cd") + 1])
                elif agent == "claude":
                    self.assertEqual("plan", arguments[arguments.index("--permission-mode") + 1])
                    self.assertEqual("Read,Grep,Glob", arguments[arguments.index("--tools") + 1])
                    self.assertIn("--safe-mode", arguments)
                    self.assertNotEqual(str(self.repo.resolve()), self.cwd_log.read_text(encoding="utf-8"))
                else:
                    self.assertEqual("read-only", arguments[arguments.index("--sandbox") + 1])
                    self.assertIn("--no-memory", arguments)
                    self.assertIn("--no-subagents", arguments)
                    self.assertNotIn("--model", arguments)

    def test_default_reviewer_home_uses_userprofile_instead_of_home(self) -> None:
        profile = self.root / "windows-profile"
        unrelated_home = self.root / "git-bash-home"
        environment = self.environment.copy()
        environment.pop("CODEX_HOME", None)
        environment.update({"USERPROFILE": str(profile), "HOME": str(unrelated_home)})
        code = (
            "import sys; "
            "sys.path.insert(0, sys.argv[1]); "
            "import onevoke_review; "
            "print(onevoke_review.agent_settings('codex').review_home)"
        )

        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", code, str(PROJECT_ROOT / "bin")],
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(str(profile / ".codex"), result.stdout.strip())

    def test_python_gate_preserves_metacharacters_quotes_and_backslashes(self) -> None:
        task_goal = 'R&D &|<>^%! "quoted" trailing\\'
        review_context = 'context &|<>^%! \\"nested\\" end\\\\'
        environment = {**self.environment, "FAKE_AGENT": "codex"}

        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-X",
                "utf8",
                str(REVIEW_IMPLEMENTATION),
                "codex",
                str(self.repo),
                self.base,
                self.head,
                "QA",
                task_goal,
                review_context,
            ],
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        prompt = self.prompt_log.read_text(encoding="utf-8")
        self.assertIn(f"Authoritative task goal: {task_goal}", prompt)
        self.assertIn(f"Additional caller-supplied review context: {review_context}", prompt)

    def test_cmd_reports_127_when_python_is_unavailable(self) -> None:
        environment = {
            **self.environment,
            "ONEVOKE_PYTHON": str(self.root / "missing-python.exe"),
        }
        result = subprocess.run(
            [str(REVIEWER)],
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        self.assertEqual(127, result.returncode)
        self.assertIn("Python 3 is required", result.stderr)

    def test_gate_rejects_batch_reviewer_without_executing_it(self) -> None:
        marker = self.root / "batch-executed.txt"
        batch = self.root / "fake-reviewer.cmd"
        batch.write_text(
            '@echo off\r\n> "%BATCH_MARKER%" echo executed\r\nexit /b 0\r\n',
            encoding="ascii",
        )

        result = self.review(
            "codex",
            CODEX_REVIEW_BIN=str(batch),
            BATCH_MARKER=str(marker),
        )

        self.assertEqual(127, result.returncode, result.stderr)
        self.assertIn("must be a native .exe", result.stderr)
        self.assertFalse(marker.exists())

    def test_doctor_does_not_execute_batch_agents_for_version_detection(self) -> None:
        batch_dir = self.root / "batch-agents"
        batch_dir.mkdir()
        untrusted_cwd = self.root / "untrusted R&D repository"
        untrusted_cwd.mkdir()
        shutil.copy2(self.fake_executable, untrusted_cwd / "codex.exe")
        marker = self.root / "agent-version-executed.txt"
        for agent in ("codex", "claude", "grok"):
            (batch_dir / f"{agent}.cmd").write_text(
                '@echo off\r\n> "%AGENT_MARKER%" echo executed\r\nexit /b 0\r\n',
                encoding="ascii",
            )
        config = {
            "schema_version": 1,
            "welcome_complete": True,
            "kanban_agent": "codex",
            "launcher": "console",
            "language": "en",
            "reviewers": {role: "codex" for role in ("PM", "CSA", "Hacker", "QA")},
            "memsearch": {"enabled": False},
        }
        Path(self.environment["ONEVOKE_CONFIG"]).write_text(json.dumps(config), encoding="utf-8")
        environment = {
            **self.environment,
            "PATH": str(batch_dir) + os.pathsep + self.environment["PATH"],
            "AGENT_MARKER": str(marker),
        }

        result = subprocess.run(
            [sys.executable, str(ONEVOKE), "doctor"],
            cwd=untrusted_cwd,
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )

        self.assertEqual(1, result.returncode)
        self.assertIn("not a native .exe", result.stderr)
        self.assertFalse(marker.exists())
        self.assertFalse(self.argv_log.exists(), "doctor must not execute a native image planted in cwd")

    def test_timeout_kills_the_entire_reviewer_process_tree(self) -> None:
        result = self.review(
            "claude",
            FAKE_SLEEP="1",
            CLAUDE_REVIEW_MAX_RUNTIME_SECONDS="1",
        )
        self.assertEqual(124, result.returncode, result.stderr)
        self.assertIn("Claude review exceeded 1 seconds", result.stderr)
        child_pid = int(self.child_pid_log.read_text(encoding="ascii"))
        for _ in range(20):
            if not self.process_is_running(child_pid):
                break
            time.sleep(0.05)
        self.assertFalse(self.process_is_running(child_pid))

    def test_worktree_tampering_still_overrides_success(self) -> None:
        result = self.review("grok", FAKE_TAMPER=str(self.repo / "injected.txt"))
        self.assertEqual(2, result.returncode)
        self.assertIn("modified the target worktree", result.stderr)

    def test_delayed_background_tamper_is_rejected_and_collected(self) -> None:
        target = self.repo / "escaped.txt"
        result = self.review("codex", FAKE_DELAYED_TAMPER=str(target))
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertIn("left background child processes", result.stderr)
        child_pid = int(self.child_pid_log.read_text(encoding="ascii"))
        for _ in range(20):
            if not self.process_is_running(child_pid):
                break
            time.sleep(0.05)
        self.assertFalse(self.process_is_running(child_pid))
        time.sleep(1.1)
        self.assertFalse(target.exists())

    def test_onevoke_dispatches_directly_to_python_gate_without_cmd_reparse(self) -> None:
        dispatch_dir = self.root / "dispatch"
        dispatch_dir.mkdir()
        dispatch_log = self.root / "dispatch.json"
        unsafe_log = self.root / "unsafe-cmd.txt"
        copied_bin = self.root / "installed R&D" / "bin"
        copied_bin.mkdir(parents=True)
        for name in ("onevoke", "onevoke_config.py", "onevoke_fs.py"):
            shutil.copy2(PROJECT_ROOT / "bin" / name, copied_bin / name)
        (copied_bin / "onevoke_review.py").write_text(
            "import json, os, sys\n"
            "open(os.environ['DISPATCH_LOG'], 'w', encoding='utf-8').write("
            "json.dumps(sys.argv[1:], ensure_ascii=False))\n"
            "raise SystemExit(29)\n",
            encoding="utf-8",
        )
        (dispatch_dir / "onevoke-review.cmd").write_text(
            '@echo off\r\n> "%UNSAFE_CMD_LOG%" echo called\r\nexit /b 99\r\n',
            encoding="ascii",
        )
        config = {
            "schema_version": 1,
            "welcome_complete": True,
            "kanban_agent": "codex",
            "launcher": "console",
            "language": "en",
            "reviewers": {role: "codex" for role in ("PM", "CSA", "Hacker", "QA")},
            "memsearch": {"enabled": False},
        }
        Path(self.environment["ONEVOKE_CONFIG"]).write_text(json.dumps(config), encoding="utf-8")
        environment = {
            **self.environment,
            "PATH": str(dispatch_dir) + os.pathsep + self.environment["PATH"],
            "DISPATCH_LOG": str(dispatch_log),
            "UNSAFE_CMD_LOG": str(unsafe_log),
        }
        task_goal = 'R&D &|<>^%! "quoted" trailing\\'
        review_context = 'context &|<>^%! \\"nested\\" end\\\\'
        result = subprocess.run(
            [
                sys.executable,
                str(copied_bin / "onevoke"),
                "review",
                str(self.repo),
                self.base,
                self.head,
                "qa",
                task_goal,
                review_context,
            ],
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        self.assertEqual(29, result.returncode, result.stderr)
        self.assertFalse(unsafe_log.exists(), "programmatic dispatch must not execute a .cmd shim")
        self.assertEqual(
            [
                "codex",
                str(self.repo),
                self.base,
                self.head,
                "QA",
                task_goal,
                review_context,
            ],
            json.loads(dispatch_log.read_text(encoding="utf-8")),
        )

    @staticmethod
    def process_is_running(process_id: int) -> bool:
        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, process_id)
        if not process:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)):
                return False
            return exit_code.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(process)


if __name__ == "__main__":
    unittest.main()
