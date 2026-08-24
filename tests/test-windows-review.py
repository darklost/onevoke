#!/usr/bin/env python3

"""原生 Windows Reviewer 门禁与 .cmd 入口测试."""

from __future__ import annotations

import contextlib
import ctypes
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

if os.name == "nt":
    from ctypes import wintypes


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = PROJECT_ROOT / "bin"
REVIEWER = PROJECT_ROOT / "bin" / "onevoke-review.cmd"
REVIEW_IMPLEMENTATION = PROJECT_ROOT / "bin" / "onevoke_review.py"
ONEVOKE = PROJECT_ROOT / "bin" / "onevoke"
sys.path.insert(0, str(BIN_DIR))
import onevoke_fs
import onevoke_review
sys.path.pop(0)

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

    def review(
        self,
        agent: str,
        task_goal: str = "确认 Windows 审核正确",
        **overrides: str,
    ) -> subprocess.CompletedProcess[str]:
        environment = {**self.environment, "FAKE_AGENT": agent, **overrides}
        return subprocess.run(
            [
                str(REVIEWER),
                agent,
                str(self.repo),
                self.base,
                self.head,
                "QA",
                task_goal,
            ],
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )

    def acl_text(self, path: Path) -> str:
        result = subprocess.run(
            ["icacls.exe", str(path)],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout

    def assert_private_acl(self, path: Path) -> None:
        acl = self.acl_text(path)
        self.assertNotIn("(I)", acl, acl)
        self.assertEqual(1, acl.count("(F)"), acl)

    @staticmethod
    def set_junction_reparse(directory: Path, target: Path) -> None:
        """将既有空目录原地改成 mount-point reparse point."""
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.DeviceIoControl.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        kernel32.DeviceIoControl.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateFileW(
            str(directory),
            0x40000000,  # GENERIC_WRITE
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,  # OPEN_EXISTING
            0x02000000 | 0x00200000,
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            print_name = str(target.resolve())
            substitute = ("\\??\\" + print_name).encode("utf-16-le")
            printable = print_name.encode("utf-16-le")
            path_buffer = substitute + b"\x00\x00" + printable + b"\x00\x00"
            reparse = struct.pack(
                "<IHHHHHH",
                0xA0000003,
                8 + len(path_buffer),
                0,
                0,
                len(substitute),
                len(substitute) + 2,
                len(printable),
            ) + path_buffer
            returned = wintypes.DWORD()
            input_buffer = ctypes.create_string_buffer(reparse)
            if not kernel32.DeviceIoControl(
                handle,
                0x000900A4,  # FSCTL_SET_REPARSE_POINT
                input_buffer,
                len(reparse),
                None,
                0,
                ctypes.byref(returned),
                None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(handle)

    def test_runtime_directory_is_private_for_entire_lease(self) -> None:
        original_open = onevoke_fs._open_relative_handle
        observed: list[Path] = []

        def inspect_created_acl(parent_handle, name, path, **kwargs):
            handle = original_open(parent_handle, name, path, **kwargs)
            if kwargs.get("private_creation") == "directory":
                self.assertEqual(onevoke_fs._CREATE_NEW, kwargs.get("creation"))
                self.assert_private_acl(path)
                observed.append(path)
            return handle

        with mock.patch.object(
            onevoke_fs, "_open_relative_handle", side_effect=inspect_created_acl
        ):
            with onevoke_fs.private_temporary_directory_nofollow(
                self.runtime, prefix="codex-review."
            ) as created:
                self.assertEqual([created], observed)
                self.assert_private_acl(created)
                with self.assertRaises(OSError):
                    created.rename(self.root / "replaced-runtime")
                with self.assertRaises(OSError):
                    self.runtime.rename(self.root / "replaced-temp-parent")
                with self.assertRaises(OSError):
                    self.root.rename(self.root.parent / f"{self.root.name}-replaced")
                (created / "evidence.txt").write_text(
                    "sensitive\n", encoding="utf-8"
                )

        self.assertEqual([created], observed)
        self.assertFalse(created.exists())

    def test_runtime_directory_creation_retries_random_name_collision(self) -> None:
        collision = self.runtime / "codex-review.collision"
        collision.mkdir()
        with mock.patch.object(
            onevoke_fs.secrets,
            "token_hex",
            side_effect=("collision", "fresh"),
        ) as token_hex:
            with onevoke_fs.private_temporary_directory_nofollow(
                self.runtime, prefix="codex-review."
            ) as created:
                self.assertEqual(self.runtime / "codex-review.fresh", created)
                self.assert_private_acl(created)

        self.assertEqual(2, token_hex.call_count)
        self.assertTrue(collision.is_dir())
        self.assertFalse(created.exists())

    def test_runtime_directory_hardening_failure_leaves_no_entry_or_handle(self) -> None:
        candidate = self.runtime / "codex-review.failure"
        with mock.patch.object(
            onevoke_fs.secrets, "token_hex", return_value="failure"
        ), mock.patch.object(
            onevoke_fs,
            "_tighten_private_handle",
            side_effect=OSError("forced runtime ACL failure"),
        ):
            with self.assertRaisesRegex(OSError, "forced runtime ACL failure"):
                with onevoke_fs.private_temporary_directory_nofollow(
                    self.runtime, prefix="codex-review."
                ):
                    self.fail("an insecure runtime must never be yielded")

        self.assertFalse(candidate.exists())
        self.assertEqual([], list(self.runtime.iterdir()))
        moved = self.root / "runtime-moved"
        self.runtime.rename(moved)
        moved.rename(self.runtime)

    def test_execute_review_holds_runtime_lease_through_sensitive_writes(self) -> None:
        observed: list[Path] = []

        def inspect_runtime(_context: object, runtime: Path) -> int:
            observed.append(runtime)
            self.assert_private_acl(runtime)
            with self.assertRaises(OSError):
                runtime.rename(self.root / "runtime-replacement")
            (runtime / "prompt.txt").write_text("secret\n", encoding="utf-8")
            return 0

        context = mock.Mock(temp_root=self.runtime, agent="codex")
        with mock.patch.object(
            onevoke_review,
            "_execute_review_in_runtime",
            side_effect=inspect_runtime,
        ):
            result = onevoke_review.execute_review(context)

        self.assertEqual(0, result)
        self.assertEqual(1, len(observed))
        self.assertFalse(observed[0].exists())
        self.assertEqual([], list(self.runtime.iterdir()))

    def test_execute_review_blocks_in_place_runtime_reparse(self) -> None:
        outside = self.root / "runtime-reparse-outside"
        outside.mkdir()
        observed: list[Path] = []

        def attempt_reparse(_context: object, runtime: Path) -> int:
            observed.append(runtime)
            with self.assertRaises(OSError):
                self.set_junction_reparse(runtime, outside)
            self.assertFalse(onevoke_fs.is_reparse_point(runtime))
            (runtime / "prompt.txt").write_text("secret\n", encoding="utf-8")
            return 0

        context = mock.Mock(temp_root=self.runtime, agent="codex")
        with mock.patch.object(
            onevoke_review,
            "_execute_review_in_runtime",
            side_effect=attempt_reparse,
        ):
            result = onevoke_review.execute_review(context)

        self.assertEqual(0, result)
        self.assertEqual(1, len(observed))
        self.assertFalse(observed[0].exists())
        self.assertEqual([], list(outside.iterdir()))

    def test_runtime_lease_covers_real_process_tree_collection(self) -> None:
        original_stop = onevoke_review.stop_process_tree
        phases: list[str] = []
        observed: list[Path] = []

        def inspect_collection(process: subprocess.Popen[bytes]) -> bool:
            active = list(self.runtime.glob("codex-review.*"))
            self.assertEqual(1, len(active))
            runtime = active[0]
            observed.append(runtime)
            phases.append("before-stop")
            with self.assertRaises(OSError):
                runtime.rename(self.root / "runtime-during-stop")
            result = original_stop(process)
            phases.append("after-stop")
            self.assertTrue(runtime.is_dir())
            with self.assertRaises(OSError):
                runtime.rename(self.root / "runtime-after-stop")
            return result

        arguments = [
            str(self.repo),
            self.base,
            self.head,
            "QA",
            "verify runtime lease covers process tree collection",
        ]
        environment = {
            **self.environment,
            "FAKE_AGENT": "codex",
            "FAKE_REPORT": "lease collection complete",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            context = onevoke_review.validate_context("codex", arguments)
            with mock.patch.object(
                onevoke_review,
                "stop_process_tree",
                side_effect=inspect_collection,
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    result = onevoke_review.execute_review(context)

        self.assertEqual(0, result)
        self.assertEqual(["before-stop", "after-stop"], phases)
        self.assertEqual(1, len(observed))
        self.assertFalse(observed[0].exists())

    def test_execute_review_rejects_reparse_cleanup_without_touching_target(self) -> None:
        outside = self.root / "cleanup-outside"
        outside.mkdir()
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("keep\n", encoding="utf-8")
        observed: list[Path] = []

        def plant_reparse(_context: object, runtime: Path) -> int:
            observed.append(runtime)
            result = subprocess.run(
                [
                    "cmd.exe",
                    "/d",
                    "/c",
                    "mklink",
                    "/J",
                    str(runtime / "unsafe-child"),
                    str(outside),
                ],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                raise unittest.SkipTest(
                    f"cannot create a Windows junction: {result.stderr}"
                )
            return 0

        context = mock.Mock(temp_root=self.runtime, agent="codex")
        error_output = io.StringIO()
        with mock.patch.object(
            onevoke_review,
            "_execute_review_in_runtime",
            side_effect=plant_reparse,
        ), mock.patch("sys.stderr", error_output):
            result = onevoke_review.execute_review(context)

        self.assertEqual(2, result)
        self.assertIn(
            "cannot safely remove private temporary directory",
            error_output.getvalue(),
        )
        self.assertTrue(sentinel.is_file())
        self.assertEqual("keep\n", sentinel.read_text(encoding="utf-8"))
        self.assertEqual(1, len(observed))
        self.assert_private_acl(observed[0])
        unsafe_child = observed[0] / "unsafe-child"
        if os.path.lexists(unsafe_child):
            os.rmdir(unsafe_child)
        shutil.rmtree(observed[0], ignore_errors=True)

    def test_execute_review_reports_cleanup_budget_exhaustion(self) -> None:
        observed: list[Path] = []

        def create_extra_files(_context: object, runtime: Path) -> int:
            observed.append(runtime)
            (runtime / "one.txt").write_text("one\n", encoding="utf-8")
            (runtime / "two.txt").write_text("two\n", encoding="utf-8")
            return 0

        context = mock.Mock(temp_root=self.runtime, agent="codex")
        error_output = io.StringIO()
        with mock.patch.object(
            onevoke_review,
            "_execute_review_in_runtime",
            side_effect=create_extra_files,
        ), mock.patch.object(
            onevoke_fs,
            "_PRIVATE_TEMP_CLEANUP_MAX_ENTRIES",
            1,
        ), mock.patch("sys.stderr", error_output):
            result = onevoke_review.execute_review(context)

        self.assertEqual(2, result)
        self.assertIn("entry budget exceeded", error_output.getvalue())
        self.assertEqual(1, len(observed))
        self.assert_private_acl(observed[0])
        shutil.rmtree(observed[0], ignore_errors=True)

    def test_reparse_temp_root_is_rejected_before_any_write(self) -> None:
        outside = self.root / "outside-temp-target"
        outside.mkdir()
        temporary_link = self.root / "unsafe-temp"
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(temporary_link), str(outside)],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            self.skipTest(f"cannot create a Windows junction: {result.stderr}")
        try:
            review = self.review(
                "codex",
                TMPDIR=str(temporary_link),
                TEMP=str(temporary_link),
                TMP=str(temporary_link),
            )
        finally:
            if os.path.lexists(temporary_link):
                os.rmdir(temporary_link)

        self.assertEqual(1, review.returncode, review.stderr)
        self.assertIn("private review runtime", review.stderr)
        self.assertNotIn("Traceback", review.stderr)
        self.assertEqual([], list(outside.iterdir()))

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

    def test_claude_spec_snapshot_is_cleaned_after_success(self) -> None:
        spec = self.root / "authoritative-spec.md"
        spec.write_text("# Windows spec\n", encoding="utf-8")

        result = self.review(
            "claude",
            task_goal=str(spec.resolve()),
            FAKE_REPORT="claude spec complete",
        )

        self.assertEqual(0, result.returncode, result.stderr)
        prompt = self.prompt_log.read_text(encoding="utf-8")
        marker = "Authoritative spec file: "
        snapshot_text = prompt.split(marker, 1)[1].split(
            ". Read it completely before reviewing.", 1
        )[0]
        snapshot = Path(snapshot_text)
        self.assertEqual("task-spec.md", snapshot.name)
        self.assertFalse(snapshot.exists())
        self.assertEqual([], list(self.runtime.iterdir()))

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
