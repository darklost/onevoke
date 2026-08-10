#!/usr/bin/env python3

"""把任务 worktree 的 memsearch 记忆并入主 worktree, 并清除非法 UTF-8 字节.

不依赖 memsearch 本身: 只读写 `.memsearch/memory/*.md`, 从不调用它的二进制.
未安装 memsearch 时 worktree 里没有该目录, 本脚本报告无事可做并以 0 退出,
不创建任何目录, 集成流程可以无条件调用它.

全程按字节处理. 记忆文件由 hook 自动追加, 历史数据可能含非法 UTF-8 序列;
先解码再处理会丢字节或直接抛错, 因此切分, 归一化和哈希都在 bytes 上完成,
只有最后的清理步骤按 UTF-8 语义丢弃非法序列.

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
import tempfile
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


def atomic_replace(path: Path, data: bytes) -> None:
    mode = path.stat().st_mode
    handle, temp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "wb") as temp_file:
            temp_file.write(data)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise

    if os.name == "posix":
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)


def clean_directory(directory: Path) -> tuple[int, int]:
    """递归清理目录下的 *.md. 不跟随符号链接, 内容未变则不重写."""
    scanned = 0
    changed = 0
    for entry in os.scandir(directory):
        path = Path(entry.path)
        if entry.is_dir(follow_symlinks=False):
            sub_scanned, sub_changed = clean_directory(path)
            scanned += sub_scanned
            changed += sub_changed
        elif entry.is_file(follow_symlinks=False) and path.suffix.lower() == ".md":
            scanned += 1
            original = path.read_bytes()
            cleaned = clean_bytes(original)
            if cleaned != original:
                atomic_replace(path, cleaned)
                changed += 1
    return scanned, changed


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
    source_files: list[Path],
    dry_run: bool,
) -> None:
    target_memory = Path(target_root) / ".memsearch" / "memory"
    if not dry_run:
        target_memory.mkdir(parents=True, exist_ok=True)

    merged = skipped = empty = 0
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")

    for source_file in source_files:
        target_file = target_memory / source_file.name
        seen = target_hashes(target_file)
        data = source_file.read_bytes()

        blocks = emit_entry_blocks(data)
        entries: list[tuple[str, str, bytes]] = [
            (normalized_hash(block), "entry", block) for block in blocks if block
        ]

        if not blocks:
            fallback = emit_fallback_block(data)
            if not fallback:
                empty += 1
                continue
            entries = [(normalized_hash(fallback), "file-entry", fallback)]

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

    # 合并只做字节透传, 脏字节会从 worktree 记忆一路带进主树, 在这里收掉.
    if dry_run:
        print(f"Would clean invalid UTF-8 in {target_memory}")
    elif target_memory.is_dir():
        scanned, changed = clean_directory(target_memory)
        print(f"scanned {scanned} markdown file(s), cleaned {changed} file(s)")


def merge(source_root: str, target_root: str, dry_run: bool) -> None:
    source_memory = Path(source_root) / ".memsearch" / "memory"

    # git 返回物理路径而 --target 可能是逻辑路径, 同一目录的两种写法要判等.
    if os.path.realpath(source_root) == os.path.realpath(target_root):
        print("Source is the main worktree; no memory merge needed.")
        return

    # 未安装 memsearch 时 worktree 里根本没有这个目录: 属于正常情况, 空操作返回.
    if not source_memory.is_dir():
        print(f"Nothing to merge: {source_memory} does not exist")
        return

    source_files = sorted(source_memory.glob("*.md"))
    if not source_files:
        print(f"Nothing to merge: no memory files in {source_memory}")
        return

    if dry_run:
        merge_files(source_root, target_root, source_files, True)
        return

    state_dir = Path(target_root) / ".memsearch"
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / ".merge-worktree-memory.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        merge_files(source_root, target_root, source_files, False)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Merge .memsearch/memory entries from a task worktree into the "
        "main worktree, then strip invalid UTF-8 from the result."
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
