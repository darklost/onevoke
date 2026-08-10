#!/usr/bin/env python3

"""把任务 worktree 的 memsearch 记忆并入主 worktree.

不依赖 memsearch 本身: 只读写 `.memsearch/memory/*.md`, 从不调用它的二进制.
未安装 memsearch 时 worktree 里没有该目录, 本脚本报告无事可做并以 0 退出,
不创建任何目录, 集成流程可以无条件调用它.

全程按字节处理. 记忆文件由 hook 自动追加, 历史数据可能含非法 UTF-8 序列;
先解码再处理会丢字节或直接抛错, 因此切分, 归一化和哈希都在 bytes 上完成.
新合并条目在写入前丢弃非法 UTF-8; 不对可能仍被追加的目标整文件 rewrite
(无 MemSearch 协作封口协议时, 改写无法证明不丢并发字节). 既有脏文件只扫描报告.

条目哈希与既有记忆文件中的 `<!-- merged-worktree-memory entry:... -->` 标记
兼容, 改动切分或归一化逻辑会让已合并条目重新判定为新条目.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


# 与 C locale 下 awk 的 [[:space:]] 对齐; 记录内不含换行.
BLANK = re.compile(rb"^[ \t\v\f\r]*$")
ENTRY_HEADER = re.compile(rb"^### [0-9][0-9]:[0-9][0-9][ \t\v\f\r]*$")
SESSION_HEADER = re.compile(rb"^## Session ")
ENTRY_MARKER = re.compile(
    rb"^<!-- merged-worktree-memory (?:entry|file-entry):([0-9a-f]+) -->$"
)
SOURCE_MARKER = re.compile(rb"^<!-- merged-worktree-memory source:.* -->$")
# Stop hook 可能在合并窗口内继续追加; 两次读取一致才视为来源稳定.
# 注意: 进程成功返回之后的写入需要 MemSearch Stop hook 与清理流程的协作封口
# 协议才能绝对消除, 本脚本只能 fail-closed 本进程可观测窗口; 见任务范围.
SOURCE_STABLE_ATTEMPTS = 5
SOURCE_STABLE_DELAY_SECONDS = 0.1


def die(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


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


def target_hashes(path: Path) -> set[str]:
    """目标文件中已存在的条目哈希: 合并标记, 重新切分的条目, 以及回退整块."""
    if not path.is_file():
        return set()

    data = path.read_bytes()
    hashes = {
        match.group(1).decode()
        for match in (ENTRY_MARKER.match(line) for line in split_lines(data))
        if match
    }
    hashes.update(normalized_hash(block) for block in emit_entry_blocks(data))

    fallback = emit_fallback_block(data)
    if fallback:
        hashes.add(normalized_hash(fallback))
    return hashes


def clean_bytes(data: bytes) -> bytes:
    """丢弃非法 UTF-8 序列; 已有的合法 U+FFFD 保留."""
    return data.decode("utf-8", errors="ignore").encode("utf-8")


def scan_dirty_files(directory: Path) -> tuple[int, int]:
    """扫描非法 UTF-8, 但不改写已有文件.

    新合并条目在写入前已清理. 对可能被 Stop hook 并发追加的目标文件做
    truncate/replace 无法在无协作协议下证明不丢字节, 因此这里 fail-closed:
    只报告脏文件数量, 留给空闲时的运维清理, 合并本身不覆盖活跃目标.
    """
    scanned = 0
    dirty = 0
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


def list_source_memory_files(source_memory: Path) -> list[Path]:
    return sorted(
        path
        for path in source_memory.glob("*.md")
        if path.is_file() and not path.is_symlink()
    )


def read_stable_source_files(source_memory: Path) -> dict[Path, bytes]:
    """目录成员与文件内容均须连续两次一致; 新增/删除/改名/改写都视为不稳定."""
    last_error = "source memory files were unstable"
    for attempt in range(1, SOURCE_STABLE_ATTEMPTS + 1):
        try:
            names_first = [path.name for path in list_source_memory_files(source_memory)]
            first: dict[Path, bytes] = {}
            for path in list_source_memory_files(source_memory):
                first[path] = path.read_bytes()
            if [path.name for path in first] != names_first:
                last_error = (
                    f"source memory directory changed while listing "
                    f"(attempt {attempt}/{SOURCE_STABLE_ATTEMPTS})"
                )
                time.sleep(SOURCE_STABLE_DELAY_SECONDS)
                continue
            time.sleep(SOURCE_STABLE_DELAY_SECONDS)
            names_second = [path.name for path in list_source_memory_files(source_memory)]
            if names_second != names_first:
                last_error = (
                    f"source memory directory membership changed while reading "
                    f"(attempt {attempt}/{SOURCE_STABLE_ATTEMPTS}); "
                    "Stop hook may still be writing"
                )
                continue
            stable = True
            for path, data in first.items():
                if not path.is_file():
                    die(f"source memory file disappeared: {path}")
                if path.read_bytes() != data:
                    stable = False
                    last_error = (
                        f"source memory changed while reading: {path.name} "
                        f"(attempt {attempt}/{SOURCE_STABLE_ATTEMPTS}); "
                        "Stop hook may still be writing"
                    )
                    break
            if stable:
                return first
        except OSError as error:
            last_error = f"failed to read source memory: {error}"
    die(last_error)


def assert_source_unchanged(source_memory: Path, snapshots: dict[Path, bytes]) -> None:
    """合并后再核对目录成员与内容; 任一变化都失败, 阻止清理 worktree."""
    try:
        current_names = {path.name for path in list_source_memory_files(source_memory)}
    except OSError as error:
        die(f"source memory unreadable after merge: {error}")
    expected_names = {path.name for path in snapshots}
    if current_names != expected_names:
        die(
            "source memory directory membership changed after merge; "
            "refusing success so the worktree is not cleaned"
        )
    for path, expected in snapshots.items():
        try:
            current = path.read_bytes()
        except OSError as error:
            die(f"source memory unreadable after merge: {path}: {error}")
        if current != expected:
            die(
                f"source memory changed after merge: {path.name}; "
                "refusing success so the worktree is not cleaned"
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

    source_root = git_output(source_path, "rev-parse", "--show-toplevel")
    if source_root:
        source_root = os.path.abspath(source_root)
    elif args.target:
        source_root = source_path
    else:
        die("source must be inside a git worktree unless --target is provided")

    if args.target:
        target_root = os.path.abspath(args.target)
    else:
        detected = detect_main_tree(source_root)
        if not detected:
            die("could not detect main worktree; pass --target")
        target_root = os.path.abspath(detected)

    return source_root, target_root


def format_entry(marker_hash: str, kind: str, source_root: str, name: str,
                 timestamp: str, block: bytes) -> bytes:
    return (
        b"\n"
        + f"<!-- merged-worktree-memory {kind}:{marker_hash} -->\n".encode()
        + f"<!-- merged-worktree-memory source:{source_root} file:{name}"
        f" merged-at:{timestamp} -->\n".encode()
        + block
        + b"\n"
    )


def merge_files(
    source_root: str,
    target_root: str,
    source_memory: Path,
    snapshots: dict[Path, bytes],
    dry_run: bool,
) -> None:
    target_memory = Path(target_root) / ".memsearch" / "memory"
    if not dry_run:
        target_memory.mkdir(parents=True, exist_ok=True)

    merged = skipped = empty = 0
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")

    for source_file in sorted(snapshots, key=lambda path: path.name):
        target_file = target_memory / source_file.name
        seen = target_hashes(target_file)
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

        for entry_hash, kind, block in entries:
            if entry_hash in seen:
                skipped += 1
                continue
            seen.add(entry_hash)

            if dry_run:
                label = "entry" if kind == "entry" else "file entry"
                print(f"Would merge {source_file.name} {label} {entry_hash}")
            else:
                with open(target_file, "ab") as handle:
                    handle.write(
                        format_entry(
                            entry_hash, kind, source_root, source_file.name,
                            timestamp, block,
                        )
                    )
            merged += 1

    print(f"Memory merge complete: merged={merged} skipped={skipped} empty_files={empty}")
    print(f"Source: {source_root}")
    print(f"Target: {target_root}")

    # 新条目写入前已清理. 不对目标整文件 rewrite, 避免与并发 append 竞态丢记录.
    if dry_run:
        print(f"Would scan invalid UTF-8 in {target_memory} without rewriting live files")
    elif target_memory.is_dir():
        scanned, dirty = scan_dirty_files(target_memory)
        print(
            f"scanned {scanned} markdown file(s), "
            f"left {dirty} dirty file(s) unchanged to avoid concurrent loss"
        )

    if not dry_run:
        assert_source_unchanged(source_memory, snapshots)


def merge(source_root: str, target_root: str, dry_run: bool) -> None:
    source_memory = Path(source_root) / ".memsearch" / "memory"

    # git 返回物理路径而 --target 可能是逻辑路径, 同一目录的两种写法要判等.
    if os.path.realpath(source_root) == os.path.realpath(target_root):
        print("Source is the main worktree; no memory merge needed.")
        return

    # 源 worktree 没有 .memsearch/memory 时是正常空操作: 常见于未装 memsearch,
    # 或已装但本 worktree 尚未产生记忆. 不创建任何目录, 以 0 退出.
    if not source_memory.is_dir():
        print(f"Nothing to merge: {source_memory} does not exist")
        return

    # 目录已存在时即使当前为空也要做稳定快照: 避免 Stop hook 稍后才创建首个文件
    # 时被误判为空操作成功, 随后 worktree 被清理导致首条记录丢失.
    snapshots = read_stable_source_files(source_memory)
    if not snapshots:
        # 再次确认空目录稳定后, 仍做一次合并后复核.
        if dry_run:
            print(f"Nothing to merge: no memory files in {source_memory}")
            return
        assert_source_unchanged(source_memory, snapshots)
        print(f"Nothing to merge: no memory files in {source_memory}")
        return

    if dry_run:
        merge_files(source_root, target_root, source_memory, snapshots, True)
        return

    state_dir = Path(target_root) / ".memsearch"
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / ".merge-worktree-memory.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # 持锁后再采一次稳定快照, 避免等待锁期间 Stop hook 已写入新条目或新文件.
        snapshots = read_stable_source_files(source_memory)
        if not snapshots:
            assert_source_unchanged(source_memory, snapshots)
            print(f"Nothing to merge: no memory files in {source_memory}")
            return
        merge_files(source_root, target_root, source_memory, snapshots, False)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Merge .memsearch/memory entries from a task worktree into the "
        "main worktree. New entries are UTF-8 cleaned on write; existing target "
        "files are scanned but not rewritten while they may still receive appends."
    )
    parser.add_argument(
        "--source", metavar="PATH",
        help="Worktree to merge from. Defaults to the current directory.",
    )
    parser.add_argument(
        "--target", metavar="PATH",
        help="Main worktree to merge into. Defaults to the git common-dir parent.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be merged without writing files.",
    )
    args = parser.parse_args()

    source_root, target_root = resolve_roots(args)
    merge(source_root, target_root, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
