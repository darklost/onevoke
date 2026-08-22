#!/usr/bin/env python3

"""onevoke-review.sh 的 Grok 门禁测试.

Grok CLI 用假二进制替代: 门禁的价值在于「不满足前置条件时拒绝执行」,
而失效是静默的 —— 校验被绕过时不会报错, 只会放行. 这里逐条验证拒绝路径,
外加一条放行路径确认传给 Grok 的隔离参数没有丢.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
REVIEWER = PROJECT_ROOT / "bin" / "onevoke-review.sh"
REVIEWER_AGENT = "grok"

FAKE_GROK = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_GROK_ARGV"

prompt=""
while [ "$#" -gt 0 ]; do
    if [ "$1" = "--prompt-file" ]; then
        prompt="$2"
    fi
    shift
done
cp "$prompt" "$FAKE_GROK_PROMPT"

if [ -n "${FAKE_GROK_TAMPER:-}" ]; then
    printf '%s\\n' 'tampered' > "$FAKE_GROK_TAMPER"
fi
if [ -n "${FAKE_GROK_FAIL:-}" ]; then
    printf '%s\\n' 'fake grok failure' >&2
    exit 3
fi
if [ -n "${FAKE_GROK_BAD_OUTPUT:-}" ]; then
    printf '%s\\n' '{}'
else
    printf '{"stopReason":"end_turn","text":"%s"}\\n' \
        "${FAKE_GROK_REPORT:-REPORT BODY}"
fi
exit 0
"""


class GrokReviewGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        # worktree 不能落在脚本判定为 Grok 可写的目录里, 因此和 TMPDIR、
        # GROK_HOME 并列而不是嵌套.
        self.repo = self.root / "repo"
        self.tmp = self.root / "tmp"
        self.grok_home = self.root / "grok"
        for path in (self.repo, self.tmp, self.grok_home):
            path.mkdir()

        self.fake_grok = self.root / "fake-grok"
        self.fake_grok.write_text(FAKE_GROK, encoding="utf-8")
        os.chmod(self.fake_grok, 0o755)
        self.argv_log = self.root / "argv.log"
        self.prompt_log = self.root / "prompt.log"

        self.git("init", "-q", "-b", "main")
        self.base = self.commit("a.txt", "base\n", "基线")
        self.head = self.commit("b.txt", "head\n", "改动")

        # 脚本内部用 `pwd -P`, 断言要拿物理路径比对.
        self.repo_real = Path(os.path.realpath(self.repo))

        self.env = os.environ.copy()
        self.env.update(
            # 临时目录的祖先可能碰巧是个 Git 仓库, 会让「不在 worktree 内」的
            # 用例走进别的分支. 限制向上搜索范围.
            GIT_CEILING_DIRECTORIES=str(self.root),
            TMPDIR=str(self.tmp),
            # 隔离 Onevoke 配置, 避免读到本机真实模型设置.
            ONEVOKE_CONFIG=str(self.root / "onevoke-config.json"),
            ONEVOKE_LANG="en",
            GROK_HOME=str(self.grok_home),
            GROK_REVIEW_BIN=str(self.fake_grok),
            GROK_REVIEW_CHECK_INTERVAL_SECONDS="1",
            GROK_REVIEW_MAX_RUNTIME_SECONDS="30",
            FAKE_GROK_ARGV=str(self.argv_log),
            FAKE_GROK_PROMPT=str(self.prompt_log),
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

    # ---- 拒绝路径 ----

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

    def test_git_status_failure_is_rejected(self) -> None:
        result = self.default_review(GIT_INDEX_FILE=str(self.fake_grok))

        self.assertEqual(2, result.returncode)
        self.assertIn("failed to inspect worktree status", result.stderr)
        self.assertFalse(self.argv_log.exists())

    def test_worktree_inside_grok_home_is_rejected(self) -> None:
        result = self.default_review(GROK_HOME=str(self.root))

        self.assertEqual(2, result.returncode)
        self.assertIn("overlaps a Grok-writable directory", result.stderr)

    def test_missing_grok_binary_reports_127(self) -> None:
        result = self.default_review(GROK_REVIEW_BIN=str(self.root / "absent"))

        self.assertEqual(127, result.returncode)
        self.assertIn("Grok CLI is unavailable", result.stderr)

    def test_grok_failure_is_propagated(self) -> None:
        result = self.default_review(FAKE_GROK_FAIL="1")

        self.assertEqual(3, result.returncode)
        self.assertIn("fake grok failure", result.stderr)

    def test_worktree_tampering_is_detected(self) -> None:
        result = self.default_review(FAKE_GROK_TAMPER=str(self.repo / "injected.txt"))

        self.assertEqual(2, result.returncode)
        self.assertIn("modified the target worktree", result.stderr)

    # ---- 放行路径 ----

    def test_clean_review_returns_the_report(self) -> None:
        result = self.default_review(FAKE_GROK_REPORT="QA-1 没有发现问题")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("QA-1 没有发现问题", result.stdout)

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

    def test_incomplete_grok_output_is_rejected(self) -> None:
        result = self.default_review(FAKE_GROK_BAD_OUTPUT="1")

        self.assertEqual(1, result.returncode)
        self.assertIn("did not complete with review text", result.stderr)

    def test_grok_is_invoked_with_the_isolation_flags(self) -> None:
        self.assertEqual(0, self.default_review().returncode)

        argv = self.argv_log.read_text(encoding="utf-8").splitlines()

        self.assertIn("--sandbox", argv)
        self.assertEqual("read-only", argv[argv.index("--sandbox") + 1])
        self.assertIn("--disable-web-search", argv)
        self.assertIn("--no-memory", argv)
        self.assertIn("--no-subagents", argv)
        self.assertIn("--no-plan", argv)
        self.assertIn("--prompt-file", argv)
        self.assertIn("--effort", argv)
        self.assertEqual("high", argv[argv.index("--effort") + 1])
        self.assertNotIn("--model", argv)
        self.assertNotIn("--reasoning-effort", argv)

    def test_model_override_is_forwarded(self) -> None:
        result = self.default_review(GROK_REVIEW_MODEL="grok-4.5")

        self.assertEqual(0, result.returncode, result.stderr)
        argv = self.argv_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual("grok-4.5", argv[argv.index("--model") + 1])

    def test_prompt_carries_role_task_and_scope(self) -> None:
        self.assertEqual(0, self.default_review().returncode)

        prompt = self.prompt_log.read_text(encoding="utf-8")

        self.assertIn("You are the QA review agent", prompt)
        self.assertIn("Authoritative task goal: 确认改动正确", prompt)
        self.assertIn(f"{self.base}..{self.head}", prompt)
        self.assertIn(str(self.repo_real), prompt)
        self.assertIn("Use only read_file, grep, and list_dir", prompt)

    def test_spec_file_is_passed_as_authoritative_context(self) -> None:
        spec = self.root / "spec.md"
        spec.write_text("# 任务契约\n", encoding="utf-8")

        result = self.review(str(self.repo), self.base, self.head, "PM", str(spec))

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(f"Authoritative spec file: {spec}", self.prompt_log.read_text())


if __name__ == "__main__":
    unittest.main()
