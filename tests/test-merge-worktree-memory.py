#!/usr/bin/env python3

import importlib.util
import fcntl
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MERGER = PROJECT_ROOT / "bin" / "merge-worktree-memory.py"

_spec = importlib.util.spec_from_file_location("merge_worktree_memory", MERGER)
merger = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(merger)

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
        self.root = Path(self.temp.name)
        self.source = self.root / "src"
        self.target = self.root / "tgt"
        self.source_memory = self.source / ".memsearch" / "memory"
        self.target_memory = self.target / ".memsearch" / "memory"
        self.source_memory.mkdir(parents=True)
        self.target_memory.mkdir(parents=True)
        subprocess.run(["git", "-C", str(self.source), "init", "-q"], check=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_source(self, name: str, data: bytes) -> Path:
        path = self.source_memory / name
        path.write_bytes(data)
        return path

    def run_merger(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                str(MERGER),
                "--source", str(self.source),
                "--target", str(self.target),
                *args,
            ],
            text=True,
            capture_output=True,
            check=False,
        )

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

    def test_merge_waits_for_the_target_lock(self) -> None:
        self.write_source("a.md", b"### 09:30\n- serialized\n")
        lock_path = self.target / ".memsearch" / ".merge-worktree-memory.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            process = subprocess.Popen(
                [str(MERGER), "--source", str(self.source), "--target", str(self.target)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            time.sleep(0.2)
            self.assertIsNone(process.poll(), "merger ignored the held target lock")
            self.assertFalse((self.target_memory / "a.md").exists())
            fcntl.flock(lock, fcntl.LOCK_UN)
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
                [str(MERGER), "--source", str(source), "--target", str(self.target)],
                text=True,
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
            [str(MERGER), "--source", str(other), "--target", str(self.target)],
            text=True,
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
            [str(MERGER), "--source", str(self.source), "--target", str(self.source)],
            text=True,
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
            [str(MERGER), "--source", str(bare), "--target", str(fresh_target)],
            text=True,
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
            [str(MERGER), "--source", str(self.source), "--target", str(self.target)],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Nothing to merge", result.stdout)
        self.assertEqual(b"### 09:30\n- kept\n", (self.target_memory / "keep.md").read_bytes())

    def test_unstable_source_snapshot_is_rejected(self) -> None:
        source_file = self.write_source("a.md", b"### 09:30\n- first\n")
        original_attempts = merger.SOURCE_STABLE_ATTEMPTS
        original_delay = merger.SOURCE_STABLE_DELAY_SECONDS
        merger.SOURCE_STABLE_ATTEMPTS = 2
        merger.SOURCE_STABLE_DELAY_SECONDS = 0.01
        reads = {"count": 0}
        real_read_bytes = Path.read_bytes

        def flaky_read(self: Path) -> bytes:
            data = real_read_bytes(self)
            if self == source_file:
                reads["count"] += 1
                if reads["count"] % 2 == 0:
                    return data + b"### 09:45\n- late\n"
            return data

        try:
            with mock.patch.object(Path, "read_bytes", flaky_read):
                with self.assertRaises(SystemExit) as raised:
                    merger.read_stable_source_files(self.source_memory)
        finally:
            merger.SOURCE_STABLE_ATTEMPTS = original_attempts
            merger.SOURCE_STABLE_DELAY_SECONDS = original_delay

        self.assertEqual(1, raised.exception.code)

    def test_new_source_file_after_merge_fails(self) -> None:
        self.write_source("a.md", b"### 09:30\n- first\n")
        original_assert = merger.assert_source_unchanged

        def add_file_then_assert(source_memory: Path, snapshots: dict[Path, bytes]) -> None:
            (source_memory / "b.md").write_bytes(b"### 09:40\n- late file\n")
            original_assert(source_memory, snapshots)

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
            source_memory: Path, snapshots: dict[Path, bytes]
        ) -> None:
            source_file.write_bytes(b"### 09:30\n- first\n### 09:40\n- late\n")
            original_assert(source_memory, snapshots)

        with mock.patch.object(
            merger, "assert_source_unchanged", side_effect=mutate_then_assert
        ):
            with self.assertRaises(SystemExit) as raised:
                merger.merge(str(self.source), str(self.target), dry_run=False)

        self.assertEqual(1, raised.exception.code)
        self.assertIn(b"late", source_file.read_bytes())

    def test_merge_does_not_rewrite_target_so_concurrent_appends_survive(self) -> None:
        target_file = self.target_memory / "a.md"
        target_file.write_bytes(b"### 08:00\n- dirty \xff byte\n")
        self.write_source("a.md", b"### 09:30\n- clean entry\n")

        def append_during_merge() -> None:
            time.sleep(0.05)
            with open(target_file, "ab") as handle:
                handle.write(b"### 09:50\n- concurrent target append\n")

        thread = threading.Thread(target=append_during_merge)
        thread.start()
        try:
            result = self.run_merger()
        finally:
            thread.join(timeout=2)

        self.assertEqual(0, result.returncode, result.stderr)
        final = target_file.read_bytes()
        self.assertIn(b"concurrent target append", final)
        self.assertIn(b"\xff", final)
        self.assertIn(b"clean entry", final)


if __name__ == "__main__":
    unittest.main()
