#!/usr/bin/env python3

"""把任务 worktree 的 memsearch 记忆并入主 worktree.

不依赖 memsearch 本身: 只读写 `.memsearch/memory/*.md`, 从不调用它的二进制.
未安装 memsearch 时 worktree 里没有该目录, 本脚本报告无事可做并以 0 退出,
不创建任何目录, 集成流程可以无条件调用它.

POSIX 合并成功后安全核验来源 `.memsearch/.watch.pid`; Linux 使用 pidfd 固定
进程身份, 并在返回成功前终止监控该来源 memory 的 MemSearch watcher. 其他
POSIX 平台遇到仍存活的 watcher 时 fail-closed. 无法确认进程身份或等待退出
失败时拒绝成功, 让调用方保留 worktree; `--dry-run` 只报告而不发送信号.

全程按字节处理. 记忆文件由 hook 自动追加, 历史数据可能含非法 UTF-8 序列;
先解码再处理会丢字节或直接抛错, 因此切分, 归一化和哈希都在 bytes 上完成.
新合并条目在写入前丢弃非法 UTF-8; 不对可能仍被追加的目标整文件 rewrite
(无 MemSearch 协作封口协议时, 改写无法证明不丢并发字节). 既有脏文件只扫描报告.

条目哈希与既有记忆文件中的 `<!-- merged-worktree-memory entry:... -->` 标记
兼容, 改动切分或归一化逻辑会让已合并条目重新判定为新条目.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

_BIN = Path(__file__).resolve().parent
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from onevoke_config import (
    ARGPARSE_ZH,
    LANGUAGES,
    LocalizedArgumentParser,
    apply_language_argument,
    bind_effective_language,
    language_text,
)
from onevoke_fs import (
    directory_exists_nofollow,
    directory_identity_nofollow,
    ensure_private_directory_nofollow,
    exclusive_file_lock,
    list_directory_nofollow,
    open_private_append_file_nofollow,
    read_regular_file_if_exists_nofollow,
    read_regular_file_with_identity_nofollow,
    validate_directory_path_nofollow,
)

t = language_text


# 与 C locale 下 awk 的 [[:space:]] 对齐; 记录内不含换行.
BLANK = re.compile(rb"^[ \t\v\f\r]*$")
ENTRY_HEADER = re.compile(rb"^### [0-9][0-9]:[0-9][0-9][ \t\v\f\r]*$")
SESSION_HEADER = re.compile(rb"^## Session ")
ENTRY_MARKER = re.compile(
    rb"^<!-- merged-worktree-memory (?:entry|file-entry):([0-9a-f]+) -->$"
)
COMPLETION_MARKER = re.compile(
    rb"^<!-- merged-worktree-memory (entry|file-entry):([0-9a-f]+) -->$"
)
SOURCE_MARKER = re.compile(rb"^<!-- merged-worktree-memory source:.* -->$")
# Stop hook 可能在合并窗口内继续追加; 两次读取一致才视为来源稳定.
# 注意: 进程成功返回之后的写入需要 MemSearch Stop hook 与清理流程的协作封口
# 协议才能绝对消除, 本脚本只能 fail-closed 本进程可观测窗口; 见任务范围.
SOURCE_STABLE_ATTEMPTS = 5
SOURCE_STABLE_DELAY_SECONDS = 0.1
WATCH_STOP_TIMEOUT_SECONDS = 5.0
WATCH_STOP_POLL_SECONDS = 0.05
WATCH_PID = re.compile(rb"[1-9][0-9]*")


class SourceSnapshots(dict[Path, bytes]):
    """兼容既有 dict 消费者，同时携带每个来源文件的稳定身份."""

    def __init__(
        self,
        values: dict[Path, bytes] | None = None,
        identities: dict[Path, tuple[int, ...]] | None = None,
    ) -> None:
        super().__init__(values or {})
        self.identities = dict(identities or {})


class MergeResult:
    """把 watcher 停止动作绑定到本轮实际核验过的来源目录身份."""

    __slots__ = ("stop_watcher", "source_identity")

    def __init__(
        self, stop_watcher: bool, source_identity: tuple[int, ...] | None
    ) -> None:
        self.stop_watcher = stop_watcher
        self.source_identity = source_identity


class ProcessSnapshot:
    """来自同一个 `/proc/<pid>` 的可复核进程身份和启动状态."""

    __slots__ = (
        "state",
        "start_time",
        "process_group",
        "executable_identity",
        "args",
    )

    def __init__(
        self,
        state: str,
        start_time: str,
        process_group: int,
        executable_identity: tuple[int, int] | None,
        args: list[str],
    ) -> None:
        self.state = state
        self.start_time = start_time
        self.process_group = process_group
        self.executable_identity = executable_identity
        self.args = args

    def same_identity(self, other: ProcessSnapshot) -> bool:
        return (
            self.start_time == other.start_time
            and self.process_group == other.process_group
            and self.executable_identity == other.executable_identity
            and self.args == other.args
        )


def die(message: str) -> None:
    print(f"{t('错误', 'ERROR')}: {message}", file=sys.stderr)
    raise SystemExit(1)


def die_both(zh: str, en: str) -> None:
    die(t(zh, en))


def split_lines(data: bytes) -> list[bytes]:
    """按换行切成记录. 末尾换行不产生空记录."""
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    return lines


def fence_marker(line: bytes) -> tuple[bytes, int] | None:
    """识别代码围栏的起止标记, 返回 (围栏字符, 长度)."""
    stripped = line.lstrip(b" \t")
    for char in (b"`", b"~"):
        if stripped.startswith(char * 3):
            return char, len(stripped) - len(stripped.lstrip(char))
    return None


def emit_entry_blocks(data: bytes) -> list[bytes]:
    """按 `### HH:MM` 切分条目. 围栏内的同形行是正文, 不是标题.

    合并标记是结构行不是正文: 它们出现在下一条目的 `### HH:MM` 之前, 若并入
    上一条目的块, 就会随该块被写进目标文件, 让目标带上一个正文尚未合并的条目
    标记, 后续真正携带该条目的 worktree 会被误判为重复而静默丢弃.
    """
    blocks: list[bytes] = []
    block: list[bytes] | None = None
    fence: tuple[bytes, int] | None = None

    def flush() -> None:
        nonlocal block
        if block is not None:
            blocks.append(b"".join(block))
            block = None

    for line in split_lines(data):
        marker = fence_marker(line)
        if marker:
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and marker[1] >= fence[1]:
                fence = None

        if fence is not None:
            if block is not None:
                block.append(line + b"\n")
            continue

        if ENTRY_HEADER.match(line):
            flush()
            block = [line + b"\n"]
            continue

        if (
            SESSION_HEADER.match(line)
            or ENTRY_MARKER.match(line)
            or SOURCE_MARKER.match(line)
        ):
            flush()
            continue

        if block is not None:
            block.append(line + b"\n")

    flush()
    return blocks


def emit_fallback_block(data: bytes) -> bytes:
    """没有 `### HH:MM` 条目时, 整个文件去掉 session 标题和空行后当作一条."""
    return b"".join(
        line + b"\n"
        for line in split_lines(data)
        if not SESSION_HEADER.match(line) and not BLANK.match(line)
    )


def normalized_hash(data: bytes) -> str:
    """去掉合并标记和首尾空行后取 sha256, 使同一条目在多次合并中稳定."""
    lines = [
        line
        for line in split_lines(data)
        if not ENTRY_MARKER.match(line) and not SOURCE_MARKER.match(line)
    ]

    start = 0
    while start < len(lines) and BLANK.match(lines[start]):
        start += 1
    end = len(lines) - 1
    while end >= start and BLANK.match(lines[end]):
        end -= 1

    body = b"".join(line + b"\n" for line in lines[start : end + 1])
    return hashlib.sha256(body).hexdigest()


def hashes_from_target_data(data: bytes) -> set[str]:
    """从固定快照提取已完整提交或可由正文证明完整的条目哈希.

    新格式 completion marker 位于正文之后；旧格式 marker 位于 source 元数据
    之前。旧 marker 可能已写完而正文因 ENOSPC 截断，因此只有其紧随正文可按
    kind 完整解析且 normalized hash 一致时才兼容，不能无条件信任 marker.
    """
    lines = split_lines(data)
    events: list[tuple[int, str, re.Match[bytes] | None]] = []
    fence: tuple[bytes, int] | None = None
    for index, line in enumerate(lines):
        delimiter = fence_marker(line)
        if delimiter:
            if fence is None:
                fence = delimiter
            elif delimiter[0] == fence[0] and delimiter[1] >= fence[1]:
                fence = None
            continue
        if fence is not None:
            continue
        marker = COMPLETION_MARKER.match(line)
        if marker:
            events.append((index, "marker", marker))
        elif SOURCE_MARKER.match(line):
            events.append((index, "source", None))

    def body_matches(kind: str, expected_hash: str, body_lines: list[bytes]) -> bool:
        body = b"\n".join(body_lines)
        if body_lines:
            body += b"\n"
        if kind == "entry":
            blocks = emit_entry_blocks(body)
            return (
                len(blocks) == 1
                and normalized_hash(blocks[0]) == expected_hash
            )
        if emit_entry_blocks(body):
            return False
        fallback = emit_fallback_block(body)
        return bool(fallback) and normalized_hash(fallback) == expected_hash

    hashes: set[str] = set()
    for event_index, (line_index, event_kind, marker) in enumerate(events):
        if event_kind != "marker" or marker is None:
            continue
        kind = marker.group(1).decode()
        expected_hash = marker.group(2).decode()

        # 新格式: source metadata -> body -> completion marker.
        if event_index > 0 and events[event_index - 1][1] == "source":
            source_index = events[event_index - 1][0]
            if body_matches(kind, expected_hash, lines[source_index + 1:line_index]):
                hashes.add(expected_hash)
                continue

        # 旧格式: marker 后必须立即是 source metadata，正文延伸到下一结构事件.
        if event_index + 1 < len(events):
            source_index, next_kind, _ = events[event_index + 1]
            if next_kind == "source" and source_index == line_index + 1:
                body_end = (
                    events[event_index + 2][0]
                    if event_index + 2 < len(events)
                    else len(lines)
                )
                if body_matches(
                    kind,
                    expected_hash,
                    lines[source_index + 1:body_end],
                ):
                    hashes.add(expected_hash)

    hashes.update(normalized_hash(block) for block in emit_entry_blocks(data))

    fallback = emit_fallback_block(data)
    if fallback:
        hashes.add(normalized_hash(fallback))
    return hashes


def target_hashes(path: Path, target_root: Path | None = None) -> set[str]:
    """目标文件中已存在的条目哈希: 合并标记, 重新切分的条目, 以及回退整块."""
    if os.name == "nt":
        if target_root is None:
            raise ValueError("target_root is required for a protected Windows read")
        data = read_regular_file_if_exists_nofollow(target_root, path)
        return set() if data is None else hashes_from_target_data(data)

    if not path.is_file():
        return set()
    return hashes_from_target_data(path.read_bytes())


def clean_bytes(data: bytes) -> bytes:
    """丢弃非法 UTF-8 序列; 已有的合法 U+FFFD 保留."""
    return data.decode("utf-8", errors="ignore").encode("utf-8")


def scan_dirty_files(directory: Path, target_root: Path | None = None) -> tuple[int, int]:
    """扫描非法 UTF-8, 但不改写已有文件.

    新合并条目在写入前已清理. 对可能被 Stop hook 并发追加的目标文件做
    truncate/replace 无法在无协作协议下证明不丢字节, 因此这里 fail-closed:
    只报告脏文件数量, 留给空闲时的运维清理, 合并本身不覆盖活跃目标.
    """
    scanned = 0
    dirty = 0
    if os.name == "nt":
        if target_root is None:
            memory_root = next(
                (
                    candidate
                    for candidate in (directory, *directory.parents)
                    if candidate.name == "memory"
                    and candidate.parent.name == ".memsearch"
                ),
                None,
            )
            if memory_root is None:
                raise ValueError(
                    "cannot infer protected worktree root from memory directory"
                )
            target_root = memory_root.parent.parent
        for name, kind in list_directory_nofollow(target_root, directory):
            path = directory / name
            if kind == "directory":
                ensure_private_directory_nofollow(target_root, path)
                sub_scanned, sub_dirty = scan_dirty_files(path, target_root)
                scanned += sub_scanned
                dirty += sub_dirty
            elif kind == "file":
                # 即使不是 markdown，也把 memory 边界内的既有普通文件迁移到
                # 当前用户独占 DACL；markdown 同时在这一固定句柄上完成扫描.
                with open_private_append_file_nofollow(target_root, path) as handle:
                    if path.suffix.lower() == ".md":
                        scanned += 1
                        handle.seek(0)
                        original = handle.read()
                        if clean_bytes(original) != original:
                            dirty += 1
        return scanned, dirty

    for entry in list(os.scandir(directory)):
        path = Path(entry.path)
        if entry.is_dir(follow_symlinks=False):
            sub_scanned, sub_dirty = scan_dirty_files(path)
            scanned += sub_scanned
            dirty += sub_dirty
        elif entry.is_file(follow_symlinks=False) and path.suffix.lower() == ".md":
            scanned += 1
            original = path.read_bytes()
            if clean_bytes(original) != original:
                dirty += 1
    return scanned, dirty


def source_memory_identity(source_memory: Path) -> tuple[int, ...]:
    """返回来源 memory 目录的 (dev, ino); 缺失/软链/非常规路径直接失败."""
    source_root = source_memory.parent.parent
    try:
        return directory_identity_nofollow(source_root, source_memory)
    except OSError as error:
        die_both(
            f"来源 memory 消失、不安全或不可读: {source_memory}: {error}",
            f"source memory disappeared, is unsafe, or unreadable: "
            f"{source_memory}: {error}",
        )


def list_source_memory_files(source_memory: Path) -> list[Path]:
    """枚举来源 `*.md`; 软链或非普通文件立即失败, 禁止静默跳过."""
    source_memory_identity(source_memory)
    if os.name == "nt":
        source_root = source_memory.parent.parent
        files: list[Path] = []
        for name, kind in sorted(list_directory_nofollow(source_root, source_memory)):
            path = source_memory / name
            if path.suffix.lower() != ".md":
                continue
            if kind != "file":
                die_both(
                    f"来源 memory 条目不是普通文件: {name}; "
                    "拒绝成功退出以免 worktree 被清理",
                    f"source memory entry is not a regular file: {name}; "
                    "refusing success so the worktree is not cleaned",
                )
            files.append(path)
        return files

    files: list[Path] = []
    for path in sorted(source_memory.glob("*.md")):
        if path.is_symlink():
            die_both(
                f"来源 memory 文件不得为符号链接: {path.name}; "
                "拒绝成功退出以免 worktree 被清理",
                f"source memory file must not be a symlink: {path.name}; "
                "refusing success so the worktree is not cleaned",
            )
        if not path.is_file():
            die_both(
                f"来源 memory 条目不是普通文件: {path.name}; "
                "拒绝成功退出以免 worktree 被清理",
                f"source memory entry is not a regular file: {path.name}; "
                "refusing success so the worktree is not cleaned",
            )
        files.append(path)
    return files


def read_stable_source_files(source_memory: Path) -> SourceSnapshots:
    """目录身份、成员、文件身份与内容均须连续两次一致."""
    last_error = t(
        "来源 memory 文件不稳定",
        "source memory files were unstable",
    )
    expected_identity = source_memory_identity(source_memory)
    for attempt in range(1, SOURCE_STABLE_ATTEMPTS + 1):
        try:
            if source_memory_identity(source_memory) != expected_identity:
                die_both(
                    f"来源 memory 目录被替换: {source_memory}",
                    f"source memory directory was replaced: {source_memory}",
                )
            first_files = list_source_memory_files(source_memory)
            names_first = [path.name for path in first_files]
            first: dict[Path, bytes] = {}
            first_identities: dict[Path, tuple[int, ...]] = {}
            for path in first_files:
                identity, data = read_regular_file_with_identity_nofollow(
                    source_memory.parent.parent, path
                )
                first_identities[path] = identity
                first[path] = data
            if [path.name for path in first] != names_first:
                last_error = t(
                    f"列举来源 memory 时目录发生变化 (第 {attempt}/{SOURCE_STABLE_ATTEMPTS} 次)",
                    f"source memory directory changed while listing "
                    f"(attempt {attempt}/{SOURCE_STABLE_ATTEMPTS})",
                )
                time.sleep(SOURCE_STABLE_DELAY_SECONDS)
                continue
            time.sleep(SOURCE_STABLE_DELAY_SECONDS)
            if source_memory_identity(source_memory) != expected_identity:
                die_both(
                    f"读取来源 memory 时目录被替换: {source_memory}",
                    f"source memory directory was replaced while reading: {source_memory}",
                )
            names_second = [path.name for path in list_source_memory_files(source_memory)]
            if names_second != names_first:
                last_error = t(
                    f"读取时来源 memory 目录成员发生变化 (第 {attempt}/{SOURCE_STABLE_ATTEMPTS} 次); "
                    "Stop hook 可能仍在写入",
                    f"source memory directory membership changed while reading "
                    f"(attempt {attempt}/{SOURCE_STABLE_ATTEMPTS}); "
                    "Stop hook may still be writing",
                )
                continue
            stable = True
            for path, data in first.items():
                current_identity, current_data = (
                    read_regular_file_with_identity_nofollow(
                        source_memory.parent.parent, path
                    )
                )
                unchanged = (
                    current_identity == first_identities[path]
                    and current_data == data
                )
                if not unchanged:
                    stable = False
                    last_error = t(
                        f"读取时来源 memory 发生变化: {path.name} "
                        f"(第 {attempt}/{SOURCE_STABLE_ATTEMPTS} 次); "
                        "Stop hook 可能仍在写入",
                        f"source memory changed while reading: {path.name} "
                        f"(attempt {attempt}/{SOURCE_STABLE_ATTEMPTS}); "
                        "Stop hook may still be writing",
                    )
                    break
            if stable:
                return SourceSnapshots(first, first_identities)
        except OSError as error:
            last_error = t(
                f"读取来源 memory 失败: {error}",
                f"failed to read source memory: {error}",
            )
    die(last_error)


def assert_source_unchanged(
    source_memory: Path,
    snapshots: dict[Path, bytes],
    expected_identity: tuple[int, ...] | None = None,
) -> None:
    """合并后再核对目录身份、成员与内容; 任一变化都失败, 阻止清理 worktree."""
    try:
        identity = source_memory_identity(source_memory)
        if expected_identity is not None and identity != expected_identity:
            die_both(
                f"合并后来源 memory 目录被替换: {source_memory}",
                f"source memory directory was replaced after merge: {source_memory}",
            )
        current_names = {path.name for path in list_source_memory_files(source_memory)}
    except OSError as error:
        die_both(
            f"合并后来源 memory 不可读: {error}",
            f"source memory unreadable after merge: {error}",
        )
    expected_names = {path.name for path in snapshots}
    if current_names != expected_names:
        die_both(
            "合并后来源 memory 目录成员发生变化; 拒绝成功退出以免 worktree 被清理",
            "source memory directory membership changed after merge; "
            "refusing success so the worktree is not cleaned",
        )
    for path, expected in snapshots.items():
        try:
            current_identity, current = read_regular_file_with_identity_nofollow(
                source_memory.parent.parent, path
            )
        except OSError as error:
            die_both(
                f"合并后来源 memory 不可读: {path}: {error}",
                f"source memory unreadable after merge: {path}: {error}",
            )
        expected_file_identity = (
            snapshots.identities.get(path)
            if isinstance(snapshots, SourceSnapshots)
            else None
        )
        if (
            current != expected
            or (
                expected_file_identity is not None
                and current_identity != expected_file_identity
            )
        ):
            die_both(
                f"合并后来源 memory 发生变化: {path.name}; "
                "拒绝成功退出以免 worktree 被清理",
                f"source memory changed after merge: {path.name}; "
                "refusing success so the worktree is not cleaned",
            )


def read_watch_pid(source_root: str) -> int | None:
    pid_path = Path(source_root) / ".memsearch" / ".watch.pid"
    data = read_regular_file_if_exists_nofollow(Path(source_root), pid_path)
    if data is None:
        return None
    stripped = data.strip()
    if not WATCH_PID.fullmatch(stripped):
        die_both(
            f"MemSearch watcher PID 文件无效: {pid_path}",
            f"invalid MemSearch watcher PID file: {pid_path}",
        )
    pid = int(stripped)
    if pid <= 1 or pid == os.getpid():
        die_both(
            f"MemSearch watcher PID 不安全: {pid}",
            f"unsafe MemSearch watcher PID: {pid}",
        )
    return pid


def linux_process_snapshot(pid: int) -> ProcessSnapshot | None:
    process_dir = Path("/proc") / str(pid)
    try:
        owner = process_dir.stat().st_uid
        stat_data = (process_dir / "stat").read_text(encoding="ascii")
        command_data = (process_dir / "cmdline").read_bytes()
    except FileNotFoundError:
        return None
    except OSError as error:
        die_both(
            f"无法核验 MemSearch watcher PID {pid}: {error}",
            f"cannot verify MemSearch watcher PID {pid}: {error}",
        )
    if owner != os.geteuid():
        die_both(
            f"MemSearch watcher PID {pid} 不属于当前用户",
            f"MemSearch watcher PID {pid} is not owned by the current user",
        )
    try:
        stat_fields = stat_data.rsplit(")", 1)[1].split()
        state = stat_fields[0]
        process_group = int(stat_fields[2])
        start_time = stat_fields[19]
    except (IndexError, ValueError):
        die_both(
            f"无法解析 MemSearch watcher PID {pid} 的进程状态",
            f"cannot parse process state for MemSearch watcher PID {pid}",
        )
    args = [os.fsdecode(part) for part in command_data.split(b"\0") if part]
    executable_identity: tuple[int, int] | None = None
    if not state.startswith("Z"):
        try:
            executable_info = os.stat(process_dir / "exe")
        except FileNotFoundError:
            return None
        except OSError as error:
            die_both(
                f"无法核验 MemSearch watcher PID {pid} 的可执行文件: {error}",
                f"cannot verify executable for MemSearch watcher PID {pid}: {error}",
            )
        executable_identity = executable_info.st_dev, executable_info.st_ino
    return ProcessSnapshot(
        state, start_time, process_group, executable_identity, args
    )


def process_snapshot(pid: int) -> ProcessSnapshot | None:
    """读取 Linux 原生 argv；其他平台不得回落到会丢 argv 边界的 `ps`."""
    if sys.platform.startswith("linux") and Path("/proc").is_dir():
        return linux_process_snapshot(pid)
    die_both(
        "当前平台无法安全核验仍存活的 MemSearch watcher; 拒绝清理 worktree",
        "this platform cannot safely verify a live MemSearch watcher; "
        "refusing to clean the worktree",
    )


def portable_process_state(pid: int) -> str | None:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        die_both(
            f"无法核验 MemSearch watcher PID {pid} 的状态: {error}",
            f"cannot verify state for MemSearch watcher PID {pid}: {error}",
        )
    state = result.stdout.strip().split(None, 1)
    if result.returncode == 0 and state:
        return state[0]
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except OSError as error:
        die_both(
            f"无法核验 MemSearch watcher PID {pid}: {error}",
            f"cannot verify MemSearch watcher PID {pid}: {error}",
        )
    die_both(
        f"无法读取仍存活的 MemSearch watcher PID {pid} 的状态",
        f"cannot read state for live MemSearch watcher PID {pid}",
    )


def watcher_invocation_arguments(
    args: list[str],
) -> tuple[str, str, str | None] | None:
    """返回 (MemSearch 入口, memory, 解释器); 只接受固定启动形态."""
    if len(args) >= 3 and Path(args[0]).name == "memsearch" and args[1] == "watch":
        return args[0], args[2], None
    interpreter = Path(args[0]).name if args else ""
    if not re.fullmatch(r"(?:python|pypy)(?:[0-9.]+)?(?:\.exe)?", interpreter):
        return None
    if (
        len(args) >= 4
        and Path(args[1]).name == "memsearch"
        and args[2] == "watch"
    ):
        return args[1], args[3], args[0]
    return None


def lexical_absolute_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def process_path(path: str, pid: int, *, search_path: bool) -> str | None:
    candidate = path
    if os.path.isabs(candidate):
        return candidate
    if os.sep in candidate or (os.altsep is not None and os.altsep in candidate):
        try:
            return os.path.join(os.readlink(f"/proc/{pid}/cwd"), candidate)
        except OSError:
            return None
    return shutil.which(candidate) if search_path else None


def path_identity(path: str | None) -> tuple[int, int] | None:
    if path is None:
        return None
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_dev, info.st_ino


def invocation_is_trusted_memsearch(
    invocation: tuple[str, str, str | None],
    snapshot: ProcessSnapshot,
    pid: int,
) -> bool:
    executable, _watched, interpreter = invocation
    trusted = shutil.which("memsearch")
    if trusted is None:
        return False
    trusted_identity = path_identity(trusted)
    candidate_identity = path_identity(
        process_path(executable, pid, search_path=True)
    )
    if trusted_identity is None or candidate_identity != trusted_identity:
        return False
    if interpreter is None:
        return snapshot.executable_identity == trusted_identity
    interpreter_identity = path_identity(
        process_path(interpreter, pid, search_path=True)
    )
    return (
        interpreter_identity is not None
        and snapshot.executable_identity == interpreter_identity
    )


def watcher_matches_memory(
    snapshot: ProcessSnapshot, source_memory: Path, pid: int
) -> bool:
    invocation = watcher_invocation_arguments(snapshot.args)
    if invocation is None:
        return False
    _executable, watched, _interpreter = invocation
    if not invocation_is_trusted_memsearch(invocation, snapshot, pid):
        return False
    if not os.path.isabs(watched):
        try:
            watched = os.path.join(os.readlink(f"/proc/{pid}/cwd"), watched)
        except OSError:
            return False
    # 不用 realpath: 来源路径若在合并后被换成软链，不能借此把另一 worktree
    # 的 watcher 解释成合法目标。目录身份另由 assert_stop_source_identity 固定。
    return lexical_absolute_path(watched) == lexical_absolute_path(str(source_memory))


def assert_stop_source_identity(
    source_memory: Path, expected_identity: tuple[int, ...] | None
) -> None:
    if expected_identity is None:
        try:
            exists = directory_exists_nofollow(
                source_memory.parent.parent, source_memory
            )
        except OSError as error:
            die_both(
                f"合并后来源 memory 路径不安全或不可读: {source_memory}: {error}",
                f"source memory path is unsafe or unreadable after merge: "
                f"{source_memory}: {error}",
            )
        if exists:
            die_both(
                f"合并后来源 memory 路径出现: {source_memory}",
                f"source memory path appeared after merge: {source_memory}",
            )
        return
    current_identity = source_memory_identity(source_memory)
    if current_identity != expected_identity:
        die_both(
            f"停止 watcher 前来源 memory 目录被替换: {source_memory}",
            f"source memory directory was replaced before stopping watcher: {source_memory}",
        )


def linux_process_group_members(process_group: int) -> set[int]:
    members: set[int] = set()
    try:
        entries = list(Path("/proc").iterdir())
    except OSError as error:
        die_both(
            f"无法核验 MemSearch watcher 进程组 {process_group}: {error}",
            f"cannot verify MemSearch watcher process group {process_group}: {error}",
        )
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            stat_data = (entry / "stat").read_text(encoding="ascii")
            fields = stat_data.rsplit(")", 1)[1].split()
            state = fields[0]
            member_group = int(fields[2])
        except FileNotFoundError:
            continue
        except (IndexError, OSError, ValueError) as error:
            die_both(
                f"无法核验进程组 {process_group} 的成员 {entry.name}: {error}",
                f"cannot verify member {entry.name} of process group {process_group}: {error}",
            )
        if member_group == process_group and not state.startswith("Z"):
            members.add(int(entry.name))
    return members


def pidfd_has_exited(pidfd: int) -> bool:
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    return bool(poller.poll(0))


def wait_for_stopped_watcher(
    pid: int, pidfd: int, expected: ProcessSnapshot
) -> ProcessSnapshot | None:
    deadline = time.monotonic() + WATCH_STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if pidfd_has_exited(pidfd):
            return None
        current = process_snapshot(pid)
        if current is None:
            return None
        if not expected.same_identity(current):
            die_both(
                f"暂停核验期间 PID {pid} 的身份发生变化",
                f"PID {pid} changed identity while being suspended",
            )
        if current.state.startswith(("T", "t")):
            return current
        time.sleep(WATCH_STOP_POLL_SECONDS)
    die_both(
        f"MemSearch watcher PID {pid} 无法暂停以完成安全核验",
        f"MemSearch watcher PID {pid} could not be suspended for safe verification",
    )


def wait_for_process_group_exit(process_group: int, pid: int) -> bool:
    deadline = time.monotonic() + WATCH_STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not linux_process_group_members(process_group):
            print(
                t(
                    f"已停止 MemSearch watcher: pid={pid}",
                    f"Stopped MemSearch watcher: pid={pid}",
                )
            )
            return True
        time.sleep(WATCH_STOP_POLL_SECONDS)
    die_both(
        f"MemSearch watcher 进程组 {process_group} 在 "
        f"{WATCH_STOP_TIMEOUT_SECONDS:g} 秒内未退出",
        f"MemSearch watcher process group {process_group} did not exit within "
        f"{WATCH_STOP_TIMEOUT_SECONDS:g} seconds",
    )


def stop_memsearch_watcher(
    source_root: str,
    dry_run: bool,
    expected_source_identity: tuple[int, ...] | None,
) -> bool:
    pid = read_watch_pid(source_root)
    if pid is None:
        return False
    source_memory = Path(source_root) / ".memsearch" / "memory"
    if not (
        sys.platform.startswith("linux")
        and Path("/proc").is_dir()
        and hasattr(os, "pidfd_open")
        and hasattr(signal, "pidfd_send_signal")
    ):
        state = portable_process_state(pid)
        if state is None or state.startswith("Z"):
            print(
                t(
                    f"MemSearch watcher 已停止: pid={pid}",
                    f"MemSearch watcher already stopped: pid={pid}",
                )
            )
            return True
        die_both(
            "当前平台缺少安全停止 watcher 所需的 pidfd; 拒绝清理 worktree",
            "this platform lacks pidfd support required to stop the watcher safely; "
            "refusing to clean the worktree",
        )
    try:
        pidfd = os.pidfd_open(pid, 0)
    except ProcessLookupError:
        print(
            t(
                f"MemSearch watcher 已停止: pid={pid}",
                f"MemSearch watcher already stopped: pid={pid}",
            )
        )
        return True
    except OSError as error:
        die_both(
            f"无法固定 MemSearch watcher PID {pid}: {error}",
            f"cannot pin MemSearch watcher PID {pid}: {error}",
        )
    try:
        return stop_memsearch_watcher_pidfd(
            source_memory, pid, pidfd, dry_run, expected_source_identity
        )
    finally:
        os.close(pidfd)


def stop_memsearch_watcher_pidfd(
    source_memory: Path,
    pid: int,
    pidfd: int,
    dry_run: bool,
    expected_source_identity: tuple[int, ...] | None,
) -> bool:
    snapshot = process_snapshot(pid)
    if snapshot is None or snapshot.state.startswith("Z"):
        print(
            t(
                f"MemSearch watcher 已停止: pid={pid}",
                f"MemSearch watcher already stopped: pid={pid}",
            )
        )
        return True
    if not watcher_matches_memory(snapshot, source_memory, pid):
        die_both(
            f"PID {pid} 不是监控来源 memory 的 MemSearch watcher; 拒绝终止进程",
            f"PID {pid} is not the MemSearch watcher for the source memory; refusing to terminate it",
        )
    assert_stop_source_identity(source_memory, expected_source_identity)
    if dry_run:
        print(
            t(
                f"将停止 MemSearch watcher: pid={pid}",
                f"Would stop MemSearch watcher: pid={pid}",
            )
        )
        return True

    process_group = snapshot.process_group
    independent_group = process_group == pid
    suspended = False
    try:
        try:
            signal.pidfd_send_signal(pidfd, signal.SIGSTOP)
        except ProcessLookupError:
            stopped_snapshot = None
        else:
            stopped_snapshot = wait_for_stopped_watcher(pid, pidfd, snapshot)
        if stopped_snapshot is None:
            if independent_group:
                die_both(
                    f"MemSearch watcher 组长 PID {pid} 在安全暂停前退出，"
                    "无法证明原进程组已经清空；拒绝信号并保留 worktree",
                    f"MemSearch watcher group leader PID {pid} exited before safe "
                    "suspension, so the original process group cannot be proven empty; "
                    "refusing to signal it and preserving the worktree",
                )
            print(
                t(
                    f"MemSearch watcher 已停止: pid={pid}",
                    f"MemSearch watcher already stopped: pid={pid}",
                )
            )
            return True
        suspended = True
        assert_stop_source_identity(source_memory, expected_source_identity)

        if independent_group:
            # 组 ID 来自 pidfd 固定的进程快照；即使组长在 SIGTERM 后消失，
            # 也必须继续等已经识别出的组全部清空，不能把 ESRCH 当整组成功。
            if linux_process_group_members(process_group):
                try:
                    os.killpg(process_group, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            if suspended:
                try:
                    signal.pidfd_send_signal(pidfd, signal.SIGCONT)
                except ProcessLookupError:
                    pass
                suspended = False
        else:
            assert stopped_snapshot is not None
            signal.pidfd_send_signal(pidfd, signal.SIGTERM)
            try:
                signal.pidfd_send_signal(pidfd, signal.SIGCONT)
            except ProcessLookupError:
                pass
            suspended = False
    except ProcessLookupError:
        # 非独立组只负责 pidfd 固定的 watcher 本身；其已退出即完成。
        suspended = False
        print(
            t(
                f"MemSearch watcher 已停止: pid={pid}",
                f"MemSearch watcher already stopped: pid={pid}",
            )
        )
        return True
    except OSError as error:
        die_both(
            f"无法停止 MemSearch watcher PID {pid}: {error}",
            f"cannot stop MemSearch watcher PID {pid}: {error}",
        )
    finally:
        if suspended:
            try:
                signal.pidfd_send_signal(pidfd, signal.SIGCONT)
            except OSError:
                pass

    if independent_group:
        return wait_for_process_group_exit(process_group, pid)

    deadline = time.monotonic() + WATCH_STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if pidfd_has_exited(pidfd):
            print(
                t(
                    f"已停止 MemSearch watcher: pid={pid}",
                    f"Stopped MemSearch watcher: pid={pid}",
                )
            )
            return True
        time.sleep(WATCH_STOP_POLL_SECONDS)
    die_both(
        f"MemSearch watcher PID {pid} 在 {WATCH_STOP_TIMEOUT_SECONDS:g} 秒内未退出",
        f"MemSearch watcher PID {pid} did not exit within {WATCH_STOP_TIMEOUT_SECONDS:g} seconds",
    )


def git_output(directory: str, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", directory, *args], capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def detect_main_tree(source_root: str) -> str | None:
    common = git_output(
        source_root, "rev-parse", "--path-format=absolute", "--git-common-dir"
    )
    if not common or os.path.basename(common) != ".git":
        return None
    return os.path.dirname(common)


def resolve_roots(args: argparse.Namespace) -> tuple[str, str]:
    source_path = os.path.abspath(args.source or os.getcwd())
    if os.name == "nt":
        try:
            validate_directory_path_nofollow(Path(source_path))
        except OSError as error:
            die_both(
                f"来源路径不安全或不可读: {source_path}: {error}",
                f"source path is unsafe or unreadable: {source_path}: {error}",
            )

    source_root = git_output(source_path, "rev-parse", "--show-toplevel")
    if source_root:
        source_root = os.path.abspath(source_root)
    elif args.target:
        source_root = source_path
    else:
        die_both(
            "未在 git worktree 内时必须提供 --target",
            "source must be inside a git worktree unless --target is provided",
        )

    if args.target:
        target_root = os.path.abspath(args.target)
    else:
        detected = detect_main_tree(source_root)
        if not detected:
            die_both(
                "无法检测主 worktree; 请传入 --target",
                "could not detect main worktree; pass --target",
            )
        target_root = os.path.abspath(detected)

    if os.name == "nt":
        for label_zh, label_en, root in (
            ("来源", "source", source_root),
            ("目标", "target", target_root),
        ):
            try:
                validate_directory_path_nofollow(Path(root))
            except OSError as error:
                die_both(
                    f"{label_zh} worktree 不安全或不可读: {root}: {error}",
                    f"{label_en} worktree is unsafe or unreadable: {root}: {error}",
                )

    return source_root, target_root


def format_entry(marker_hash: str, kind: str, source_root: str, name: str,
                 timestamp: str, block: bytes) -> bytes:
    return (
        b"\n"
        + f"<!-- merged-worktree-memory source:{source_root} file:{name}"
        f" merged-at:{timestamp} -->\n".encode()
        + block
        + b"\n"
        # 完成标记必须最后提交；旧版前置 marker 仍由 target_hashes 兼容读取.
        + f"<!-- merged-worktree-memory {kind}:{marker_hash} -->\n".encode()
    )


def append_payload_fully(handle: BinaryIO, payload: bytes, target: Path) -> None:
    """处理短写；异常时不把尚未落完正文的条目视为已提交."""
    offset = 0
    while offset < len(payload):
        written = handle.write(payload[offset:])
        if not written:
            raise OSError(f"short append to protected file: {target}")
        offset += written


def merge_files(
    source_root: str,
    target_root: str,
    source_memory: Path,
    snapshots: dict[Path, bytes],
    dry_run: bool,
    source_identity: tuple[int, ...] | None = None,
) -> None:
    target_memory = Path(target_root) / ".memsearch" / "memory"
    if not dry_run:
        if os.name == "nt":
            ensure_private_directory_nofollow(Path(target_root), target_memory)
            # 写入任何目标记忆前先完整遍历边界：拒绝 nested reparse，并迁移
            # 全部既有子目录/文件的 DACL。失败时不会先产生部分合并结果.
            scan_dirty_files(target_memory)
        else:
            target_memory.mkdir(parents=True, exist_ok=True)

    merged = skipped = empty = 0
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")

    for source_file in sorted(snapshots, key=lambda path: path.name):
        target_file = target_memory / source_file.name
        data = snapshots[source_file]

        blocks = emit_entry_blocks(data)
        # 先清非法 UTF-8 再哈希/写入, 使标记与正文一致并减少事后 rewrite.
        entries: list[tuple[str, str, bytes]] = []
        for block in blocks:
            if not block:
                continue
            cleaned = clean_bytes(block)
            entries.append((normalized_hash(cleaned), "entry", cleaned))

        if not blocks:
            fallback = emit_fallback_block(data)
            if not fallback:
                empty += 1
                continue
            cleaned = clean_bytes(fallback)
            entries = [(normalized_hash(cleaned), "file-entry", cleaned)]

        def process_entries(seen: set[str], writer) -> None:
            nonlocal merged, skipped
            for entry_hash, kind, block in entries:
                if entry_hash in seen:
                    skipped += 1
                    continue
                seen.add(entry_hash)

                if dry_run:
                    label = (
                        t("条目", "entry")
                        if kind == "entry"
                        else t("文件条目", "file entry")
                    )
                    print(
                        t(
                            f"将合并 {source_file.name} {label} {entry_hash}",
                            f"Would merge {source_file.name} {label} {entry_hash}",
                        )
                    )
                else:
                    payload = format_entry(
                        entry_hash,
                        kind,
                        source_root,
                        source_file.name,
                        timestamp,
                        block,
                    )
                    writer(payload)
                merged += 1

        if dry_run:
            process_entries(
                target_hashes(target_file, Path(target_root)),
                lambda _payload: None,
            )
        elif os.name == "nt":
            # 从去重读取直到所有 append 完成都固定同一个不共享 DELETE 的句柄.
            with open_private_append_file_nofollow(
                Path(target_root), target_file
            ) as handle:
                handle.seek(0)
                seen = hashes_from_target_data(handle.read())

                def append_payload(payload: bytes) -> None:
                    append_payload_fully(handle, payload, target_file)

                process_entries(seen, append_payload)
        else:
            seen = target_hashes(target_file)

            def append_payload(payload: bytes) -> None:
                with open(target_file, "ab") as handle:
                    append_payload_fully(handle, payload, target_file)

            process_entries(seen, append_payload)

    print(
        t(
            f"记忆合并完成: merged={merged} skipped={skipped} empty_files={empty}",
            f"Memory merge complete: merged={merged} skipped={skipped} empty_files={empty}",
        )
    )
    print(t(f"来源: {source_root}", f"Source: {source_root}"))
    print(t(f"目标: {target_root}", f"Target: {target_root}"))

    # 新条目写入前已清理. 不对目标整文件 rewrite, 避免与并发 append 竞态丢记录.
    if dry_run:
        print(
            t(
                f"将扫描 {target_memory} 中的非法 UTF-8, 不改写线上文件",
                f"Would scan invalid UTF-8 in {target_memory} without rewriting live files",
            )
        )
    elif os.name == "nt" or target_memory.is_dir():
        scanned, dirty = scan_dirty_files(target_memory)
        print(
            t(
                f"已扫描 {scanned} 个 markdown 文件, "
                f"保留 {dirty} 个脏文件未改写以避免并发丢失",
                f"scanned {scanned} markdown file(s), "
                f"left {dirty} dirty file(s) unchanged to avoid concurrent loss",
            )
        )

    if not dry_run:
        assert_source_unchanged(source_memory, snapshots, source_identity)


def merge(source_root: str, target_root: str, dry_run: bool) -> MergeResult:
    source_memory = Path(source_root) / ".memsearch" / "memory"

    # git 返回物理路径而 --target 可能是逻辑路径, 同一目录的两种写法要判等.
    if os.name == "nt":
        try:
            source_root_identity = validate_directory_path_nofollow(Path(source_root))
            target_root_identity = validate_directory_path_nofollow(Path(target_root))
        except OSError as error:
            die_both(
                f"worktree 根目录不安全或不可读: {error}",
                f"worktree root is unsafe or unreadable: {error}",
            )
        roots_are_equal = source_root_identity == target_root_identity
    else:
        roots_are_equal = os.path.realpath(source_root) == os.path.realpath(target_root)
    if roots_are_equal:
        print(t("来源即主 worktree; 无需合并 memory.", "Source is the main worktree; no memory merge needed."))
        return MergeResult(False, None)

    # 源 worktree 没有 .memsearch/memory 时是正常空操作: 常见于未装 memsearch,
    # 或已装但本 worktree 尚未产生记忆. 不创建任何目录, 以 0 退出.
    try:
        source_memory_exists = directory_exists_nofollow(
            Path(source_root), source_memory
        )
    except OSError as error:
        die_both(
            f"来源 memory 路径不安全或不可读: {source_memory}: {error}",
            f"source memory path is unsafe or unreadable: {source_memory}: {error}",
        )
    if not source_memory_exists:
        print(
            t(
                f"无需合并: {source_memory} 不存在",
                f"Nothing to merge: {source_memory} does not exist",
            )
        )
        return MergeResult(True, None)

    source_identity = source_memory_identity(source_memory)
    # 目录已存在时即使当前为空也要做稳定快照: 避免 Stop hook 稍后才创建首个文件
    # 时被误判为空操作成功, 随后 worktree 被清理导致首条记录丢失.
    snapshots = read_stable_source_files(source_memory)
    if not snapshots:
        # 再次确认空目录稳定后, 仍做一次合并后复核.
        if dry_run:
            print(
                t(
                    f"无需合并: {source_memory} 中没有 memory 文件",
                    f"Nothing to merge: no memory files in {source_memory}",
                )
            )
            return MergeResult(True, source_identity)
        assert_source_unchanged(source_memory, snapshots, source_identity)
        print(
            t(
                f"无需合并: {source_memory} 中没有 memory 文件",
                f"Nothing to merge: no memory files in {source_memory}",
            )
        )
        return MergeResult(True, source_identity)

    if dry_run:
        merge_files(
            source_root, target_root, source_memory, snapshots, True, source_identity
        )
        return MergeResult(True, source_identity)

    state_dir = Path(target_root) / ".memsearch"
    if os.name == "nt":
        ensure_private_directory_nofollow(Path(target_root), state_dir)
    else:
        state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / ".merge-worktree-memory.lock"
    lock_context = (
        open_private_append_file_nofollow(Path(target_root), lock_path)
        if os.name == "nt"
        else lock_path.open("a+b")
    )
    with lock_context as lock:
        with exclusive_file_lock(lock):
            # 持锁后再采一次稳定快照, 避免等待锁期间 Stop hook 已写入新条目或新文件.
            source_identity = source_memory_identity(source_memory)
            snapshots = read_stable_source_files(source_memory)
            if not snapshots:
                assert_source_unchanged(source_memory, snapshots, source_identity)
                print(
                    t(
                        f"无需合并: {source_memory} 中没有 memory 文件",
                        f"Nothing to merge: no memory files in {source_memory}",
                    )
                )
                return MergeResult(True, source_identity)
            merge_files(
                source_root, target_root, source_memory, snapshots, False, source_identity
            )
    return MergeResult(True, source_identity)


def main() -> int:
    apply_language_argument(sys.argv[1:])
    bind_effective_language()
    import argparse

    argparse._ = lambda message: t(ARGPARSE_ZH.get(message, message), message)
    parser = LocalizedArgumentParser(
        description=t(
            "把任务 worktree 的 .memsearch/memory 条目并入主 worktree. "
            "新条目写入前清理非法 UTF-8; 目标既有文件只扫描, 在仍可能被追加时不改写. "
            "Linux 成功后用 pidfd 停止经核验的来源 MemSearch watcher.",
            "Merge .memsearch/memory entries from a task worktree into the "
            "main worktree. New entries are UTF-8 cleaned on write; existing target "
            "files are scanned but not rewritten while they may still receive appends. "
            "On Linux, pidfd is used to stop a verified source MemSearch watcher "
            "after success.",
        )
    )
    parser.add_argument("--lang", choices=LANGUAGES, help=t("覆盖输出语言", "override the output language"))
    parser.add_argument(
        "--source", metavar="PATH",
        help=t("来源 worktree. 默认为当前目录.", "Worktree to merge from. Defaults to the current directory."),
    )
    parser.add_argument(
        "--target", metavar="PATH",
        help=t("并入的主 worktree. 默认为 git common-dir 父目录.", "Main worktree to merge into. Defaults to the git common-dir parent."),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help=t("只打印将合并的内容, 不写文件.", "Print what would be merged without writing files."),
    )
    args = parser.parse_args()

    source_root, target_root = resolve_roots(args)
    try:
        merge_result = merge(source_root, target_root, args.dry_run)
        if merge_result.stop_watcher and os.name != "nt":
            watcher_checked = stop_memsearch_watcher(
                source_root, args.dry_run, merge_result.source_identity
            )
            # watcher 停止期间 Stop hook 仍可能完成最后一次追加。停止后重新走
            # 同一套稳定快照、去重合并和最终复核，再允许调用方删除 worktree。
            if watcher_checked and not args.dry_run:
                merge(source_root, target_root, False)
    except OSError as error:
        die_both(
            f"安全文件操作失败: {error}",
            f"secure file operation failed: {error}",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
