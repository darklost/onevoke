#!/usr/bin/env python3

"""onevoke-review.sh 的 Claude 门禁和隔离适配测试."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
REVIEWER = PROJECT_ROOT / "bin" / "onevoke-review.sh"
REVIEWER_AGENT = "claude"

FAKE_CLAUDE = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_CLAUDE_ARGV"
cat > "$FAKE_CLAUDE_PROMPT"
pwd -P > "$FAKE_CLAUDE_CWD"
printf '%s\\n' "$CLAUDE_CONFIG_DIR" > "$FAKE_CLAUDE_HOME"
if [ -n "${FAKE_CLAUDE_SPEC_SNAPSHOT_LOG:-}" ]; then
    spec_path=$(sed -n 's/^Authoritative spec file: \\(.*\\)\\. Read it completely before reviewing\\.$/\\1/p' "$FAKE_CLAUDE_PROMPT")
    if [ -n "$spec_path" ]; then
        cat "$spec_path" > "$FAKE_CLAUDE_SPEC_SNAPSHOT_LOG"
    fi
fi

if [ -n "${FAKE_CLAUDE_TAMPER:-}" ]; then
    printf '%s\\n' 'tampered' > "$FAKE_CLAUDE_TAMPER"
fi
if [ -n "${FAKE_CLAUDE_SLEEP:-}" ]; then
    sleep 30
fi
if [ -n "${FAKE_CLAUDE_FAIL:-}" ]; then
    printf '%s\\n' 'fake claude failure' >&2
    exit 3
fi
if [ -n "${FAKE_CLAUDE_BAD_OUTPUT:-}" ]; then
    printf '%s\\n' '{}'
else
    printf '{"type":"result","subtype":"success","is_error":false,"result":"%s"}\\n' \\
        "${FAKE_CLAUDE_REPORT:-REPORT BODY}"
fi
exit 0
"""


class ClaudeReviewGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.tmp = self.root / "tmp"
        self.claude_home = self.root / "claude"
        for path in (self.repo, self.tmp, self.claude_home):
            path.mkdir()

        self.fake_claude = self.root / "fake-claude"
        self.fake_claude.write_text(FAKE_CLAUDE, encoding="utf-8")
        self.fake_claude.chmod(0o755)
        self.argv_log = self.root / "argv.log"
        self.prompt_log = self.root / "prompt.log"
        self.cwd_log = self.root / "cwd.log"
        self.home_log = self.root / "home.log"
        self.spec_snapshot_log = self.root / "spec-snapshot.log"

        self.git("init", "-q", "-b", "main")
        self.base = self.commit("a.txt", "base\n", "基线")
        self.head = self.commit("b.txt", "head\n", "改动")
        self.repo_real = Path(os.path.realpath(self.repo))

        self.env = os.environ.copy()
        self.env.update(
            GIT_CEILING_DIRECTORIES=str(self.root),
            TMPDIR=str(self.tmp),
            CLAUDE_CONFIG_DIR=str(self.claude_home),
            CLAUDE_REVIEW_BIN=str(self.fake_claude),
            CLAUDE_REVIEW_CHECK_INTERVAL_SECONDS="1",
            CLAUDE_REVIEW_MAX_RUNTIME_SECONDS="30",
            FAKE_CLAUDE_ARGV=str(self.argv_log),
            FAKE_CLAUDE_PROMPT=str(self.prompt_log),
            FAKE_CLAUDE_CWD=str(self.cwd_log),
            FAKE_CLAUDE_HOME=str(self.home_log),
            FAKE_CLAUDE_SPEC_SNAPSHOT_LOG=str(self.spec_snapshot_log),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def git(self, *args: str) -> str:
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
        return subprocess.run(
            [str(REVIEWER), REVIEWER_AGENT, *args],
            env={**self.env, **overrides},
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

    def test_unreadable_spec_path_is_rejected(self) -> None:
        result = self.review(
            str(self.repo), self.base, self.head, "PM", str(self.root / "missing.md")
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("spec path is not a readable file", result.stderr)

    def test_abbreviated_sha_is_rejected(self) -> None:
        result = self.review(str(self.repo), self.base[:8], self.head, "QA", "目标")
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

    def test_git_status_failure_is_rejected(self) -> None:
        result = self.default_review(GIT_INDEX_FILE=str(self.fake_claude))
        self.assertEqual(2, result.returncode)
        self.assertIn("failed to inspect worktree status", result.stderr)
        self.assertFalse(self.argv_log.exists())

    def test_worktree_inside_claude_home_is_rejected(self) -> None:
        result = self.default_review(CLAUDE_CONFIG_DIR=str(self.root))
        self.assertEqual(2, result.returncode)
        self.assertIn("overlaps a Claude-writable directory", result.stderr)

    def test_missing_claude_binary_reports_127(self) -> None:
        result = self.default_review(CLAUDE_REVIEW_BIN=str(self.root / "absent"))
        self.assertEqual(127, result.returncode)
        self.assertIn("Claude CLI is unavailable", result.stderr)

    def test_claude_failure_is_propagated(self) -> None:
        result = self.default_review(FAKE_CLAUDE_FAIL="1")
        self.assertEqual(3, result.returncode)
        self.assertIn("fake claude failure", result.stderr)

    def test_timeout_stops_claude(self) -> None:
        result = self.default_review(
            FAKE_CLAUDE_SLEEP="1", CLAUDE_REVIEW_MAX_RUNTIME_SECONDS="1"
        )
        self.assertEqual(124, result.returncode)
        self.assertIn("Claude review exceeded 1 seconds", result.stderr)

    def test_worktree_tampering_is_detected(self) -> None:
        result = self.default_review(FAKE_CLAUDE_TAMPER=str(self.repo / "injected.txt"))
        self.assertEqual(2, result.returncode)
        self.assertIn("modified the target worktree", result.stderr)

    def test_clean_review_returns_the_report(self) -> None:
        result = self.default_review(FAKE_CLAUDE_REPORT="QA-1 没有发现问题")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("QA-1 没有发现问题", result.stdout)

    def test_incomplete_claude_output_is_rejected(self) -> None:
        result = self.default_review(FAKE_CLAUDE_BAD_OUTPUT="1")
        self.assertEqual(1, result.returncode)
        self.assertIn("did not complete with review text", result.stderr)

    def test_review_can_be_invoked_through_an_external_symlink(self) -> None:
        link_dir = self.root / "links"
        link_dir.mkdir()
        reviewer = link_dir / REVIEWER.name
        reviewer.symlink_to(REVIEWER)
        result = subprocess.run(
            [reviewer, REVIEWER_AGENT, str(self.repo), self.base, self.head, "QA", "确认改动正确"],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("REPORT BODY", result.stdout)

    def test_claude_is_invoked_with_read_only_isolation(self) -> None:
        self.assertEqual(0, self.default_review().returncode)
        argv = self.argv_log.read_text(encoding="utf-8").splitlines()
        self.assertIn("--print", argv)
        self.assertEqual("json", argv[argv.index("--output-format") + 1])
        self.assertEqual("plan", argv[argv.index("--permission-mode") + 1])
        self.assertEqual("Read,Grep,Glob", argv[argv.index("--tools") + 1])
        denied = argv[argv.index("--disallowedTools") + 1]
        for tool in ("Bash", "Edit", "Write", "WebFetch", "WebSearch", "Task"):
            self.assertIn(tool, denied)
        self.assertIn("--safe-mode", argv)
        self.assertIn("--disable-slash-commands", argv)
        self.assertIn("--no-session-persistence", argv)
        self.assertEqual(str(self.repo_real), argv[argv.index("--add-dir") + 1])
        self.assertEqual("opus", argv[argv.index("--model") + 1])
        self.assertEqual("high", argv[argv.index("--effort") + 1])
        self.assertEqual(str(self.claude_home), self.home_log.read_text().strip())
        runtime = Path(self.cwd_log.read_text(encoding="utf-8").strip())
        self.assertNotEqual(self.repo_real, runtime)
        self.assertEqual(self.tmp, runtime.parent)
        self.assertTrue(runtime.name.startswith("claude-review."))

    def test_model_override_is_forwarded(self) -> None:
        result = self.default_review(CLAUDE_REVIEW_MODEL="sonnet")
        self.assertEqual(0, result.returncode, result.stderr)
        argv = self.argv_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual("sonnet", argv[argv.index("--model") + 1])

    def test_prompt_carries_role_task_and_scope(self) -> None:
        self.assertEqual(0, self.default_review().returncode)
        prompt = self.prompt_log.read_text(encoding="utf-8")
        self.assertIn("You are the QA review agent", prompt)
        self.assertIn("Authoritative task goal: 确认改动正确", prompt)
        self.assertIn(f"{self.base}..{self.head}", prompt)
        self.assertIn(str(self.repo_real), prompt)
        self.assertIn("Use only the Read, Grep, and Glob tools", prompt)

    def test_spec_file_is_passed_as_authoritative_context(self) -> None:
        spec_dir = self.root / "specs"
        spec_dir.mkdir()
        spec = spec_dir / "spec.md"
        spec.write_text("# 任务契约\n", encoding="utf-8")
        result = self.review(str(self.repo), self.base, self.head, "PM", str(spec))
        self.assertEqual(0, result.returncode, result.stderr)
        prompt = self.prompt_log.read_text(encoding="utf-8")
        self.assertNotIn(str(spec), prompt)
        self.assertIn("Authoritative spec file:", prompt)
        self.assertEqual("# 任务契约\n", self.spec_snapshot_log.read_text(encoding="utf-8"))
        argv = self.argv_log.read_text(encoding="utf-8").splitlines()
        allowed_dirs = [
            argv[index + 1]
            for index, argument in enumerate(argv)
            if argument == "--add-dir"
        ]
        self.assertEqual([str(self.repo_real)], allowed_dirs)
        self.assertNotIn(str(spec_dir.resolve()), allowed_dirs)


if __name__ == "__main__":
    unittest.main()
