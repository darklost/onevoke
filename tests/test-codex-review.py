#!/usr/bin/env python3

"""onevoke-review.sh 的 Codex 门禁测试.

Codex CLI 用假二进制替代: 门禁的价值在于「不满足前置条件时拒绝执行」,
而失效是静默的 —— 校验被绕过时不会报错, 只会放行. 这里逐条验证拒绝路径,
外加一条放行路径确认传给 Codex 的隔离参数没有丢.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
REVIEWER = PROJECT_ROOT / "bin" / "onevoke-review.sh"
REVIEWER_AGENT = "codex"

FAKE_CODEX = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_CODEX_ARGV"
cat > "$FAKE_CODEX_STDIN"

out=""
while [ "$#" -gt 0 ]; do
    if [ "$1" = "--output-last-message" ]; then
        out="$2"
    fi
    shift
done

if [ -n "${FAKE_CODEX_TAMPER:-}" ]; then
    printf '%s\\n' 'tampered' > "$FAKE_CODEX_TAMPER"
fi
if [ -n "${FAKE_CODEX_FAIL:-}" ]; then
    printf '%s\\n' 'fake codex failure' >&2
    exit 3
fi
if [ -n "$out" ]; then
    printf '%s\\n' "${FAKE_CODEX_REPORT:-REPORT BODY}" > "$out"
fi
exit 0
"""


class CodexReviewGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        # worktree 不能落在脚本判定为 Codex 可写的目录里, 因此和 TMPDIR、
        # CODEX_HOME 并列而不是嵌套.
        self.repo = self.root / "repo"
        self.tmp = self.root / "tmp"
        self.codex_home = self.root / "codex"
        for path in (self.repo, self.tmp, self.codex_home):
            path.mkdir()

        self.fake_codex = self.root / "fake-codex"
        self.fake_codex.write_text(FAKE_CODEX, encoding="utf-8")
        os.chmod(self.fake_codex, 0o755)
        self.argv_log = self.root / "argv.log"
        self.stdin_log = self.root / "stdin.log"

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
            CODEX_HOME=str(self.codex_home),
            CODEX_REVIEW_BIN=str(self.fake_codex),
            CODEX_REVIEW_CHECK_INTERVAL_SECONDS="1",
            CODEX_REVIEW_MAX_RUNTIME_SECONDS="30",
            FAKE_CODEX_ARGV=str(self.argv_log),
            FAKE_CODEX_STDIN=str(self.stdin_log),
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

    def test_missing_agent_reports_usage(self) -> None:
        result = subprocess.run(
            [str(REVIEWER)],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("Usage: onevoke-review.sh <agent>", result.stderr)

    def test_default_locale_reports_chinese_usage(self) -> None:
        env = {key: value for key, value in self.env.items() if key != "ONEVOKE_LANG"}
        for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
            env.pop(name, None)
        result = subprocess.run(
            [str(REVIEWER)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("用法: onevoke-review.sh", result.stderr)
        self.assertNotIn("Usage: onevoke-review.sh", result.stderr)

    def test_config_language_beats_env_without_cli_override(self) -> None:
        config_path = self.root / "onevoke-config.json"
        config_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "welcome_complete": True,
                    "kanban_agent": "codex",
                    "launcher": "tmux",
                    "language": "cn",
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
        env = {
            key: value
            for key, value in self.env.items()
            if key not in ("ONEVOKE_LANG", "ONEVOKE_LANG_CLI")
        }
        env.update({
            "ONEVOKE_CONFIG": str(config_path),
            "ONEVOKE_LANG": "en",
        })
        result = subprocess.run(
            [str(REVIEWER)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("用法: onevoke-review.sh", result.stderr)

    def test_cli_language_override_beats_config(self) -> None:
        config_path = self.root / "onevoke-config.json"
        config_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "welcome_complete": True,
                    "kanban_agent": "codex",
                    "launcher": "tmux",
                    "language": "cn",
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
        env = {
            key: value
            for key, value in self.env.items()
            if key not in ("ONEVOKE_LANG", "ONEVOKE_LANG_CLI")
        }
        env.update({
            "ONEVOKE_CONFIG": str(config_path),
            "ONEVOKE_LANG": "en",
            "ONEVOKE_LANG_CLI": "1",
        })
        result = subprocess.run(
            [str(REVIEWER)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("Usage: onevoke-review.sh <agent>", result.stderr)

    def test_invalid_config_language_falls_back_to_env(self) -> None:
        config_path = self.root / "onevoke-config.json"
        config_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "welcome_complete": True,
                    "kanban_agent": "invalid",
                    "launcher": "tmux",
                    "language": "cn",
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
        env = {
            key: value
            for key, value in self.env.items()
            if key not in ("ONEVOKE_LANG", "ONEVOKE_LANG_CLI")
        }
        env.update({
            "ONEVOKE_CONFIG": str(config_path),
            "ONEVOKE_LANG": "en",
        })
        result = subprocess.run(
            [str(REVIEWER)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("Usage: onevoke-review.sh <agent>", result.stderr)

    def test_unsupported_agent_is_rejected(self) -> None:
        result = subprocess.run(
            [str(REVIEWER), "other"],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("unsupported reviewer agent", result.stderr)

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
        result = self.default_review(GIT_INDEX_FILE=str(self.fake_codex))

        self.assertEqual(2, result.returncode)
        self.assertIn("failed to inspect worktree status", result.stderr)
        self.assertFalse(self.argv_log.exists())

    def test_worktree_inside_codex_home_is_rejected(self) -> None:
        result = self.default_review(CODEX_HOME=str(self.root))

        self.assertEqual(2, result.returncode)
        self.assertIn("overlaps a Codex-writable directory", result.stderr)

    def test_missing_codex_binary_reports_127(self) -> None:
        result = self.default_review(CODEX_REVIEW_BIN=str(self.root / "absent"))

        self.assertEqual(127, result.returncode)
        self.assertIn("Codex CLI is unavailable", result.stderr)

    def test_codex_failure_is_propagated(self) -> None:
        result = self.default_review(FAKE_CODEX_FAIL="1")

        self.assertEqual(3, result.returncode)
        self.assertIn("fake codex failure", result.stderr)

    def test_worktree_tampering_is_detected(self) -> None:
        result = self.default_review(FAKE_CODEX_TAMPER=str(self.repo / "injected.txt"))

        self.assertEqual(2, result.returncode)
        self.assertIn("modified the target worktree", result.stderr)

    # ---- 放行路径 ----

    def test_clean_review_returns_the_report(self) -> None:
        result = self.default_review(FAKE_CODEX_REPORT="QA-1 没有发现问题")

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

    def test_codex_is_invoked_with_the_isolation_flags(self) -> None:
        self.assertEqual(0, self.default_review().returncode)

        argv = self.argv_log.read_text(encoding="utf-8").splitlines()

        self.assertEqual("exec", argv[0])
        self.assertIn("--sandbox", argv)
        self.assertEqual("read-only", argv[argv.index("--sandbox") + 1])
        self.assertIn("--ephemeral", argv)
        self.assertEqual(str(self.repo_real), argv[argv.index("--cd") + 1])
        self.assertIn("--model", argv)

    def test_model_config_is_read_and_env_still_overrides(self) -> None:
        Path(self.env["ONEVOKE_CONFIG"]).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "welcome_complete": True,
                    "kanban_agent": "codex",
                    "launcher": "tmux",
                    "reviewers": {
                        role: "codex" for role in ("PM", "CSA", "Hacker", "QA")
                    },
                    "models": {
                        "review": {"codex": {"model": "config-model", "effort": "medium"}}
                    },
                    "memsearch": {"enabled": False},
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(0, self.default_review().returncode)
        argv = self.argv_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual("config-model", argv[argv.index("--model") + 1])
        self.assertIn('model_reasoning_effort="medium"', argv)

        result = self.default_review(CODEX_REVIEW_MODEL="env-model")
        self.assertEqual(0, result.returncode, result.stderr)
        argv = self.argv_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual("env-model", argv[argv.index("--model") + 1])

    def test_malformed_review_model_output_falls_back_to_builtin_default(self) -> None:
        """配置查询输出不是恰好两行时按读取失败处理, 回落内置默认."""
        fake_bin = self.root / "fake-python-bin"
        fake_bin.mkdir()
        fake_python = fake_bin / "python3"
        fake_python.write_text(
            "#!/bin/sh\nprintf 'cfg-model\\nmedium\\n\\n'\n", encoding="utf-8"
        )
        fake_python.chmod(0o755)

        result = self.default_review(
            PATH=f"{fake_bin}{os.pathsep}{self.env['PATH']}"
        )

        self.assertEqual(0, result.returncode, result.stderr)
        argv = self.argv_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual("gpt-5.6-sol", argv[argv.index("--model") + 1])
        self.assertIn('model_reasoning_effort="high"', argv)

    def test_empty_config_model_omits_model_flag(self) -> None:
        """配置里的空 model 表示用 CLI 默认模型, 不回落 Onevoke 内置默认."""
        Path(self.env["ONEVOKE_CONFIG"]).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "welcome_complete": True,
                    "kanban_agent": "codex",
                    "launcher": "tmux",
                    "reviewers": {
                        role: "codex" for role in ("PM", "CSA", "Hacker", "QA")
                    },
                    "models": {"review": {"codex": {"model": ""}}},
                    "memsearch": {"enabled": False},
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(0, self.default_review().returncode)
        argv = self.argv_log.read_text(encoding="utf-8").splitlines()
        self.assertNotIn("--model", argv)
        self.assertIn('model_reasoning_effort="high"', argv)

    def test_prompt_carries_role_task_and_scope(self) -> None:
        self.assertEqual(0, self.default_review().returncode)

        prompt = self.stdin_log.read_text(encoding="utf-8")

        self.assertIn("You are the QA review agent", prompt)
        self.assertIn("Authoritative task goal: 确认改动正确", prompt)
        self.assertIn(f"{self.base}..{self.head}", prompt)
        self.assertIn("Do not modify files", prompt)

    def test_spec_file_is_passed_as_authoritative_context(self) -> None:
        spec = self.root / "spec.md"
        spec.write_text("# 任务契约\n", encoding="utf-8")

        result = self.review(str(self.repo), self.base, self.head, "PM", str(spec))

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(f"Authoritative spec file: {spec}", self.stdin_log.read_text())


if __name__ == "__main__":
    unittest.main()
