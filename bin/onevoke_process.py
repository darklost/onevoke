#!/usr/bin/env python3

"""Agent 程序发现、任务载荷与跨平台进程调用边界."""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Iterable, Mapping


WINDOWS_BATCH_SUFFIXES = {".cmd", ".bat"}


def _is_windows() -> bool:
    return os.name == "nt"


@dataclass(frozen=True)
class AgentProgram:
    path: str
    batch: bool = False


@dataclass(frozen=True)
class ProcessInvocation:
    argv: tuple[str, ...]
    environment: dict[str, str]


def _usable_file(path: Path, *, nonempty: bool = False) -> bool:
    try:
        return path.is_file() and (not nonempty or path.stat().st_size > 0)
    except OSError:
        return False


def _explicit_program(name: str) -> AgentProgram | None:
    path = Path(name)
    suffix = path.suffix.lower()
    if suffix not in {".exe", *WINDOWS_BATCH_SUFFIXES}:
        return None
    if not _usable_file(path, nonempty=suffix == ".exe"):
        return None
    return AgentProgram(str(path), suffix in WINDOWS_BATCH_SUFFIXES)


def _windows_path_candidates(name: str, suffix: str) -> Iterable[Path]:
    filename = name if name.lower().endswith(suffix) else f"{name}{suffix}"
    separator = ";" if _is_windows() else os.pathsep
    for raw_directory in os.environ.get("PATH", "").split(separator):
        directory = raw_directory.strip().strip('"')
        if directory:
            yield Path(directory) / filename


def resolve_agent_program(name: str) -> AgentProgram | None:
    """解析当前四种 Agent 的进程入口; Windows 优先原生 exe, 再接受 batch."""
    if not _is_windows():
        found = shutil.which(name)
        return AgentProgram(found) if found else None

    explicit = _explicit_program(name)
    if explicit is not None:
        return explicit
    if Path(name).suffix:
        return None

    # 显式遍历使 0 字节 Windows App Execution Alias 不会遮住 PATH 后续入口.
    for candidate in _windows_path_candidates(name, ".exe"):
        if _usable_file(candidate, nonempty=True):
            return AgentProgram(str(candidate))
    for suffix in (".cmd", ".bat"):
        for candidate in _windows_path_candidates(name, suffix):
            if _usable_file(candidate):
                return AgentProgram(str(candidate), batch=True)
    return None


def _quote_windows_batch_argument(argument: str) -> str:
    """编码一个经 batch ``%*`` 转发给原生程序的参数片段."""
    if "\0" in argument:
        raise ValueError("Windows process arguments cannot contain NUL")
    reverse = ['"']
    quote_hit = True
    for character in reversed(argument):
        reverse.append(character)
        if quote_hit and character == "\\":
            reverse.append("\\")
        elif character == '"':
            quote_hit = True
            reverse.append('"')
        else:
            quote_hit = False
    reverse.append('"')
    return "".join(reversed(reverse))


def _windows_command_interpreter(environment: Mapping[str, str]) -> str:
    comspec = environment.get("COMSPEC", "")
    comspec_path = PureWindowsPath(comspec)
    if (
        comspec
        and comspec_path.is_absolute()
        and comspec_path.name.lower() == "cmd.exe"
    ):
        return comspec
    system_root = environment.get("SystemRoot", "")
    system_root_path = PureWindowsPath(system_root)
    if system_root and system_root_path.is_absolute():
        return str(system_root_path / "System32" / "cmd.exe")
    raise FileNotFoundError("could not resolve an absolute Windows cmd.exe path")


def process_invocation(
    program: AgentProgram,
    arguments: Iterable[str],
    environment: Mapping[str, str] | None = None,
) -> ProcessInvocation:
    """构造进程调用; batch 的 ``/c`` 文本只含 Onevoke 生成的变量引用."""
    child_environment = dict(os.environ if environment is None else environment)
    values = [program.path, *arguments]
    if not _is_windows() or not program.batch:
        return ProcessInvocation(tuple(values), child_environment)

    namespace = f"ONEVOKE_CMD_{uuid.uuid4().hex.upper()}"
    references: list[str] = []
    for index, value in enumerate(values):
        variable = f"{namespace}_{index}"
        child_environment[variable] = _quote_windows_batch_argument(value)
        references.append(f"%{variable}%")
    command = " ".join(references)
    return ProcessInvocation(
        (
            _windows_command_interpreter(child_environment),
            "/d",
            "/s",
            "/v:off",
            "/c",
            command,
        ),
        child_environment,
    )


def task_payload(body: str, path: Path) -> str:
    return (
        body.rstrip()
        + "\n\n"
        + f"完成本次任务后尝试删除任务文件: {path}\n"
        + "删除失败或文件遗留不影响任务结果.\n"
    )


def create_task_file(body: str, *, prefix: str = "onevoke-task-") -> Path:
    """用系统临时文件机制写入任务载荷, 不附加权限或 ACL 检查."""
    handle, raw_path = tempfile.mkstemp(prefix=prefix, suffix=".md")
    path = Path(raw_path).absolute()
    with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(task_payload(body, path))
    return path


def write_task_file(path: Path, body: str) -> None:
    """写入已有 runtime 中的任务载荷, 不附加权限或 ACL 检查."""
    path.write_text(task_payload(body, path), encoding="utf-8")


def task_file_instruction(prefix: str, path: Path) -> str:
    return (
        f"{prefix.rstrip(' .')}; full instructions are in the UTF-8 task file at {path}; "
        "read the complete file first and follow it exactly."
    )
