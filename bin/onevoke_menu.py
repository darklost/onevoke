#!/usr/bin/env python3
"""onevoke welcome/config 用的轻量方向键选择菜单 (termios + ANSI)."""

from __future__ import annotations

import os
import sys
import unicodedata
from typing import Any, Callable

try:
    import termios
    import tty
except ImportError:  # pragma: no cover - 非 POSIX
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]


class MenuCancelled(Exception):
    """用户取消菜单."""


class MenuEnded(Exception):
    """输入结束, 菜单无法完成."""


# 菜单之间暂存提前输入; 单个菜单内部只用 local queue, 避免同屏重复读取.
_PENDING: list[bytes] = []


def curses_menu_available() -> bool:
    """stdin/stderr 均为 tty 且 termios 可用时启用方向键菜单."""
    if termios is None or tty is None or os.name == "nt":
        return False
    try:
        return sys.stdin.isatty() and sys.stderr.isatty()
    except Exception:
        return False


def _write(text: str) -> None:
    sys.stderr.write(text)
    sys.stderr.flush()


def _hide_cursor() -> None:
    _write("\033[?25l")


def _show_cursor() -> None:
    _write("\033[?25h")


def _move_up(lines: int) -> None:
    if lines > 0:
        _write(f"\033[{lines}A")


def _clear_line() -> None:
    _write("\033[2K\r")


def _read_byte_from(fd: int, queue: list[bytes]) -> bytes:
    if queue:
        return queue.pop(0)
    return os.read(fd, 1)


def _defer_byte(queue: list[bytes], data: bytes) -> None:
    if data:
        queue.append(data)


def _read_key(fd: int, queue: list[bytes]) -> str:
    ch = _read_byte_from(fd, queue)
    if not ch:
        raise MenuEnded("eof")
    if ch == b"\x1b":
        rest = b""
        os.set_blocking(fd, False)
        try:
            while True:
                try:
                    chunk = _read_byte_from(fd, queue)
                except BlockingIOError:
                    break
                if not chunk:
                    break
                rest += chunk
                if len(rest) >= 2:
                    break
        finally:
            os.set_blocking(fd, True)
        if rest.startswith(b"[A") or rest == b"OA":
            return "up"
        if rest.startswith(b"[B") or rest == b"OB":
            return "down"
        if rest:
            for offset in range(len(rest) - 1, -1, -1):
                _defer_byte(queue, rest[offset : offset + 1])
        return "esc"
    if ch == b"\r":
        os.set_blocking(fd, False)
        try:
            try:
                nxt = _read_byte_from(fd, queue)
            except BlockingIOError:
                nxt = b""
            if nxt and nxt != b"\n":
                _defer_byte(queue, nxt)
        finally:
            os.set_blocking(fd, True)
        return "enter"
    if ch == b"\n":
        return "enter"
    try:
        return ch.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _digit_target(buffer: str, count: int) -> int | None:
    if not buffer:
        return None
    try:
        number = int(buffer)
    except ValueError:
        return None
    if 1 <= number <= count:
        return number - 1
    return None


def _digit_prefix_possible(buffer: str, count: int) -> bool:
    return any(str(index).startswith(buffer) for index in range(1, count + 1))


def _render(
    prompt: str,
    labels: list[str],
    index: int,
    footer: str,
    *,
    painted_lines: int,
) -> int:
    lines = list(prompt.splitlines() or [prompt])
    lines.append("")
    for item_index, label in enumerate(labels):
        marker = ">" if item_index == index else " "
        prefix = f" {marker} "
        if item_index == index:
            lines.append(f"\033[7m{prefix}{label}\033[0m")
        else:
            lines.append(f"{prefix}{label}")
    lines.append("")
    lines.append(f"\033[2m{footer}\033[0m")

    if painted_lines:
        _move_up(painted_lines)
    for line in lines:
        _clear_line()
        _write(line + "\n")
    return len(lines)


def select_index(
    prompt: str,
    labels: list[str],
    *,
    default_index: int = 0,
    footer: str,
    allow_cancel: bool = False,
    on_key: Callable[[str], int | None] | None = None,
) -> int:
    """方向键选择一项, 返回索引. allow_cancel 时 q/Esc 抛 MenuCancelled."""
    if not labels:
        raise ValueError("labels must not be empty")
    if not curses_menu_available():
        raise RuntimeError("interactive menu is unavailable")

    global _PENDING
    queue = list(_PENDING)
    _PENDING = []

    index = max(0, min(default_index, len(labels) - 1))
    digit_buffer = ""
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    painted = 0
    try:
        tty.setcbreak(fd)
        _hide_cursor()
        _write("\n")
        while True:
            painted = _render(prompt, labels, index, footer, painted_lines=painted)
            key = _read_key(fd, queue)
            if on_key is not None and len(key) == 1:
                mapped = on_key(key)
                if mapped is not None:
                    index = mapped
                    break
            if key == "enter":
                break
            if allow_cancel and key in ("q", "Q", "esc"):
                raise MenuCancelled()
            if key in ("up", "k", "K"):
                index = (index - 1) % len(labels)
                digit_buffer = ""
                continue
            if key in ("down", "j", "J"):
                index = (index + 1) % len(labels)
                digit_buffer = ""
                continue
            if key.isdigit():
                candidate = digit_buffer + key
                target = _digit_target(candidate, len(labels))
                if target is not None:
                    digit_buffer = candidate
                    index = target
                    continue
                if _digit_prefix_possible(candidate, len(labels)):
                    digit_buffer = candidate
                    continue
                single = _digit_target(key, len(labels))
                digit_buffer = key if single is not None else ""
                if single is not None:
                    index = single
                continue
            if len(key) == 1 and key.isprintable():
                _defer_byte(queue, key.encode("utf-8"))
                continue
            continue
    finally:
        _show_cursor()
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        _PENDING = queue
    return index
