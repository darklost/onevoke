#!/usr/bin/env python3

import contextlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MERGER = PROJECT_ROOT / "bin" / "merge-worktree-memory.py"
MERGER_COMMAND = (sys.executable, str(MERGER))
_LOCALE_VARS = ("ONEVOKE_LANG", "LC_ALL", "LC_MESSAGES", "LANG")


def merger_env(
    config_path: Path, *, language: str | None = "en", **extra: str
) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in _LOCALE_VARS}
    if language is not None:
        env["ONEVOKE_LANG"] = language
    env["ONEVOKE_CONFIG"] = str(config_path)
    env.update(extra)
    return env

_spec = importlib.util.spec_from_file_location("merge_worktree_memory", MERGER)
merger = importlib.util.module_from_spec(_spec)
sys.path.insert(0, str(MERGER.parent))
_spec.loader.exec_module(merger)
sys.path.pop(0)

# 由被替换的 bash + awk + sha256sum 实现产出, 锁住与既有记忆文件中
# merged-worktree-memory 标记的兼容性. 改动切分或归一化逻辑会让它失败.
GOLDEN_SOURCE = (
    "## Session 09:00\n\n"
    "### 09:30\n- User asked: X\n- Codex: Y\n\n"
    "### 10:05\n- 围栏用例\n\n```sh\n### 11:11\n```\n\n- 围栏后正文\n"
).encode()
GOLDEN_HASHES = [
    "478ac18edbd8431f999bd8dac7b3e9f52ba71a554f28514dde27406726c3a97f",
    "74bf4a78d110f718f201c4825879bce9ba7c6dcaad15f49d52e89399e5f9467d",
]


class BlockSplitTest(unittest.TestCase):
    def test_golden_hashes_match_replaced_implementation(self) -> None:
        blocks = merger.emit_entry_blocks(GOLDEN_SOURCE)

        self.assertEqual(2, len(blocks))
        self.assertEqual(GOLDEN_HASHES, [merger.normalized_hash(b) for b in blocks])

    def test_fenced_entry_header_is_body_not_header(self) -> None:
        blocks = merger.emit_entry_blocks(GOLDEN_SOURCE)

        self.assertIn(b"### 11:11\n", blocks[1])
        self.assertTrue(blocks[1].startswith(b"### 10:05\n"))

    def test_tilde_fence_and_longer_closing_marker(self) -> None:
        data = b"### 09:30\n~~~\n### 10:00\n~~~~\n- after\n"

        blocks = merger.emit_entry_blocks(data)

        self.assertEqual(1, len(blocks))
        self.assertIn(b"- after\n", blocks[0])

    def test_session_header_ends_block_and_is_dropped(self) -> None:
        data = b"### 09:30\n- one\n## Session 10:00\n- orphan\n### 10:30\n- two\n"

        blocks = merger.emit_entry_blocks(data)

        self.assertEqual([b"### 09:30\n- one\n", b"### 10:30\n- two\n"], blocks)

    def test_merge_markers_do_not_leak_into_the_previous_block(self) -> None:
        # 合并产物的形态: 每条正文之后紧跟下一条的头部标记.
        data = (
            b"\n<!-- merged-worktree-memory entry:" + b"1" * 64 + b" -->\n"
            b"<!-- merged-worktree-memory source:/old file:a.md merged-at:T -->\n"
            b"### 09:30\n- one\n\n"
            b"\n<!-- merged-worktree-memory entry:" + b"2" * 64 + b" -->\n"
            b"<!-- merged-worktree-memory source:/old file:a.md merged-at:T -->\n"
            b"### 10:05\n- two\n"
        )

        blocks = merger.emit_entry_blocks(data)

        self.assertEqual(2, len(blocks))
        for block in blocks:
            self.assertNotIn(b"merged-worktree-memory", block)

    def test_hash_ignores_markers_and_surrounding_blank_lines(self) -> None:
        bare = b"### 09:30\n- one\n"
        decorated = (
            b"\n<!-- merged-worktree-memory entry:" + b"0" * 64 + b" -->\n"
            b"<!-- merged-worktree-memory source:/tmp file:a.md merged-at:T -->\n"
            b"### 09:30\n- one\n\n\n"
        )

        self.assertEqual(
            merger.normalized_hash(bare), merger.normalized_hash(decorated)
        )

    def test_fallback_drops_session_headers_and_blank_lines(self) -> None:
        data = b"## Session 09:00\n\n- loose note\n\n- another\n"

        self.assertEqual(b"- loose note\n- another\n", merger.emit_fallback_block(data))


class CleanBytesTest(unittest.TestCase):
    def test_drops_invalid_sequences_and_keeps_valid_replacement_char(self) -> None:
        raw = b"ok \xf0\x9f\x98\x80 \xff \xe4\xbd" + " �".encode()

        self.assertEqual("ok \U0001f600   �", merger.clean_bytes(raw).decode())

    def test_drops_overlong_and_surrogate_encodings(self) -> None:
        self.assertEqual(b"ab", merger.clean_bytes(b"a\xc0\x80b"))
        self.assertEqual(b"ab", merger.clean_bytes(b"a\xed\xa0\x80b"))
        self.assertEqual(b"tail", merger.clean_bytes(b"tail\xe4\xbd"))


class MergeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.junctions: list[Path] = []
        self.root = Path(self.temp.name)
        self.source = self.root / "src"
        self.target = self.root / "tgt"
        self.source_memory = self.source / ".memsearch" / "memory"
        self.target_memory = self.target / ".memsearch" / "memory"
        self.missing_config = self.root / "missing-onevoke-config.json"
        self.source_memory.mkdir(parents=True)
        self.target_memory.mkdir(parents=True)
        subprocess.run(["git", "-C", str(self.source), "init", "-q"], check=True)

    def tearDown(self) -> None:
        for junction in reversed(self.junctions):
            if os.path.lexists(junction):
                os.rmdir(junction)
        self.temp.cleanup()

    def write_source(self, name: str, data: bytes) -> Path:
        path = self.source_memory / name
        path.write_bytes(data)
        return path

    def run_merger(self, *args: str, **env_overrides: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                *MERGER_COMMAND,
                "--source", str(self.source),
                "--target", str(self.target),
                *args,
            ],
            env=merger_env(self.missing_config, **env_overrides),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

    def run_merger_paths(
        self, source: Path, target: Path
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [*MERGER_COMMAND, "--source", str(source), "--target", str(target)],
            env=merger_env(self.missing_config),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )

    def make_junction(self, link: Path, target: Path) -> None:
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            self.skipTest(f"cannot create a Windows junction: {result.stderr}")
        self.junctions.append(link)

    def assert_private_acl(self, path: Path) -> None:
        acl = subprocess.run(
            ["icacls.exe", str(path)],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, acl.returncode, acl.stderr)
        self.assertNotIn("(I)", acl.stdout, acl.stdout)
        self.assertEqual(1, acl.stdout.count("(F)"), acl.stdout)

    def test_merges_then_skips_on_second_run(self) -> None:
        self.write_source("a.md", GOLDEN_SOURCE)

        first = self.run_merger()
        second = self.run_merger()

        self.assertEqual(0, first.returncode, first.stderr)
        self.assertIn("merged=2 skipped=0", first.stdout)
        self.assertIn("merged=0 skipped=2", second.stdout)
        merged = (self.target_memory / "a.md").read_bytes()
        for entry_hash in GOLDEN_HASHES:
            self.assertEqual(1, merged.count(entry_hash.encode()))

    def test_completion_marker_is_last_and_dedupes(self) -> None:
        block = b"### 09:30\n- complete body\n"
        entry_hash = merger.normalized_hash(block)
        payload = merger.format_entry(
            entry_hash, "entry", "/source", "a.md", "T", block
        )
        marker = f"<!-- merged-worktree-memory entry:{entry_hash} -->".encode()

        self.assertGreater(payload.index(marker), payload.index(block) + len(block) - 1)
        self.assertIn(entry_hash, merger.hashes_from_target_data(payload))

    def test_complete_legacy_prefix_marker_still_dedupes(self) -> None:
        block = b"### 09:30\n- complete legacy body\n"
        entry_hash = merger.normalized_hash(block)
        marker = f"<!-- merged-worktree-memory entry:{entry_hash} -->".encode()
        legacy = (
            b"\n"
            + marker
            + b"\n<!-- merged-worktree-memory source:/old file:a.md merged-at:T -->\n"
            + block
            + b"\n"
        )
        self.assertIn(entry_hash, merger.hashes_from_target_data(legacy))

        self.write_source("a.md", block)
        (self.target_memory / "a.md").write_bytes(legacy)
        result = self.run_merger()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("merged=0 skipped=1", result.stdout)

    def test_partial_legacy_prefix_marker_is_not_trusted_and_retries(self) -> None:
        block = b"### 09:30\n- full legacy source body\n"
        entry_hash = merger.normalized_hash(block)
        marker = f"<!-- merged-worktree-memory entry:{entry_hash} -->".encode()
        legacy_partial = (
            b"\n"
            + marker
            + b"\n<!-- merged-worktree-memory source:/old file:a.md merged-at:T -->\n"
            + block[: len(block) // 2]
        )
        self.assertNotIn(entry_hash, merger.hashes_from_target_data(legacy_partial))

        self.write_source("a.md", block)
        target_file = self.target_memory / "a.md"
        target_file.write_bytes(legacy_partial)
        retry = self.run_merger()
        second = self.run_merger()
        self.assertEqual(0, retry.returncode, retry.stderr)
        self.assertIn("merged=1 skipped=0", retry.stdout)
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertIn("merged=0 skipped=1", second.stdout)
        final = target_file.read_bytes()
        self.assertGreater(final.rfind(marker), final.rfind(block) + len(block) - 1)

    def test_legacy_file_entry_marker_requires_matching_complete_body(self) -> None:
        body = b"- complete fallback memory\n- second line\n"
        entry_hash = merger.normalized_hash(body)
        marker = (
            f"<!-- merged-worktree-memory file-entry:{entry_hash} -->".encode()
        )
        prefix = (
            b"\n"
            + marker
            + b"\n<!-- merged-worktree-memory source:/old file:a.md merged-at:T -->\n"
        )

        self.assertIn(
            entry_hash,
            merger.hashes_from_target_data(prefix + body + b"\n"),
        )
        self.assertNotIn(
            entry_hash,
            merger.hashes_from_target_data(prefix + body[: len(body) // 2]),
        )

    def test_partial_append_without_completion_marker_fails_then_retries(self) -> None:
        block = b"### 09:30\n- source body must be complete\n"
        self.write_source("a.md", block)
        target_file = self.target_memory / "a.md"
        expected_hash = merger.normalized_hash(block)
        completion_marker = (
            f"<!-- merged-worktree-memory entry:{expected_hash} -->".encode()
        )
        final_assert = mock.Mock()

        def fail_after_partial_body(handle, payload: bytes, target: Path) -> None:
            self.assertEqual(target_file, target)
            body_start = payload.index(block)
            partial_end = body_start + len(block) // 2
            written = handle.write(payload[:partial_end])
            self.assertEqual(partial_end, written)
            raise OSError(28, "simulated disk full")

        with mock.patch.object(
            merger, "append_payload_fully", side_effect=fail_after_partial_body
        ), mock.patch.object(merger, "assert_source_unchanged", final_assert):
            with self.assertRaises(OSError):
                merger.merge(str(self.source), str(self.target), dry_run=False)

        final_assert.assert_not_called()
        partial = target_file.read_bytes()
        self.assertNotIn(completion_marker, partial)
        self.assertNotIn(expected_hash, merger.hashes_from_target_data(partial))
        self.assertTrue((self.source_memory / "a.md").exists())

        retry = self.run_merger()
        second = self.run_merger()
        self.assertEqual(0, retry.returncode, retry.stderr)
        self.assertEqual(0, second.returncode, second.stderr)
        completed = target_file.read_bytes()
        body_position = completed.rfind(block)
        marker_position = completed.rfind(completion_marker)
        self.assertGreater(marker_position, body_position + len(block) - 1)
        self.assertIn("merged=0 skipped=1", second.stdout)

    def test_merge_waits_for_the_target_lock(self) -> None:
        self.write_source("a.md", b"### 09:30\n- serialized\n")
        lock_path = self.target / ".memsearch" / ".merge-worktree-memory.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock:
            with merger.exclusive_file_lock(lock):
                process = subprocess.Popen(
                    [*MERGER_COMMAND, "--source", str(self.source), "--target", str(self.target)],
                    text=True,
                    encoding="utf-8",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                time.sleep(0.2)
                self.assertIsNone(process.poll(), "merger ignored the held target lock")
                self.assertFalse((self.target_memory / "a.md").exists())
            stdout, stderr = process.communicate(timeout=10)

        self.assertEqual(0, process.returncode, stderr)
        self.assertIn("merged=1", stdout)

    def test_concurrent_merges_do_not_duplicate_an_entry(self) -> None:
        sources = []
        for index in range(6):
            source = self.root / f"source-{index}"
            memory = source / ".memsearch" / "memory"
            memory.mkdir(parents=True)
            (memory / "a.md").write_bytes(b"### 09:30\n- concurrent\n")
            subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)
            sources.append(source)

        processes = [
            subprocess.Popen(
                [*MERGER_COMMAND, "--source", str(source), "--target", str(self.target)],
                text=True,
                encoding="utf-8",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for source in sources
        ]
        results = [process.communicate(timeout=10) for process in processes]

        for process, (_, stderr) in zip(processes, results):
            self.assertEqual(0, process.returncode, stderr)
        merged = (self.target_memory / "a.md").read_bytes()
        entry_hash = merger.normalized_hash(b"### 09:30\n- concurrent\n")
        self.assertEqual(1, merged.count(entry_hash.encode()))

    def test_dedupes_against_unmarked_target_content(self) -> None:
        self.write_source("a.md", b"### 09:30\n- one\n")
        # 目标已有同一条目, 但没有合并标记: 靠重新切分和哈希识别.
        (self.target_memory / "a.md").write_bytes(b"## Session 09:00\n\n### 09:30\n- one\n")

        result = self.run_merger()

        self.assertIn("merged=0 skipped=1", result.stdout)

    def test_file_without_entry_headers_uses_fallback_entry(self) -> None:
        self.write_source("a.md", b"## Session 09:00\n\n- loose note\n")

        result = self.run_merger()

        merged = (self.target_memory / "a.md").read_bytes()
        self.assertIn("merged=1", result.stdout)
        self.assertIn(b"merged-worktree-memory file-entry:", merged)
        self.assertIn(b"- loose note\n", merged)

    def test_empty_source_file_counts_as_empty(self) -> None:
        self.write_source("a.md", b"## Session 09:00\n\n")

        result = self.run_merger()

        self.assertIn("merged=0 skipped=0 empty_files=1", result.stdout)
        self.assertFalse((self.target_memory / "a.md").exists())

    def test_new_entries_are_cleaned_on_write_without_rewriting_live_target(self) -> None:
        # 预置脏目标保留; 新条目写入前清理. 不对活跃目标做整文件 rewrite.
        (self.target_memory / "a.md").write_bytes(b"### 08:00\n- old \xff dirty\n")
        self.write_source("a.md", b"### 09:30\n- dirty \xff byte\n")

        result = self.run_merger()

        merged = (self.target_memory / "a.md").read_bytes()
        self.assertIn(b"\xff", merged)  # 预置脏字节保留
        self.assertIn(b"- dirty  byte", merged)  # 新条目已清理
        self.assertIn("left 1 dirty file(s) unchanged", result.stdout)

    def test_clean_target_is_not_rewritten(self) -> None:
        self.write_source("a.md", b"### 09:30\n- clean\n")
        self.run_merger()
        before = (self.target_memory / "a.md").stat().st_ino

        result = self.run_merger()

        self.assertEqual(before, (self.target_memory / "a.md").stat().st_ino)
        self.assertIn("left 0 dirty file(s) unchanged", result.stdout)

    def test_preexisting_dirty_target_keeps_permissions_and_bytes(self) -> None:
        target_file = self.target_memory / "a.md"
        target_file.write_bytes(b"### 08:00\n- old \xff byte\n")
        os.chmod(target_file, 0o640)
        self.write_source("a.md", b"### 09:30\n- new\n")

        self.run_merger()

        if os.name != "nt":
            self.assertEqual(0o640, target_file.stat().st_mode & 0o777)
        self.assertIn(b"\xff", target_file.read_bytes())

    def test_entry_pending_in_another_worktree_is_not_skipped(self) -> None:
        # A 的记忆文件是上次合并的产物, 条目一的正文之后带着条目二的头部标记,
        # 而条目二的正文只存在于 B. 标记不得让 B 的条目二被判为重复.
        entry_two = b"### 10:05\n- entry two\n"
        two_hash = merger.normalized_hash(entry_two)
        self.write_source(
            "a.md",
            b"\n<!-- merged-worktree-memory entry:" + b"1" * 64 + b" -->\n"
            b"<!-- merged-worktree-memory source:/old file:a.md merged-at:T -->\n"
            b"### 09:30\n- entry one\n\n"
            b"\n<!-- merged-worktree-memory entry:" + two_hash.encode() + b" -->\n"
            b"<!-- merged-worktree-memory source:/old file:a.md merged-at:T -->\n",
        )
        self.run_merger()

        other = self.root / "other"
        (other / ".memsearch" / "memory").mkdir(parents=True)
        (other / ".memsearch" / "memory" / "a.md").write_bytes(entry_two)
        subprocess.run(["git", "-C", str(other), "init", "-q"], check=True)
        result = subprocess.run(
            [*MERGER_COMMAND, "--source", str(other), "--target", str(self.target)],
            env=merger_env(self.missing_config),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

        self.assertIn("merged=1", result.stdout)
        self.assertIn(b"- entry two", (self.target_memory / "a.md").read_bytes())

    def test_dry_run_writes_nothing(self) -> None:
        self.write_source("a.md", GOLDEN_SOURCE)

        result = self.run_merger("--dry-run")

        self.assertIn(f"Would merge a.md entry {GOLDEN_HASHES[0]}", result.stdout)
        self.assertIn("Would scan invalid UTF-8", result.stdout)
        self.assertIn("without rewriting live files", result.stdout)
        self.assertFalse((self.target_memory / "a.md").exists())

    def test_source_equal_to_target_is_a_noop(self) -> None:
        result = subprocess.run(
            [*MERGER_COMMAND, "--source", str(self.source), "--target", str(self.source)],
            env=merger_env(self.missing_config),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("no memory merge needed", result.stdout)

    def test_worktree_without_memsearch_is_a_clean_noop(self) -> None:
        # 没装 memsearch 的机器上 worktree 里不会有 .memsearch/, 集成流程仍会
        # 无条件调用本脚本: 必须正常返回, 且不留下任何目录.
        bare = self.root / "bare"
        bare.mkdir()
        subprocess.run(["git", "-C", str(bare), "init", "-q"], check=True)
        fresh_target = self.root / "fresh"
        fresh_target.mkdir()

        result = subprocess.run(
            [*MERGER_COMMAND, "--source", str(bare), "--target", str(fresh_target)],
            env=merger_env(self.missing_config),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Nothing to merge", result.stdout)
        self.assertFalse((bare / ".memsearch").exists())
        self.assertFalse((fresh_target / ".memsearch").exists())

    def test_empty_memory_directory_is_a_clean_noop(self) -> None:
        (self.target_memory / "keep.md").write_bytes(b"### 09:30\n- kept\n")

        result = subprocess.run(
            [*MERGER_COMMAND, "--source", str(self.source), "--target", str(self.target)],
            env=merger_env(self.missing_config),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Nothing to merge", result.stdout)
        self.assertEqual(b"### 09:30\n- kept\n", (self.target_memory / "keep.md").read_bytes())

    def test_merge_messages_default_to_chinese_without_locale(self) -> None:
        result = subprocess.run(
            [*MERGER_COMMAND, "--source", str(self.source), "--target", str(self.target)],
            env=merger_env(self.missing_config, language=None),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("无需合并", result.stdout)
        self.assertNotIn("Nothing to merge", result.stdout)

    def test_merge_help_defaults_to_chinese_without_locale(self) -> None:
        result = subprocess.run(
            [*MERGER_COMMAND, "--help"],
            env=merger_env(self.missing_config, language=None),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("用法:", result.stdout)
        self.assertNotIn("usage:", result.stdout.lower())

    def test_merge_lang_override_beats_config_language(self) -> None:
        config_path = self.root / "onevoke-config.json"
        config_path.write_text(
            '{"schema_version":1,"welcome_complete":true,"kanban_agent":"codex","launcher":"tmux","language":"cn","reviewers":{"PM":"codex","CSA":"codex","Hacker":"codex","QA":"codex"},"memsearch":{"enabled":false}}\n',
            encoding="utf-8",
        )
        result = subprocess.run(
            [*MERGER_COMMAND, "--lang", "en", "--source", str(self.source), "--target", str(self.target)],
            env=merger_env(config_path),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Nothing to merge", result.stdout)

    def test_merge_invalid_argument_defaults_to_chinese_without_locale(self) -> None:
        result = subprocess.run(
            [*MERGER_COMMAND, "--nope"],
            env=merger_env(self.missing_config, language=None),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("无法识别的参数", result.stderr)
        self.assertNotIn("unrecognized arguments", result.stderr)

    def test_source_memory_symlink_file_fails_closed(self) -> None:
        """来源 `*.md` 若是软链, 必须失败, 禁止当空目录成功后清 worktree."""
        real = self.source_memory / "real-body.md"
        real.write_bytes(b"### 09:30\n- secret memory that must not be dropped\n")
        link = self.source_memory / "a.md"
        try:
            link.symlink_to(real.name)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows symlink privilege is unavailable")
            raise
        before = real.read_bytes()

        result = subprocess.run(
            [*MERGER_COMMAND, "--source", str(self.source), "--target", str(self.target)],
            env=merger_env(self.missing_config),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(0, result.returncode, result.stdout)
        if os.name == "nt":
            self.assertIn("reparse point", result.stderr)
        else:
            self.assertIn("must not be a symlink", result.stderr)
        self.assertFalse((self.target_memory / "a.md").exists())
        self.assertEqual(before, real.read_bytes())
        self.assertTrue(link.is_symlink())

    def test_source_memory_disappearance_fails_closed(self) -> None:
        self.write_source("a.md", b"### 09:30\n- first\n")
        shutil.rmtree(self.source_memory)

        with self.assertRaises(SystemExit) as raised:
            merger.read_stable_source_files(self.source_memory)

        self.assertEqual(1, raised.exception.code)

    def test_empty_source_dir_that_gains_a_file_during_check_fails(self) -> None:
        original_assert = merger.assert_source_unchanged

        def create_then_assert(
            source_memory: Path,
            snapshots: dict[Path, bytes],
            expected_identity: tuple[int, int] | None = None,
        ) -> None:
            (source_memory / "late.md").write_bytes(b"### 09:30\n- late first file\n")
            original_assert(source_memory, snapshots, expected_identity)

        with mock.patch.object(
            merger, "assert_source_unchanged", side_effect=create_then_assert
        ):
            with self.assertRaises(SystemExit) as raised:
                merger.merge(str(self.source), str(self.target), dry_run=False)

        self.assertEqual(1, raised.exception.code)
        self.assertTrue((self.source_memory / "late.md").exists())

    def test_unstable_source_snapshot_is_rejected(self) -> None:
        source_file = self.write_source("a.md", b"### 09:30\n- first\n")
        original_attempts = merger.SOURCE_STABLE_ATTEMPTS
        original_delay = merger.SOURCE_STABLE_DELAY_SECONDS
        merger.SOURCE_STABLE_ATTEMPTS = 2
        merger.SOURCE_STABLE_DELAY_SECONDS = 0.01
        reads = {"count": 0}
        real_safe_read = merger.read_regular_file_with_identity_nofollow

        def flaky_safe_read(root: Path, path: Path):
            identity, data = real_safe_read(root, path)
            if path == source_file:
                reads["count"] += 1
                if reads["count"] % 2 == 0:
                    data += b"### 09:45\n- late\n"
            return identity, data

        try:
            with mock.patch.object(
                merger,
                "read_regular_file_with_identity_nofollow",
                side_effect=flaky_safe_read,
            ):
                with self.assertRaises(SystemExit) as raised:
                    merger.read_stable_source_files(self.source_memory)
        finally:
            merger.SOURCE_STABLE_ATTEMPTS = original_attempts
            merger.SOURCE_STABLE_DELAY_SECONDS = original_delay

        self.assertEqual(1, raised.exception.code)

    @unittest.skipUnless(os.name == "nt", "Windows file-identity regression")
    def test_windows_source_replaced_with_same_bytes_between_reads_is_unstable(self) -> None:
        source_file = self.write_source("a.md", b"### 09:30\n- same bytes\n")
        backup = self.source_memory / "old-copy.bin"
        original_attempts = merger.SOURCE_STABLE_ATTEMPTS
        original_delay = merger.SOURCE_STABLE_DELAY_SECONDS
        original_read = merger.read_regular_file_with_identity_nofollow
        replaced = {"value": False}

        def replace_after_first_read(root: Path, path: Path):
            identity, data = original_read(root, path)
            if path == source_file and not replaced["value"]:
                replaced["value"] = True
                os.replace(source_file, backup)
                source_file.write_bytes(data)
            return identity, data

        merger.SOURCE_STABLE_ATTEMPTS = 1
        merger.SOURCE_STABLE_DELAY_SECONDS = 0.01
        try:
            with mock.patch.object(
                merger,
                "read_regular_file_with_identity_nofollow",
                side_effect=replace_after_first_read,
            ):
                with self.assertRaises(SystemExit) as raised:
                    merger.read_stable_source_files(self.source_memory)
        finally:
            merger.SOURCE_STABLE_ATTEMPTS = original_attempts
            merger.SOURCE_STABLE_DELAY_SECONDS = original_delay

        self.assertEqual(1, raised.exception.code)
        self.assertTrue(replaced["value"])
        self.assertEqual(backup.read_bytes(), source_file.read_bytes())

    def test_new_source_file_after_merge_fails(self) -> None:
        self.write_source("a.md", b"### 09:30\n- first\n")
        original_assert = merger.assert_source_unchanged

        def add_file_then_assert(
            source_memory: Path,
            snapshots: dict[Path, bytes],
            expected_identity: tuple[int, int] | None = None,
        ) -> None:
            (source_memory / "b.md").write_bytes(b"### 09:40\n- late file\n")
            original_assert(source_memory, snapshots, expected_identity)

        with mock.patch.object(
            merger, "assert_source_unchanged", side_effect=add_file_then_assert
        ):
            with self.assertRaises(SystemExit) as raised:
                merger.merge(str(self.source), str(self.target), dry_run=False)

        self.assertEqual(1, raised.exception.code)
        self.assertTrue((self.source_memory / "b.md").exists())

    def test_source_late_append_during_stable_read_fails(self) -> None:
        source_file = self.write_source("a.md", b"### 09:30\n- first\n")
        original_attempts = merger.SOURCE_STABLE_ATTEMPTS
        original_delay = merger.SOURCE_STABLE_DELAY_SECONDS
        merger.SOURCE_STABLE_ATTEMPTS = 2
        merger.SOURCE_STABLE_DELAY_SECONDS = 0.05
        writer_stop = threading.Event()

        def append_during_merge() -> None:
            while not writer_stop.is_set():
                source_file.write_bytes(
                    source_file.read_bytes() + b"### 09:45\n- racing\n"
                )
                time.sleep(0.02)

        thread = threading.Thread(target=append_during_merge)
        thread.start()
        try:
            with self.assertRaises(SystemExit) as raised:
                merger.merge(str(self.source), str(self.target), dry_run=False)
        finally:
            writer_stop.set()
            thread.join(timeout=2)
            merger.SOURCE_STABLE_ATTEMPTS = original_attempts
            merger.SOURCE_STABLE_DELAY_SECONDS = original_delay

        self.assertEqual(1, raised.exception.code)

    def test_source_changed_after_merge_fails(self) -> None:
        source_file = self.write_source("a.md", b"### 09:30\n- first\n")
        original_assert = merger.assert_source_unchanged

        def mutate_then_assert(
            source_memory: Path,
            snapshots: dict[Path, bytes],
            expected_identity: tuple[int, int] | None = None,
        ) -> None:
            source_file.write_bytes(b"### 09:30\n- first\n### 09:40\n- late\n")
            original_assert(source_memory, snapshots, expected_identity)

        with mock.patch.object(
            merger, "assert_source_unchanged", side_effect=mutate_then_assert
        ):
            with self.assertRaises(SystemExit) as raised:
                merger.merge(str(self.source), str(self.target), dry_run=False)

        self.assertEqual(1, raised.exception.code)
        self.assertIn(b"late", source_file.read_bytes())

    def test_source_replaced_with_same_bytes_after_snapshot_fails_final_check(self) -> None:
        source_file = self.write_source("a.md", b"### 09:30\n- unchanged bytes\n")
        backup = self.source_memory / "original.bin"
        original_assert = merger.assert_source_unchanged
        replaced = {"value": False}

        def replace_then_assert(
            source_memory: Path,
            snapshots: dict[Path, bytes],
            expected_identity: tuple[int, ...] | None = None,
        ) -> None:
            data = source_file.read_bytes()
            os.replace(source_file, backup)
            source_file.write_bytes(data)
            replaced["value"] = True
            original_assert(source_memory, snapshots, expected_identity)

        with mock.patch.object(
            merger, "assert_source_unchanged", side_effect=replace_then_assert
        ):
            with self.assertRaises(SystemExit) as raised:
                merger.merge(str(self.source), str(self.target), dry_run=False)

        self.assertEqual(1, raised.exception.code)
        self.assertTrue(replaced["value"])
        self.assertEqual(backup.read_bytes(), source_file.read_bytes())

    def test_merge_does_not_rewrite_target_so_concurrent_appends_survive(self) -> None:
        target_file = self.target_memory / "a.md"
        dirty = b"### 08:00\n- dirty \xff byte\n"
        target_file.write_bytes(dirty)
        before_ino = target_file.stat().st_ino
        self.write_source("a.md", b"### 09:30\n- clean entry\n")
        entered = threading.Event()
        release = threading.Event()
        original_scan = merger.scan_dirty_files

        def blocked_scan(directory: Path) -> tuple[int, int]:
            entered.set()
            if not release.wait(timeout=2):
                raise TimeoutError("writer did not finish during scan")
            return original_scan(directory)

        def append_during_scan() -> None:
            if not entered.wait(timeout=2):
                raise TimeoutError("scan never started")
            with open(target_file, "ab") as handle:
                handle.write(b"### 09:50\n- concurrent target append\n")
            release.set()

        thread = threading.Thread(target=append_during_scan)
        with mock.patch.object(merger, "scan_dirty_files", side_effect=blocked_scan):
            thread.start()
            try:
                merger.merge(str(self.source), str(self.target), dry_run=False)
            finally:
                release.set()
                thread.join(timeout=2)

        final = target_file.read_bytes()
        self.assertEqual(before_ino, target_file.stat().st_ino)
        self.assertIn(b"concurrent target append", final)
        self.assertIn(b"\xff", final)
        self.assertIn(b"clean entry", final)

    @unittest.skipUnless(os.name == "nt", "Windows reparse regression")
    def test_windows_rejects_source_and_target_root_junctions(self) -> None:
        self.write_source("a.md", b"### 09:30\n- protected\n")
        source_link = self.root / "source-junction"
        target_link = self.root / "target-junction"
        self.make_junction(source_link, self.source)
        self.make_junction(target_link, self.target)

        for source, target in (
            (source_link, self.target),
            (self.source, target_link),
        ):
            with self.subTest(source=source, target=target):
                result = self.run_merger_paths(source, target)
                self.assertNotEqual(0, result.returncode, result.stdout)
                self.assertIn("reparse point", result.stderr)

        self.assertFalse((self.target_memory / "a.md").exists())

    @unittest.skipUnless(os.name == "nt", "Windows reparse regression")
    def test_windows_rejects_source_state_and_memory_junctions(self) -> None:
        for component in (".memsearch", "memory", "entry"):
            with self.subTest(component=component):
                case_source = self.root / f"source-{component.lstrip('.')}"
                case_source.mkdir()
                subprocess.run(["git", "-C", str(case_source), "init", "-q"], check=True)
                outside = self.root / f"outside-source-{component.lstrip('.')}"
                if component == ".memsearch":
                    (outside / "memory").mkdir(parents=True)
                    (outside / "memory" / "a.md").write_bytes(
                        b"### 09:30\n- outside\n"
                    )
                    link = case_source / ".memsearch"
                elif component == "memory":
                    (case_source / ".memsearch").mkdir()
                    outside.mkdir()
                    (outside / "a.md").write_bytes(b"### 09:30\n- outside\n")
                    link = case_source / ".memsearch" / "memory"
                else:
                    memory = case_source / ".memsearch" / "memory"
                    memory.mkdir(parents=True)
                    outside.mkdir()
                    link = memory / "ignored-junction"
                self.make_junction(link, outside)

                result = self.run_merger_paths(case_source, self.target)
                self.assertNotEqual(0, result.returncode, result.stdout)
                self.assertIn("reparse point", result.stderr)

        self.assertFalse((self.target_memory / "a.md").exists())

    @unittest.skipUnless(os.name == "nt", "Windows reparse regression")
    def test_windows_rejects_target_state_memory_lock_and_file_reparse(self) -> None:
        cases = ("state", "memory", "lock", "file", "nested")
        for case in cases:
            with self.subTest(case=case):
                case_source = self.root / f"case-source-{case}"
                case_target = self.root / f"case-target-{case}"
                source_memory = case_source / ".memsearch" / "memory"
                target_memory = case_target / ".memsearch" / "memory"
                source_memory.mkdir(parents=True)
                target_memory.mkdir(parents=True)
                (source_memory / "a.md").write_bytes(b"### 09:30\n- protected\n")
                subprocess.run(["git", "-C", str(case_source), "init", "-q"], check=True)
                outside = self.root / f"outside-target-{case}"
                outside.mkdir()
                sentinel = outside / "sentinel.txt"
                sentinel.write_text("untouched", encoding="utf-8")

                if case == "state":
                    shutil.rmtree(case_target / ".memsearch")
                    link = case_target / ".memsearch"
                elif case == "memory":
                    shutil.rmtree(target_memory)
                    link = target_memory
                elif case == "lock":
                    link = case_target / ".memsearch" / ".merge-worktree-memory.lock"
                elif case == "file":
                    link = target_memory / "a.md"
                else:
                    link = target_memory / "nested"
                self.make_junction(link, outside)

                result = self.run_merger_paths(case_source, case_target)
                self.assertNotEqual(0, result.returncode, result.stdout)
                self.assertIn("reparse point", result.stderr)
                self.assertEqual("untouched", sentinel.read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "nt", "Windows ACL regression")
    def test_windows_merge_migrates_entire_target_memory_boundary_to_private_acl(self) -> None:
        self.write_source("a.md", b"### 09:30\n- new\n")
        target_file = self.target_memory / "a.md"
        target_file.write_bytes(b"### 08:00\n- old\n")
        nested = self.target_memory / "nested"
        nested.mkdir()
        nested_file = nested / "old.md"
        nested_file.write_bytes(b"### 07:00\n- nested\n")
        auxiliary = self.target_memory / "index.bin"
        auxiliary.write_bytes(b"index")
        for path in (
            self.target / ".memsearch",
            self.target_memory,
            target_file,
            nested,
            nested_file,
            auxiliary,
        ):
            reset = subprocess.run(
                ["icacls.exe", str(path), "/reset"],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, reset.returncode, reset.stderr)

        result = self.run_merger()
        self.assertEqual(0, result.returncode, result.stderr)
        lock_path = self.target / ".memsearch" / ".merge-worktree-memory.lock"
        for path in (
            self.target / ".memsearch",
            self.target_memory,
            lock_path,
            target_file,
            nested,
            nested_file,
            auxiliary,
        ):
            with self.subTest(path=path):
                self.assert_private_acl(path)

    @unittest.skipUnless(os.name == "nt", "Windows replacement-race regression")
    def test_windows_target_handle_blocks_replacement_between_read_and_append(self) -> None:
        self.write_source("a.md", b"### 09:30\n- new entry\n")
        target_file = self.target_memory / "a.md"
        target_file.write_bytes(b"### 08:00\n- existing entry\n")
        intruder = self.target_memory / "intruder.md"
        original_open = merger.open_private_append_file_nofollow
        target_opens = {"count": 0}
        attempted = {"value": False}

        @contextlib.contextmanager
        def attempt_replace(root: Path, path: Path):
            with original_open(root, path) as handle:
                if path == target_file:
                    target_opens["count"] += 1
                # 第一次是全边界 preflight，第二次才是去重读取+append 句柄.
                if path == target_file and target_opens["count"] == 2:
                    attempted["value"] = True
                    intruder.write_bytes(b"attacker replacement\n")
                    with self.assertRaises(OSError):
                        os.replace(intruder, target_file)
                yield handle

        with mock.patch.object(
            merger,
            "open_private_append_file_nofollow",
            side_effect=attempt_replace,
        ):
            merger.merge(str(self.source), str(self.target), dry_run=False)

        self.assertTrue(attempted["value"])
        final = target_file.read_bytes()
        self.assertIn(b"existing entry", final)
        self.assertIn(b"new entry", final)
        self.assertEqual(b"attacker replacement\n", intruder.read_bytes())


if __name__ == "__main__":
    unittest.main()
