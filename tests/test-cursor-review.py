#!/usr/bin/env python3

"""onevoke-review.sh 的 Cursor 门禁测试.

Cursor CLI 用假二进制替代: 门禁的价值在于「不满足前置条件时拒绝执行」,
而失效是静默的. 这里逐条验证拒绝路径, 外加放行路径确认传给 cursor-agent
的隔离参数、JSON 成功判定和会话目录隔离没有丢.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
REVIEWER = PROJECT_ROOT / "bin" / "onevoke-review.sh"
REVIEWER_AGENT = "cursor"

FAKE_CURSOR = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_CURSOR_ARGV"
printf '%s\\n' "$CURSOR_CONFIG_DIR" > "$FAKE_CURSOR_CONFIG_DIR"
printf '%s\\n' "$CURSOR_DATA_DIR" > "$FAKE_CURSOR_DATA_DIR"
cat > "$FAKE_CURSOR_PROMPT"
pwd -P > "$FAKE_CURSOR_CWD"

if [ -n "${FAKE_CURSOR_TAMPER:-}" ]; then
    printf '%s\\n' 'tampered' > "$FAKE_CURSOR_TAMPER"
fi
if [ -n "${FAKE_CURSOR_FAIL:-}" ]; then
    printf '%s\\n' 'fake cursor failure' >&2
    exit 3
fi
if [ -n "${FAKE_CURSOR_BAD_OUTPUT:-}" ]; then
    printf '%s\\n' '{}'
else
    printf '{"type":"result","subtype":"success","is_error":false,"result":"%s"}\\n' \\
        "${FAKE_CURSOR_REPORT:-REPORT BODY}"
fi
exit 0
"""


@unittest.skipUnless(os.name == "posix", "POSIX shell wrapper test")
class CursorReviewGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.tmp = self.root / "tmp"
        self.cursor_home = self.root / "cursor"
        for path in (self.repo, self.tmp, self.cursor_home):
            path.mkdir()

        self.fake_cursor = self.root / "fake-cursor"
        self.fake_cursor.write_text(FAKE_CURSOR, encoding="utf-8")
        os.chmod(self.fake_cursor, 0o755)
        self.argv_log = self.root / "argv.log"
        self.prompt_log = self.root / "prompt.log"
        self.cwd_log = self.root / "cwd.log"
        self.config_dir_log = self.root / "config-dir.log"
        self.data_dir_log = self.root / "data-dir.log"

        self.git("init", "-q", "-b", "main")
        self.base = self.commit("a.txt", "base\n", "基线")
        self.head = self.commit("b.txt", "head\n", "改动")
        self.repo_real = Path(os.path.realpath(self.repo))

        self.env = os.environ.copy()
        self.env.update(
            GIT_CEILING_DIRECTORIES=str(self.root),
            TMPDIR=str(self.tmp),
            ONEVOKE_CONFIG=str(self.root / "onevoke-config.json"),
            ONEVOKE_LANG="en",
            CURSOR_CONFIG_DIR=str(self.cursor_home),
            CURSOR_REVIEW_BIN=str(self.fake_cursor),
            CURSOR_REVIEW_CHECK_INTERVAL_SECONDS="1",
            CURSOR_REVIEW_MAX_RUNTIME_SECONDS="30",
            FAKE_CURSOR_ARGV=str(self.argv_log),
            FAKE_CURSOR_PROMPT=str(self.prompt_log),
            FAKE_CURSOR_CWD=str(self.cwd_log),
            FAKE_CURSOR_CONFIG_DIR=str(self.config_dir_log),
            FAKE_CURSOR_DATA_DIR=str(self.data_dir_log),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def git(self, *args: str) -> str:
        result = subprocess.run(
            [
                "git", "-C", str(self.repo),
                "-c", "user.name=test", "-c", "user.email=test@example.com",
                "-c", "commit.gpgsign=false",
                *args,
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        return result.stdout.strip()

    def commit(self, name: str, body: str, message: str) -> str:
        (self.repo / name).write_text(body, encoding="utf-8")
        self.git("add", name)
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def review(self, *args: str, **overrides: str) -> subprocess.CompletedProcess:
        env = {**self.env, **overrides}
        return subprocess.run(
            [str(REVIEWER), REVIEWER_AGENT, *args],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def default_review(self, **overrides: str) -> subprocess.CompletedProcess:
        return self.review(
            str(self.repo), self.base, self.head, "QA", "确认改动正确", **overrides
        )

    def test_missing_arguments_report_usage(self) -> None:
        result = self.review()

        self.assertEqual(2, result.returncode)
        self.assertIn("Usage: onevoke-review.sh <agent>", result.stderr)
        self.assertIn("cursor", result.stderr)

    def test_unsupported_role_is_rejected(self) -> None:
        result = self.review(str(self.repo), self.base, self.head, "Architect", "目标")

        self.assertEqual(2, result.returncode)
        self.assertIn("unsupported role", result.stderr)

    def test_relative_cwd_is_rejected(self) -> None:
        result = self.review("repo", self.base, self.head, "QA", "目标")

        self.assertEqual(2, result.returncode)
        self.assertIn("CWD must be an absolute path", result.stderr)

    def test_path_outside_git_worktree_is_rejected(self) -> None:
        outside = self.root / "plain"
        outside.mkdir()

        result = self.review(str(outside), self.base, self.head, "QA", "目标")

        self.assertEqual(2, result.returncode)
        self.assertIn("not inside a Git worktree", result.stderr)

    def test_empty_task_goal_is_rejected(self) -> None:
        result = self.review(str(self.repo), self.base, self.head, "QA", "")

        self.assertEqual(2, result.returncode)
        self.assertIn("task goal must not be empty", result.stderr)

    def test_abbreviated_sha_is_rejected(self) -> None:
        result = self.review(
            str(self.repo), self.base[:8], self.head, "QA", "目标"
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("must be a full commit SHA", result.stderr)

    def test_base_that_is_not_an_ancestor_is_rejected(self) -> None:
        self.git("checkout", "-q", "-b", "side", self.base)
        sibling = self.commit("c.txt", "side\n", "旁支")
        self.git("checkout", "-q", "main")

        result = self.review(str(self.repo), sibling, self.head, "QA", "目标")

        self.assertEqual(2, result.returncode)
        self.assertIn("not an ancestor", result.stderr)

    def test_head_not_matching_commit_is_rejected(self) -> None:
        result = self.review(str(self.repo), self.base, self.base, "QA", "目标")

        self.assertEqual(2, result.returncode)
        self.assertIn("HEAD does not match commit", result.stderr)

    def test_untracked_file_blocks_the_review(self) -> None:
        (self.repo / "scratch.txt").write_text("未提交\n", encoding="utf-8")

        result = self.default_review()

        self.assertEqual(2, result.returncode)
        self.assertIn("uncommitted or untracked changes", result.stderr)

    def test_worktree_inside_cursor_home_is_rejected(self) -> None:
        result = self.default_review(CURSOR_CONFIG_DIR=str(self.root))

        self.assertEqual(2, result.returncode)
        self.assertIn("overlaps a Cursor-writable directory", result.stderr)

    def test_missing_cursor_binary_reports_127(self) -> None:
        result = self.default_review(CURSOR_REVIEW_BIN=str(self.root / "absent"))

        self.assertEqual(127, result.returncode)
        self.assertIn("Cursor CLI is unavailable", result.stderr)

    def test_cursor_failure_is_propagated(self) -> None:
        result = self.default_review(FAKE_CURSOR_FAIL="1")

        self.assertEqual(3, result.returncode)
        self.assertIn("fake cursor failure", result.stderr)

    def test_worktree_tampering_is_detected(self) -> None:
        result = self.default_review(FAKE_CURSOR_TAMPER=str(self.repo / "injected.txt"))

        self.assertEqual(2, result.returncode)
        self.assertIn("modified the target worktree", result.stderr)

    def test_clean_review_returns_the_report(self) -> None:
        result = self.default_review(FAKE_CURSOR_REPORT="QA-1 没有发现问题")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("QA-1 没有发现问题", result.stdout)

    def test_incomplete_cursor_output_is_rejected(self) -> None:
        result = self.default_review(FAKE_CURSOR_BAD_OUTPUT="1")

        self.assertEqual(1, result.returncode)
        self.assertIn("did not complete with review text", result.stderr)
        self.assertIn("{}", result.stdout)

    def test_cursor_is_invoked_with_isolation_flags(self) -> None:
        self.assertEqual(0, self.default_review().returncode)

        argv = self.argv_log.read_text(encoding="utf-8").splitlines()
        self.assertIn("--print", argv)
        self.assertIn("--output-format", argv)
        self.assertEqual("json", argv[argv.index("--output-format") + 1])
        self.assertIn("--trust", argv)
        self.assertIn("--model", argv)
        self.assertEqual("cursor-grok-4.6-xhigh", argv[argv.index("--model") + 1])
        self.assertNotIn("--sandbox", argv)
        self.assertNotIn("--mode", argv)
        self.assertNotIn("--effort", argv)
        self.assertNotIn("--force", argv)

        runtime_config = self.config_dir_log.read_text(encoding="utf-8").strip()
        runtime_data = self.data_dir_log.read_text(encoding="utf-8").strip()
        self.assertTrue(runtime_config.startswith(str(self.tmp)))
        self.assertEqual(runtime_config, runtime_data)
        self.assertNotEqual(runtime_config, str(self.cursor_home))
        cwd = Path(self.cwd_log.read_text(encoding="utf-8").strip())
        self.assertEqual(Path(os.path.realpath(runtime_config)), cwd)

    def test_model_override_is_forwarded(self) -> None:
        result = self.default_review(CURSOR_REVIEW_MODEL="cursor-composer-1.5")

        self.assertEqual(0, result.returncode, result.stderr)
        argv = self.argv_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual("cursor-composer-1.5", argv[argv.index("--model") + 1])

    def test_prompt_is_fed_via_stdin(self) -> None:
        self.assertEqual(0, self.default_review().returncode)

        prompt = self.prompt_log.read_text(encoding="utf-8")
        self.assertIn("You are the QA review agent", prompt)
        self.assertIn("Authoritative task goal: 确认改动正确", prompt)
        self.assertIn(f"{self.base}..{self.head}", prompt)
        self.assertIn(str(self.repo_real), prompt)

    def test_spec_file_is_passed_as_authoritative_context(self) -> None:
        spec = self.root / "spec.md"
        spec.write_text("# 任务契约\n", encoding="utf-8")

        result = self.review(str(self.repo), self.base, self.head, "PM", str(spec))

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(f"Authoritative spec file: {spec}", self.prompt_log.read_text())


if __name__ == "__main__":
    unittest.main()
