#!/usr/bin/env python3

"""Onevoke 的跨平台底层文件系统原语.

看板中的任务路径属于安全边界. POSIX 使用 ``openat`` 和
``O_NOFOLLOW`` 逐级打开; Windows 使用 ``CreateFileW`` 的
``FILE_FLAG_OPEN_REPARSE_POINT`` 逐级打开, 并让每个已验证目录句柄都不
共享 ``DELETE``. 后者会在继续解析子路径期间阻止目录被改名、删除或换成
symlink/junction, 因而不把安全性退化为一次性的 ``Path.is_symlink`` 检查.
"""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import BinaryIO, Iterator


class UnsafePathError(OSError):
    """路径越界、含 reparse point，或最终对象类型不符合契约."""


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _relative_parts(root: Path, path: Path) -> tuple[Path, Path, tuple[str, ...]]:
    """返回不解析链接的绝对路径和相对分量，并做词法边界校验."""
    root_absolute = _absolute_path(root)
    candidate = _absolute_path(path if path.is_absolute() else root_absolute / path)
    try:
        common = os.path.commonpath((os.fspath(root_absolute), os.fspath(candidate)))
    except ValueError as error:
        raise UnsafePathError(f"path escapes protected root: {candidate}") from error
    if os.path.normcase(common) != os.path.normcase(os.fspath(root_absolute)):
        raise UnsafePathError(f"path escapes protected root: {candidate}")
    relative = os.path.relpath(os.fspath(candidate), os.fspath(root_absolute))
    parts = () if relative in ("", ".") else Path(relative).parts
    if any(part in ("", ".", "..") for part in parts):
        raise UnsafePathError(f"invalid protected path: {candidate}")
    return root_absolute, candidate, tuple(parts)


def is_reparse_point(path: Path) -> bool:
    """不跟随路径，判断它是否为 POSIX symlink 或 Windows reparse point."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & 0x00000400)  # FILE_ATTRIBUTE_REPARSE_POINT


if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _DELETE = 0x00010000
    _READ_CONTROL = 0x00020000
    _WRITE_DAC = 0x00040000
    _FILE_READ_ATTRIBUTES = 0x00000080
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _FILE_SHARE_DELETE = 0x00000004
    _CREATE_NEW = 1
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_NORMAL = 0x00000080
    _FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _ERROR_FILE_NOT_FOUND = 2
    _ERROR_PATH_NOT_FOUND = 3
    _ERROR_FILE_EXISTS = 80
    _ERROR_ALREADY_EXISTS = 183
    _FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
    _FILE_RENAME_INFO_CLASS = 3
    _FILE_DISPOSITION_INFO_CLASS = 4
    _LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
    _TOKEN_QUERY = 0x00000008
    _TOKEN_USER_CLASS = 1
    _SDDL_REVISION_1 = 1
    _SE_FILE_OBJECT = 1
    _DACL_SECURITY_INFORMATION = 0x00000004
    _PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000

    class _FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
        _fields_ = [
            ("FileAttributes", wintypes.DWORD),
            ("ReparseTag", wintypes.DWORD),
        ]

    class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    class _FILE_DISPOSITION_INFO(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOL)]

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    class _SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("Sid", ctypes.c_void_p),
            ("Attributes", wintypes.DWORD),
        ]

    class _TOKEN_USER(ctypes.Structure):
        _fields_ = [("User", _SID_AND_ATTRIBUTES)]

    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
    _kernel32.CreateDirectoryW.restype = wintypes.BOOL
    _kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    _kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION),
    ]
    _kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    _kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    _kernel32.ReadFile.restype = wintypes.BOOL
    _kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    _kernel32.WriteFile.restype = wintypes.BOOL
    _kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    _kernel32.FlushFileBuffers.restype = wintypes.BOOL
    _kernel32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    _kernel32.LockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_OVERLAPPED),
    ]
    _kernel32.LockFileEx.restype = wintypes.BOOL
    _kernel32.UnlockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_OVERLAPPED),
    ]
    _kernel32.UnlockFileEx.restype = wintypes.BOOL
    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p

    _advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL
    _advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL
    _advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    _advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    _advapi32.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    _advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    _advapi32.SetSecurityInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    _advapi32.SetSecurityInfo.restype = wintypes.DWORD


def _raise_windows_error(action: str, path: Path | None = None) -> None:
    code = ctypes.get_last_error()  # type: ignore[name-defined]
    detail = ctypes.FormatError(code).strip()  # type: ignore[name-defined]
    target = f": {path}" if path is not None else ""
    raise OSError(code, f"{action}{target}: {detail}", os.fspath(path) if path else None)


if os.name == "nt":
    def _close_handle(handle: int) -> None:
        if handle not in (None, _INVALID_HANDLE_VALUE):
            _kernel32.CloseHandle(handle)


    def _attribute_info(handle: int, path: Path) -> _FILE_ATTRIBUTE_TAG_INFO:
        info = _FILE_ATTRIBUTE_TAG_INFO()
        if not _kernel32.GetFileInformationByHandleEx(
            handle,
            _FILE_ATTRIBUTE_TAG_INFO_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            _raise_windows_error("cannot inspect path", path)
        return info


    def _validate_handle_kind(
        handle: int,
        path: Path,
        expected: str,
    ) -> None:
        info = _attribute_info(handle, path)
        if info.FileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise UnsafePathError(f"reparse point is not allowed: {path}")
        is_directory = bool(info.FileAttributes & _FILE_ATTRIBUTE_DIRECTORY)
        if expected == "directory" and not is_directory:
            raise UnsafePathError(f"path component is not a directory: {path}")
        if expected == "file" and is_directory:
            raise UnsafePathError(f"task document is not a regular file: {path}")


    def _create_file_handle(
        path: Path,
        *,
        access: int,
        creation: int = _OPEN_EXISTING,
        expected: str = "any",
        share_delete: bool = False,
    ) -> int:
        share = _FILE_SHARE_READ | _FILE_SHARE_WRITE
        if share_delete:
            share |= _FILE_SHARE_DELETE
        flags = _FILE_FLAG_OPEN_REPARSE_POINT
        if expected in ("directory", "any"):
            flags |= _FILE_FLAG_BACKUP_SEMANTICS
        if creation == _CREATE_NEW:
            flags |= _FILE_ATTRIBUTE_NORMAL
        handle = _kernel32.CreateFileW(
            os.fspath(path), access, share, None, creation, flags, None
        )
        if handle == _INVALID_HANDLE_VALUE:
            _raise_windows_error("cannot open protected path", path)
        try:
            _validate_handle_kind(handle, path, expected)
        except BaseException:
            _close_handle(handle)
            raise
        return handle


    @contextlib.contextmanager
    def _open_chain(
        root: Path,
        path: Path,
        *,
        final_access: int,
        final_expected: str,
    ) -> Iterator[tuple[Path, int]]:
        root_absolute, candidate, parts = _relative_parts(root, path)
        handles: list[int] = []
        current = root_absolute
        try:
            handles.append(_create_file_handle(
                current,
                access=_FILE_READ_ATTRIBUTES,
                expected="directory",
            ))
            if not parts:
                if final_expected != "directory":
                    raise UnsafePathError(f"protected path cannot be the root: {candidate}")
                yield candidate, handles[-1]
                return
            for index, part in enumerate(parts):
                current /= part
                final = index == len(parts) - 1
                handles.append(_create_file_handle(
                    current,
                    access=final_access if final else _FILE_READ_ATTRIBUTES,
                    expected=final_expected if final else "directory",
                ))
            yield candidate, handles[-1]
        finally:
            for handle in reversed(handles):
                _close_handle(handle)


    def _try_open_leaf(path: Path, *, expected: str = "any") -> int | None:
        try:
            return _create_file_handle(
                path,
                access=_FILE_READ_ATTRIBUTES,
                expected=expected,
            )
        except OSError as error:
            if getattr(error, "winerror", None) in (
                _ERROR_FILE_NOT_FOUND,
                _ERROR_PATH_NOT_FOUND,
            ) or getattr(error, "errno", None) in (
                _ERROR_FILE_NOT_FOUND,
                _ERROR_PATH_NOT_FOUND,
            ):
                return None
            raise


    def _handle_identity(handle: int, path: Path) -> tuple[int, int, int]:
        info = _BY_HANDLE_FILE_INFORMATION()
        if not _kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            _raise_windows_error("cannot identify protected path", path)
        return (
            int(info.dwVolumeSerialNumber),
            int(info.nFileIndexHigh),
            int(info.nFileIndexLow),
        )


    def _read_handle(handle: int, path: Path) -> bytes:
        chunks: list[bytes] = []
        while True:
            buffer = ctypes.create_string_buffer(1 << 20)
            count = wintypes.DWORD()
            if not _kernel32.ReadFile(
                handle, buffer, len(buffer), ctypes.byref(count), None
            ):
                _raise_windows_error("cannot read protected file", path)
            if count.value == 0:
                return b"".join(chunks)
            chunks.append(buffer.raw[:count.value])


    def _write_handle(handle: int, path: Path, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            piece = data[offset:offset + (1 << 20)]
            buffer = ctypes.create_string_buffer(piece)
            count = wintypes.DWORD()
            if not _kernel32.WriteFile(
                handle, buffer, len(piece), ctypes.byref(count), None
            ):
                _raise_windows_error("cannot write protected file", path)
            if count.value == 0:
                raise OSError(f"short write to protected file: {path}")
            offset += count.value
        if not _kernel32.FlushFileBuffers(handle):
            _raise_windows_error("cannot flush protected file", path)


    def _rename_handle(
        handle: int,
        target_parent_handle: int,
        target_path: Path,
        *,
        replace: bool,
    ) -> None:
        target_name = os.fspath(target_path)
        if not target_name or target_name in (".", "..") or "\\" in target_name or "/" in target_name:
            # 绝对路径本身当然含分隔符; 这里只拒绝不是绝对路径的多分量名称.
            if not os.path.isabs(target_name):
                raise UnsafePathError(f"invalid rename target name: {target_name}")
        name_type = wintypes.WCHAR * (len(target_name) + 1)

        class _FILE_RENAME_INFO(ctypes.Structure):
            _fields_ = [
                ("ReplaceIfExists", wintypes.BOOLEAN),
                ("RootDirectory", wintypes.HANDLE),
                ("FileNameLength", wintypes.DWORD),
                ("FileName", name_type),
            ]

        info = _FILE_RENAME_INFO()
        info.ReplaceIfExists = bool(replace)
        # SetFileInformationByHandle 对绝对路径的支持覆盖到 Windows Vista.
        # 目标父目录句柄仍保持打开且不共享 DELETE，绝对路径在本次调用中不会
        # 因祖先目录掉包而改变含义.
        info.RootDirectory = None
        info.FileNameLength = len(target_name.encode("utf-16-le"))
        info.FileName = target_name
        if not _kernel32.SetFileInformationByHandle(
            handle,
            _FILE_RENAME_INFO_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            code = ctypes.get_last_error()
            if not replace and code in (_ERROR_FILE_EXISTS, _ERROR_ALREADY_EXISTS):
                raise FileExistsError(f"rename target already exists: {target_path}")
            _raise_windows_error("cannot atomically rename protected path")


    def _delete_handle(handle: int) -> None:
        info = _FILE_DISPOSITION_INFO(True)
        _kernel32.SetFileInformationByHandle(
            handle,
            _FILE_DISPOSITION_INFO_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )


    def read_regular_file_nofollow(root: Path, path: Path) -> bytes:
        with _open_chain(
            root,
            path,
            final_access=_GENERIC_READ | _FILE_READ_ATTRIBUTES,
            final_expected="file",
        ) as (candidate, handle):
            return _read_handle(handle, candidate)


    def write_text_atomic_nofollow(
        root: Path, path: Path, text: str, *, replace: bool = True
    ) -> None:
        root_absolute, candidate, parts = _relative_parts(root, path)
        if not parts:
            raise UnsafePathError(f"protected file cannot be the root: {candidate}")
        parent = candidate.parent
        with _open_chain(
            root_absolute,
            parent,
            final_access=_FILE_READ_ATTRIBUTES,
            final_expected="directory",
        ) as (_, parent_handle):
            existing = _try_open_leaf(candidate, expected="file")
            if existing is not None:
                _close_handle(existing)
                if not replace:
                    raise FileExistsError(f"protected file already exists: {candidate}")

            temp_path = parent / (
                f".{candidate.name}.{os.getpid()}.{time.time_ns()}.tmp"
            )
            temp_handle = _create_file_handle(
                temp_path,
                access=_GENERIC_WRITE | _DELETE | _FILE_READ_ATTRIBUTES,
                creation=_CREATE_NEW,
                expected="file",
                share_delete=False,
            )
            moved = False
            try:
                identity = _handle_identity(temp_handle, temp_path)
                _write_handle(temp_handle, temp_path, text.encode("utf-8"))
                # 任务文档替换后不能退回父目录继承 ACL。临时文件仍由不可删除的
                # 已验证句柄钉住，在变为可见目标前收紧为当前用户独占 DACL。
                _tighten_private_permissions(temp_path, expected="file")
                _rename_handle(
                    temp_handle,
                    parent_handle,
                    candidate,
                    replace=replace,
                )
                moved = True
                replacement = _try_open_leaf(candidate, expected="file")
                if replacement is None:
                    raise UnsafePathError(
                        f"atomic replacement disappeared after rename: {candidate}"
                    )
                try:
                    if _handle_identity(replacement, candidate) != identity:
                        raise UnsafePathError(
                            f"atomic replacement identity changed after rename: {candidate}"
                        )
                finally:
                    _close_handle(replacement)
            finally:
                if not moved:
                    _delete_handle(temp_handle)
                _close_handle(temp_handle)


    def create_directory_with_text_file_nofollow(
        root: Path,
        directory: Path,
        filename: str,
        text: str,
    ) -> Path:
        """先在受保护父目录内完成私有目录和文件，再原子发布目录入口."""
        if not filename or Path(filename).name != filename or filename in (".", ".."):
            raise UnsafePathError(f"invalid protected filename: {filename}")
        root_absolute, candidate, parts = _relative_parts(root, directory)
        if not parts:
            raise UnsafePathError("protected directory cannot be the root")
        parent = candidate.parent
        with _open_chain(
            root_absolute,
            parent,
            final_access=_FILE_READ_ATTRIBUTES,
            final_expected="directory",
        ) as (_, parent_handle):
            collision = _try_open_leaf(candidate, expected="any")
            if collision is not None:
                _close_handle(collision)
                raise FileExistsError(f"protected directory already exists: {candidate}")

            temporary = parent / (
                f".{candidate.name}.{os.getpid()}.{time.time_ns()}.tmp"
            )
            if not _kernel32.CreateDirectoryW(os.fspath(temporary), None):
                _raise_windows_error("cannot create protected directory", temporary)
            directory_handle: int | None = None
            moved = False
            try:
                directory_handle = _create_file_handle(
                    temporary,
                    access=_DELETE | _FILE_READ_ATTRIBUTES,
                    expected="directory",
                )
                identity = _handle_identity(directory_handle, temporary)
                _tighten_private_permissions(temporary, expected="directory")
                write_text_atomic_nofollow(
                    root_absolute,
                    temporary / filename,
                    text,
                    replace=False,
                )
                _rename_handle(
                    directory_handle,
                    parent_handle,
                    candidate,
                    replace=False,
                )
                moved = True
                published = _try_open_leaf(candidate, expected="directory")
                if published is None:
                    raise UnsafePathError(
                        f"published directory disappeared after rename: {candidate}"
                    )
                try:
                    if _handle_identity(published, candidate) != identity:
                        raise UnsafePathError(
                            f"published directory identity changed after rename: {candidate}"
                        )
                finally:
                    _close_handle(published)
                return candidate
            finally:
                if directory_handle is not None and not moved:
                    try:
                        child = _create_file_handle(
                            temporary / filename,
                            access=_DELETE | _FILE_READ_ATTRIBUTES,
                            expected="file",
                        )
                    except OSError:
                        child = None
                    if child is not None:
                        _delete_handle(child)
                        _close_handle(child)
                    _delete_handle(directory_handle)
                if directory_handle is not None:
                    _close_handle(directory_handle)


    def rename_nofollow(root: Path, source: Path, target: Path) -> None:
        root_absolute, source_absolute, source_parts = _relative_parts(root, source)
        _, target_absolute, target_parts = _relative_parts(root, target)
        if not source_parts or not target_parts:
            raise UnsafePathError("cannot rename the protected root")
        with _open_chain(
            root_absolute,
            source_absolute,
            final_access=_DELETE | _FILE_READ_ATTRIBUTES,
            final_expected="any",
        ) as (_, source_handle):
            source_identity = _handle_identity(source_handle, source_absolute)
            source_info = _attribute_info(source_handle, source_absolute)
            source_expected = (
                "directory"
                if source_info.FileAttributes & _FILE_ATTRIBUTE_DIRECTORY
                else "file"
            )
            with _open_chain(
                root_absolute,
                target_absolute.parent,
                final_access=_FILE_READ_ATTRIBUTES,
                final_expected="directory",
            ) as (_, target_parent_handle):
                collision = _try_open_leaf(target_absolute, expected="any")
                if collision is not None:
                    _close_handle(collision)
                    raise FileExistsError(f"rename target already exists: {target_absolute}")
                _rename_handle(
                    source_handle,
                    target_parent_handle,
                    target_absolute,
                    replace=False,
                )
                moved = _try_open_leaf(target_absolute, expected=source_expected)
                if moved is None:
                    raise UnsafePathError(
                        f"renamed path is missing after move: {target_absolute}"
                    )
                try:
                    if _handle_identity(moved, target_absolute) != source_identity:
                        raise UnsafePathError(
                            f"renamed path identity changed after move: {target_absolute}"
                        )
                finally:
                    _close_handle(moved)
                leftover = _try_open_leaf(source_absolute, expected="any")
                if leftover is not None:
                    _close_handle(leftover)
                    raise UnsafePathError(
                        f"source path still exists after move: {source_absolute}"
                    )


else:
    def _openat_nofollow(
        dir_fd: int,
        name: str,
        flags: int,
        mode: int = 0o777,
    ) -> int:
        open_flags = flags | getattr(os, "O_NOFOLLOW", 0)
        return os.open(name, open_flags, mode, dir_fd=dir_fd)


    @contextlib.contextmanager
    def _open_posix_parent(root: Path, path: Path) -> Iterator[tuple[Path, int]]:
        root_absolute, candidate, parts = _relative_parts(root, path)
        if not parts:
            raise UnsafePathError(f"protected path cannot be the root: {candidate}")
        dir_fd = os.open(
            root_absolute,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            for part in parts[:-1]:
                next_fd = _openat_nofollow(
                    dir_fd,
                    part,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
                os.close(dir_fd)
                dir_fd = next_fd
            yield candidate, dir_fd
        finally:
            os.close(dir_fd)


    def read_regular_file_nofollow(root: Path, path: Path) -> bytes:
        with _open_posix_parent(root, path) as (candidate, parent_fd):
            fd = _openat_nofollow(parent_fd, candidate.name, os.O_RDONLY)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise UnsafePathError(
                        f"task document is not a regular file: {candidate}"
                    )
                chunks: list[bytes] = []
                while True:
                    piece = os.read(fd, 1 << 20)
                    if not piece:
                        return b"".join(chunks)
                    chunks.append(piece)
            finally:
                os.close(fd)


    def write_text_atomic_nofollow(
        root: Path, path: Path, text: str, *, replace: bool = True
    ) -> None:
        with _open_posix_parent(root, path) as (candidate, parent_fd):
            mode = None
            try:
                existing = _openat_nofollow(parent_fd, candidate.name, os.O_RDONLY)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                try:
                    info = os.fstat(existing)
                    if not stat.S_ISREG(info.st_mode):
                        raise UnsafePathError(
                            f"task document is not a regular file: {candidate}"
                        )
                    mode = stat.S_IMODE(info.st_mode)
                finally:
                    os.close(existing)
                if not replace:
                    raise FileExistsError(f"protected file already exists: {candidate}")
            temp_name = f".{candidate.name}.{os.getpid()}.{time.time_ns()}.tmp"
            temp_fd = None
            try:
                temp_fd = _openat_nofollow(
                    parent_fd,
                    temp_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                if mode is not None:
                    os.fchmod(temp_fd, mode)
                data = text.encode("utf-8")
                offset = 0
                while offset < len(data):
                    offset += os.write(temp_fd, data[offset:])
                os.fsync(temp_fd)
                os.close(temp_fd)
                temp_fd = None
                if replace:
                    os.replace(
                        temp_name,
                        candidate.name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                    )
                else:
                    os.link(
                        temp_name,
                        candidate.name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    os.unlink(temp_name, dir_fd=parent_fd)
            finally:
                if temp_fd is not None:
                    os.close(temp_fd)
                try:
                    os.unlink(temp_name, dir_fd=parent_fd)
                except OSError:
                    pass


    def create_directory_with_text_file_nofollow(
        root: Path,
        directory: Path,
        filename: str,
        text: str,
    ) -> Path:
        """POSIX 以父目录 fd 原子占用任务目录，再安全写入其文档."""
        if not filename or Path(filename).name != filename or filename in (".", ".."):
            raise UnsafePathError(f"invalid protected filename: {filename}")
        with _open_posix_parent(root, directory) as (candidate, parent_fd):
            os.mkdir(candidate.name, mode=0o700, dir_fd=parent_fd)
        try:
            write_text_atomic_nofollow(
                root,
                candidate / filename,
                text,
                replace=False,
            )
        except BaseException:
            try:
                os.unlink(candidate / filename)
            except OSError:
                pass
            try:
                os.rmdir(candidate)
            except OSError:
                pass
            raise
        return candidate


    def rename_nofollow(root: Path, source: Path, target: Path) -> None:
        root_absolute, source_absolute, source_parts = _relative_parts(root, source)
        _, target_absolute, target_parts = _relative_parts(root, target)
        if len(source_parts) < 2 or len(target_parts) < 2:
            raise UnsafePathError("protected rename must stay below state directories")
        with _open_posix_parent(root_absolute, source_absolute) as (_, source_parent_fd):
            with _open_posix_parent(root_absolute, target_absolute) as (_, target_parent_fd):
                try:
                    os.lstat(target_absolute.name, dir_fd=target_parent_fd)
                except FileNotFoundError:
                    pass
                else:
                    raise FileExistsError(f"rename target already exists: {target_absolute}")
                os.rename(
                    source_absolute.name,
                    target_absolute.name,
                    src_dir_fd=source_parent_fd,
                    dst_dir_fd=target_parent_fd,
                )


@contextlib.contextmanager
def exclusive_file_lock(file: BinaryIO) -> Iterator[None]:
    """在整个文件上取得阻塞式跨进程独占锁."""
    if os.name == "nt":
        handle = msvcrt.get_osfhandle(file.fileno())  # type: ignore[name-defined]
        overlapped = _OVERLAPPED()  # type: ignore[name-defined]
        if not _kernel32.LockFileEx(  # type: ignore[name-defined]
            handle,
            _LOCKFILE_EXCLUSIVE_LOCK,  # type: ignore[name-defined]
            0,
            0xFFFFFFFF,
            0xFFFFFFFF,
            ctypes.byref(overlapped),  # type: ignore[name-defined]
        ):
            _raise_windows_error("cannot lock file")
        try:
            yield
        finally:
            if not _kernel32.UnlockFileEx(  # type: ignore[name-defined]
                handle,
                0,
                0xFFFFFFFF,
                0xFFFFFFFF,
                ctypes.byref(overlapped),  # type: ignore[name-defined]
            ):
                _raise_windows_error("cannot unlock file")
        return

    import fcntl

    fcntl.flock(file, fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(file, fcntl.LOCK_UN)


def _tighten_private_permissions(path: Path, *, expected: str) -> None:
    if os.name != "nt":
        os.chmod(path, 0o700 if expected == "directory" else 0o600)
        return

    handle = _create_file_handle(  # type: ignore[name-defined]
        _absolute_path(path),
        access=_READ_CONTROL | _WRITE_DAC | _FILE_READ_ATTRIBUTES,  # type: ignore[name-defined]
        expected=expected,
        share_delete=True,
    )
    token = wintypes.HANDLE()  # type: ignore[name-defined]
    sid_string = wintypes.LPWSTR()  # type: ignore[name-defined]
    security_descriptor = ctypes.c_void_p()  # type: ignore[name-defined]
    try:
        if not _advapi32.OpenProcessToken(  # type: ignore[name-defined]
            _kernel32.GetCurrentProcess(),  # type: ignore[name-defined]
            _TOKEN_QUERY,  # type: ignore[name-defined]
            ctypes.byref(token),  # type: ignore[name-defined]
        ):
            _raise_windows_error("cannot open current process token")
        size = wintypes.DWORD()  # type: ignore[name-defined]
        _advapi32.GetTokenInformation(  # type: ignore[name-defined]
            token,
            _TOKEN_USER_CLASS,  # type: ignore[name-defined]
            None,
            0,
            ctypes.byref(size),  # type: ignore[name-defined]
        )
        token_data = ctypes.create_string_buffer(size.value)  # type: ignore[name-defined]
        if not _advapi32.GetTokenInformation(  # type: ignore[name-defined]
            token,
            _TOKEN_USER_CLASS,  # type: ignore[name-defined]
            token_data,
            size,
            ctypes.byref(size),  # type: ignore[name-defined]
        ):
            _raise_windows_error("cannot read current user SID")
        token_user = ctypes.cast(  # type: ignore[name-defined]
            token_data, ctypes.POINTER(_TOKEN_USER)  # type: ignore[name-defined]
        ).contents
        if not _advapi32.ConvertSidToStringSidW(  # type: ignore[name-defined]
            token_user.User.Sid,
            ctypes.byref(sid_string),  # type: ignore[name-defined]
        ):
            _raise_windows_error("cannot format current user SID")
        inheritance = "OICI" if expected == "directory" else ""
        sddl = f"D:P(A;{inheritance};FA;;;{sid_string.value})"
        if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(  # type: ignore[name-defined]
            sddl,
            _SDDL_REVISION_1,  # type: ignore[name-defined]
            ctypes.byref(security_descriptor),  # type: ignore[name-defined]
            None,
        ):
            _raise_windows_error("cannot build private file ACL")
        present = wintypes.BOOL()  # type: ignore[name-defined]
        defaulted = wintypes.BOOL()  # type: ignore[name-defined]
        dacl = ctypes.c_void_p()  # type: ignore[name-defined]
        if not _advapi32.GetSecurityDescriptorDacl(  # type: ignore[name-defined]
            security_descriptor,
            ctypes.byref(present),  # type: ignore[name-defined]
            ctypes.byref(dacl),  # type: ignore[name-defined]
            ctypes.byref(defaulted),  # type: ignore[name-defined]
        ) or not present.value:
            _raise_windows_error("cannot read private file ACL")
        status = _advapi32.SetSecurityInfo(  # type: ignore[name-defined]
            handle,
            _SE_FILE_OBJECT,  # type: ignore[name-defined]
            _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,  # type: ignore[name-defined]
            None,
            None,
            dacl,
            None,
        )
        if status:
            ctypes.set_last_error(status)  # type: ignore[name-defined]
            _raise_windows_error("cannot tighten private file ACL", path)
    finally:
        if security_descriptor.value:
            _kernel32.LocalFree(security_descriptor)  # type: ignore[name-defined]
        if sid_string:
            _kernel32.LocalFree(sid_string)  # type: ignore[name-defined]
        if token:
            _close_handle(token)  # type: ignore[name-defined]
        _close_handle(handle)  # type: ignore[name-defined]


def tighten_private_file_permissions(path: Path) -> None:
    """把文件权限收紧为 POSIX 0600 或 Windows 当前用户独占 DACL.

    Windows ACL 会关闭继承，只给当前进程 token 的用户 SID 显式完全控制.
    任一步失败都抛错，调用方不得把失败当成权限已经收紧.
    """
    _tighten_private_permissions(path, expected="file")


def tighten_private_directory_permissions(path: Path) -> None:
    """把目录权限收紧为 POSIX 0700 或 Windows 当前用户独占 DACL."""
    _tighten_private_permissions(path, expected="directory")
