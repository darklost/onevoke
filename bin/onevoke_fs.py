#!/usr/bin/env python3

"""Onevoke 的跨平台底层文件系统原语.

看板任务与记忆合并路径属于安全边界. POSIX 使用 ``openat`` 和
``O_NOFOLLOW`` 逐级打开; Windows 只用 ``CreateFileW`` 打开不可替换的卷根,
之后通过 ``NtCreateFile`` 的 ``RootDirectory`` 对已固定父目录句柄逐分量
打开或创建. 子路径不再经绝对路径重新解析, 因而即使攻击者在验证后
把空目录原地改成 junction, 后续读写、创建、rename 和 ACL 也只会
作用于已固定的对象或明确失败, 不会跟随到 reparse target.
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
    _ntdll = ctypes.WinDLL("ntdll")

    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _FILE_APPEND_DATA = 0x00000004
    _FILE_LIST_DIRECTORY = 0x00000001
    _DELETE = 0x00010000
    _READ_CONTROL = 0x00020000
    _WRITE_DAC = 0x00040000
    _FILE_TRAVERSE = 0x00000020
    _FILE_READ_ATTRIBUTES = 0x00000080
    _SYNCHRONIZE = 0x00100000
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
    _ERROR_CANT_RESOLVE_FILENAME = 1921
    _FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
    _FILE_DISPOSITION_INFO_CLASS = 4
    _NT_FILE_RENAME_INFORMATION_CLASS = 10
    _NT_FILE_NAMES_INFORMATION_CLASS = 12
    _NT_FILE_OPEN = 1
    _NT_FILE_CREATE = 2
    _NT_FILE_DIRECTORY_FILE = 0x00000001
    _NT_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
    _NT_FILE_NON_DIRECTORY_FILE = 0x00000040
    _NT_FILE_OPEN_FOR_BACKUP_INTENT = 0x00004000
    _NT_FILE_OPEN_REPARSE_POINT = 0x00200000
    _OBJ_CASE_INSENSITIVE = 0x00000040
    _STATUS_NO_MORE_FILES = 0x80000006
    _STATUS_REPARSE_POINT_ENCOUNTERED = 0xC000050B
    _STATUS_IO_REPARSE_TAG_NOT_HANDLED = 0xC0000279
    _STATUS_STOPPED_ON_SYMLINK = 0x8000002D
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

    class _UNICODE_STRING(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", wintypes.LPWSTR),
        ]

    class _OBJECT_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(_UNICODE_STRING)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", ctypes.c_void_p),
            ("SecurityQualityOfService", ctypes.c_void_p),
        ]

    class _IO_STATUS_BLOCK(ctypes.Structure):
        _fields_ = [
            ("Status", ctypes.c_void_p),
            ("Information", ctypes.c_size_t),
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
    _ntdll.NtCreateFile.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(_OBJECT_ATTRIBUTES),
        ctypes.POINTER(_IO_STATUS_BLOCK),
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _ntdll.NtCreateFile.restype = wintypes.LONG
    _ntdll.NtSetInformationFile.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_IO_STATUS_BLOCK),
        ctypes.c_void_p,
        wintypes.ULONG,
        ctypes.c_int,
    ]
    _ntdll.NtSetInformationFile.restype = wintypes.LONG
    _ntdll.NtQueryDirectoryFile.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(_IO_STATUS_BLOCK),
        ctypes.c_void_p,
        wintypes.ULONG,
        ctypes.c_int,
        wintypes.BOOLEAN,
        ctypes.POINTER(_UNICODE_STRING),
        wintypes.BOOLEAN,
    ]
    _ntdll.NtQueryDirectoryFile.restype = wintypes.LONG
    _ntdll.RtlNtStatusToDosError.argtypes = [wintypes.LONG]
    _ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG


def _raise_windows_error(action: str, path: Path | None = None) -> None:
    code = ctypes.get_last_error()  # type: ignore[name-defined]
    detail = ctypes.FormatError(code).strip()  # type: ignore[name-defined]
    target = f": {path}" if path is not None else ""
    raise OSError(code, f"{action}{target}: {detail}", os.fspath(path) if path else None)


if os.name == "nt":
    def _unsigned_ntstatus(status: int) -> int:
        return int(status) & 0xFFFFFFFF


    def _raise_nt_error(action: str, status: int, path: Path) -> None:
        unsigned = _unsigned_ntstatus(status)
        if unsigned in (
            _STATUS_REPARSE_POINT_ENCOUNTERED,
            _STATUS_IO_REPARSE_TAG_NOT_HANDLED,
            _STATUS_STOPPED_ON_SYMLINK,
        ):
            raise UnsafePathError(f"reparse point is not allowed: {path}")
        code = int(_ntdll.RtlNtStatusToDosError(status))
        if code == _ERROR_CANT_RESOLVE_FILENAME:
            raise UnsafePathError(f"reparse point is not allowed: {path}")
        detail = ctypes.FormatError(code).strip()
        raise OSError(
            code,
            f"{action}: {path}: {detail}",
            os.fspath(path),
        )


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
        """只用于打开卷根; 所有子路径必须走 ``_open_relative_handle``."""
        if expected == "directory":
            access |= _FILE_TRAVERSE
        share = _FILE_SHARE_READ | _FILE_SHARE_WRITE
        if share_delete:
            share |= _FILE_SHARE_DELETE
        flags = _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_BACKUP_SEMANTICS
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


    def _validate_relative_name(name: str, path: Path) -> None:
        if (
            not name
            or name in (".", "..")
            or "\\" in name
            or "/" in name
            or ":" in name
            or "\x00" in name
        ):
            raise UnsafePathError(f"invalid protected path component: {path}")


    @contextlib.contextmanager
    def _private_security_descriptor(
        expected: str,
    ) -> Iterator[ctypes.c_void_p]:
        """构造仅当前用户完全控制的受保护 DACL, 并维持指针生命周期."""
        if expected not in ("file", "directory"):
            raise ValueError(f"unsupported private object kind: {expected}")
        token = wintypes.HANDLE()
        sid_string = wintypes.LPWSTR()
        security_descriptor = ctypes.c_void_p()
        try:
            if not _advapi32.OpenProcessToken(
                _kernel32.GetCurrentProcess(),
                _TOKEN_QUERY,
                ctypes.byref(token),
            ):
                _raise_windows_error("cannot open current process token")
            size = wintypes.DWORD()
            _advapi32.GetTokenInformation(
                token,
                _TOKEN_USER_CLASS,
                None,
                0,
                ctypes.byref(size),
            )
            token_data = ctypes.create_string_buffer(size.value)
            if not _advapi32.GetTokenInformation(
                token,
                _TOKEN_USER_CLASS,
                token_data,
                size,
                ctypes.byref(size),
            ):
                _raise_windows_error("cannot read current user SID")
            token_user = ctypes.cast(
                token_data, ctypes.POINTER(_TOKEN_USER)
            ).contents
            if not _advapi32.ConvertSidToStringSidW(
                token_user.User.Sid,
                ctypes.byref(sid_string),
            ):
                _raise_windows_error("cannot format current user SID")
            inheritance = "OICI" if expected == "directory" else ""
            sddl = f"D:P(A;{inheritance};FA;;;{sid_string.value})"
            if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl,
                _SDDL_REVISION_1,
                ctypes.byref(security_descriptor),
                None,
            ):
                _raise_windows_error("cannot build private file ACL")
            yield security_descriptor
        finally:
            if security_descriptor.value:
                _kernel32.LocalFree(security_descriptor)
            if sid_string:
                _kernel32.LocalFree(sid_string)
            if token:
                _close_handle(token)


    def _open_relative_handle(
        parent_handle: int,
        name: str,
        path: Path,
        *,
        access: int,
        creation: int = _OPEN_EXISTING,
        expected: str = "any",
        share_write: bool = True,
        share_delete: bool = False,
        private_creation: str | None = None,
    ) -> int:
        """相对已固定父目录打开单一分量, 永不重新解析绝对路径."""
        _validate_relative_name(name, path)
        if private_creation is not None:
            if creation != _CREATE_NEW:
                raise ValueError("private_creation requires CREATE_NEW")
            if (
                private_creation != expected
                or expected not in ("file", "directory")
            ):
                raise ValueError(
                    "private_creation must match the created file or directory kind"
                )
        name_buffer = ctypes.create_unicode_buffer(name)
        name_length = len(name.encode("utf-16-le"))
        object_name = _UNICODE_STRING(
            name_length,
            name_length + ctypes.sizeof(wintypes.WCHAR),
            ctypes.cast(name_buffer, wintypes.LPWSTR),
        )
        options = (
            _NT_FILE_SYNCHRONOUS_IO_NONALERT
            | _NT_FILE_OPEN_FOR_BACKUP_INTENT
            | _NT_FILE_OPEN_REPARSE_POINT
        )
        if expected == "directory":
            access |= _FILE_TRAVERSE
            options |= _NT_FILE_DIRECTORY_FILE
        elif expected == "file":
            options |= _NT_FILE_NON_DIRECTORY_FILE
        disposition = _NT_FILE_CREATE if creation == _CREATE_NEW else _NT_FILE_OPEN
        share = _FILE_SHARE_READ
        if share_write:
            share |= _FILE_SHARE_WRITE
        if share_delete:
            share |= _FILE_SHARE_DELETE
        handle = wintypes.HANDLE()
        io_status = _IO_STATUS_BLOCK()
        descriptor_context = (
            _private_security_descriptor(private_creation)
            if private_creation is not None
            else contextlib.nullcontext(None)
        )
        with descriptor_context as security_descriptor:
            attributes = _OBJECT_ATTRIBUTES(
                ctypes.sizeof(_OBJECT_ATTRIBUTES),
                parent_handle,
                ctypes.pointer(object_name),
                # RootDirectory 已是固定文件对象. OBJ_DONT_REPARSE 会在该
                # 对象验证后被原地加上 reparse tag 时拒绝相对访问,
                # 反而破坏句柄固定语义. FILE_OPEN_REPARSE_POINT 保证新
                # 打开的单分量自身不被跟随, 随后再从句柄检查 tag.
                _OBJ_CASE_INSENSITIVE,
                security_descriptor.value
                if security_descriptor is not None
                else None,
                None,
            )
            status = _ntdll.NtCreateFile(
                ctypes.byref(handle),
                access | _SYNCHRONIZE,
                ctypes.byref(attributes),
                ctypes.byref(io_status),
                None,
                _FILE_ATTRIBUTE_NORMAL if expected != "directory" else 0,
                share,
                disposition,
                options,
                None,
                0,
            )
        if status < 0:
            _raise_nt_error("cannot open protected path", status, path)
        value = int(handle.value)
        try:
            _validate_handle_kind(value, path, expected)
        except BaseException:
            _close_handle(value)
            raise
        return value


    @contextlib.contextmanager
    def _open_chain(
        root: Path,
        path: Path,
        *,
        final_access: int,
        final_expected: str,
    ) -> Iterator[tuple[Path, int]]:
        root_absolute, candidate, parts = _relative_parts(root, path)
        anchor = Path(root_absolute.anchor)
        if not root_absolute.anchor:
            raise UnsafePathError(f"protected root has no absolute anchor: {root_absolute}")
        _, _, root_parts = _relative_parts(anchor, root_absolute)
        chain_parts = (*root_parts, *parts)
        handles: list[int] = []
        current = anchor
        try:
            first_expected = final_expected if not chain_parts else "directory"
            handles.append(_create_file_handle(
                current,
                access=final_access if not chain_parts else _FILE_READ_ATTRIBUTES,
                expected=first_expected,
            ))
            if not chain_parts:
                yield candidate, handles[-1]
                return
            for index, part in enumerate(chain_parts):
                current /= part
                final = index == len(chain_parts) - 1
                expected = final_expected if final else "directory"
                if final:
                    final_handle = _try_open_leaf(
                        handles[-1],
                        part,
                        current,
                        access=final_access,
                        expected=expected,
                    )
                    if final_handle is None:
                        raise FileNotFoundError(
                            _ERROR_FILE_NOT_FOUND,
                            f"protected path does not exist: {current}",
                            os.fspath(current),
                        )
                    handles.append(final_handle)
                else:
                    handles.append(_open_relative_handle(
                        handles[-1],
                        part,
                        current,
                        access=_FILE_READ_ATTRIBUTES,
                        expected="directory",
                    ))
            yield candidate, handles[-1]
        finally:
            for handle in reversed(handles):
                _close_handle(handle)


    def _try_open_leaf(
        parent_handle: int,
        name: str,
        path: Path,
        *,
        access: int = _FILE_READ_ATTRIBUTES,
        expected: str = "any",
        share_write: bool = True,
        share_delete: bool = False,
    ) -> int | None:
        probe: int | None = None
        try:
            # 先以最小权限打开“本体”才能把 directory junction
            # 稳定识别为 reparse point；直接用高权限和
            # FILE_NON_DIRECTORY_FILE 打开可能只返回 access denied.
            probe = _open_relative_handle(
                parent_handle,
                name,
                path,
                access=_FILE_READ_ATTRIBUTES,
                expected="any",
                share_write=share_write,
                share_delete=share_delete or bool(access & _DELETE),
            )
            _validate_handle_kind(probe, path, expected)
            if access == _FILE_READ_ATTRIBUTES:
                result = probe
                probe = None
                return result
            identity = _handle_identity(probe, path)
            result = _open_relative_handle(
                parent_handle,
                name,
                path,
                access=access,
                expected=expected,
                share_write=share_write,
                share_delete=share_delete,
            )
            if _handle_identity(result, path) != identity:
                _close_handle(result)
                raise UnsafePathError(
                    f"protected path identity changed while opening: {path}"
                )
            return result
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
        finally:
            if probe is not None:
                _close_handle(probe)


    def _open_or_create_regular_file_handle(
        parent_handle: int, name: str, path: Path, *, access: int
    ) -> int:
        """先用最小权限识别既有叶对象，再固定身份并取得高权限句柄."""
        while True:
            probe = _try_open_leaf(parent_handle, name, path, expected="file")
            if probe is not None:
                try:
                    return _open_relative_handle(
                        parent_handle,
                        name,
                        path,
                        access=access,
                        creation=_OPEN_EXISTING,
                        expected="file",
                        share_delete=False,
                    )
                finally:
                    _close_handle(probe)
            try:
                return _open_relative_handle(
                    parent_handle,
                    name,
                    path,
                    access=access,
                    creation=_CREATE_NEW,
                    expected="file",
                    share_delete=False,
                    private_creation="file",
                )
            except OSError as error:
                code = getattr(error, "winerror", None) or getattr(error, "errno", None)
                if code in (_ERROR_FILE_EXISTS, _ERROR_ALREADY_EXISTS):
                    continue
                raise


    def _is_missing_windows_error(error: OSError) -> bool:
        return getattr(error, "winerror", None) in (
            _ERROR_FILE_NOT_FOUND,
            _ERROR_PATH_NOT_FOUND,
        ) or getattr(error, "errno", None) in (
            _ERROR_FILE_NOT_FOUND,
            _ERROR_PATH_NOT_FOUND,
        )


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
        target_name = target_path.name
        if (
            not target_name
            or target_name in (".", "..")
            or "\\" in target_name
            or "/" in target_name
        ):
            raise UnsafePathError(f"invalid rename target name: {target_name}")
        encoded = target_name.encode("utf-16-le")
        is_32_bit = ctypes.sizeof(ctypes.c_void_p) == 4
        handle_offset = 4 if is_32_bit else 8
        length_offset = 8 if is_32_bit else 16
        filename_offset = 12 if is_32_bit else 20
        buffer = ctypes.create_string_buffer(filename_offset + len(encoded))
        ctypes.c_ubyte.from_buffer(buffer, 0).value = int(bool(replace))
        wintypes.HANDLE.from_buffer(buffer, handle_offset).value = target_parent_handle
        wintypes.ULONG.from_buffer(buffer, length_offset).value = len(encoded)
        buffer[filename_offset:filename_offset + len(encoded)] = encoded
        io_status = _IO_STATUS_BLOCK()
        status = _ntdll.NtSetInformationFile(
            handle,
            ctypes.byref(io_status),
            buffer,
            filename_offset + len(encoded),
            _NT_FILE_RENAME_INFORMATION_CLASS,
        )
        if status < 0:
            code = int(_ntdll.RtlNtStatusToDosError(status))
            if not replace and code in (_ERROR_FILE_EXISTS, _ERROR_ALREADY_EXISTS):
                raise FileExistsError(f"rename target already exists: {target_path}")
            _raise_nt_error("cannot atomically rename protected path", status, target_path)


    def _delete_handle(
        handle: int,
        path: Path | None = None,
        *,
        required: bool = False,
    ) -> None:
        info = _FILE_DISPOSITION_INFO(True)
        deleted = _kernel32.SetFileInformationByHandle(
            handle,
            _FILE_DISPOSITION_INFO_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not deleted and required:
            _raise_windows_error("cannot delete failed protected path", path)


    def read_regular_file_nofollow(root: Path, path: Path) -> bytes:
        with _open_chain(
            root,
            path,
            final_access=_GENERIC_READ | _FILE_READ_ATTRIBUTES,
            final_expected="file",
        ) as (candidate, handle):
            return _read_handle(handle, candidate)


    def read_regular_file_with_identity_nofollow(
        root: Path, path: Path
    ) -> tuple[tuple[int, ...], bytes]:
        """从同一固定句柄读取普通文件及其稳定身份."""
        with _open_chain(
            root,
            path,
            final_access=_GENERIC_READ | _FILE_READ_ATTRIBUTES,
            final_expected="file",
        ) as (candidate, handle):
            return _handle_identity(handle, candidate), _read_handle(handle, candidate)


    def read_regular_file_if_exists_nofollow(
        root: Path, path: Path
    ) -> bytes | None:
        """安全读取可选普通文件; 只把真实缺失视为 ``None``."""
        root_absolute, candidate, parts = _relative_parts(root, path)
        if not parts:
            raise UnsafePathError(f"protected file cannot be the root: {candidate}")
        try:
            with _open_chain(
                root_absolute,
                candidate.parent,
                final_access=_FILE_READ_ATTRIBUTES,
                final_expected="directory",
            ) as (_, parent_handle):
                handle = _try_open_leaf(
                    parent_handle,
                    candidate.name,
                    candidate,
                    access=_GENERIC_READ | _FILE_READ_ATTRIBUTES,
                    expected="file",
                )
                if handle is None:
                    return None
                try:
                    return _read_handle(handle, candidate)
                finally:
                    _close_handle(handle)
        except OSError as error:
            if _is_missing_windows_error(error):
                return None
            raise


    @contextlib.contextmanager
    def _open_regular_stream_if_exists_nofollow(
        root: Path,
        path: Path,
        *,
        private_acl_access: bool,
    ) -> Iterator[BinaryIO | None]:
        """在不共享 WRITE/DELETE 的叶句柄上给调用方稳定读取.

        ``private_acl_access`` 同时请求 READ_CONTROL/WRITE_DAC, 使调用方
        可在校验内容后直接收紧同一句柄的 DACL.
        """
        root_absolute, candidate, parts = _relative_parts(root, path)
        if not parts:
            raise UnsafePathError(f"protected file cannot be the root: {candidate}")
        stack = contextlib.ExitStack()
        try:
            _, parent_handle = stack.enter_context(_open_chain(
                root_absolute,
                candidate.parent,
                final_access=_FILE_READ_ATTRIBUTES,
                final_expected="directory",
            ))
            access = _GENERIC_READ | _FILE_READ_ATTRIBUTES
            if private_acl_access:
                access |= _READ_CONTROL | _WRITE_DAC
            handle = _try_open_leaf(
                parent_handle,
                candidate.name,
                candidate,
                access=access,
                expected="file",
                share_write=False,
                share_delete=False,
            )
        except OSError as error:
            stack.close()
            if _is_missing_windows_error(error):
                yield None
                return
            raise
        if handle is None:
            stack.close()
            yield None
            return
        file: BinaryIO | None = None
        try:
            descriptor = msvcrt.open_osfhandle(
                handle, os.O_RDONLY | os.O_BINARY
            )
            handle = _INVALID_HANDLE_VALUE
            file = os.fdopen(descriptor, "rb", buffering=0)
            yield file
        finally:
            try:
                if file is not None:
                    file.close()
                else:
                    _close_handle(handle)
            finally:
                stack.close()


    @contextlib.contextmanager
    def open_regular_file_if_exists_nofollow(
        root: Path, path: Path
    ) -> Iterator[BinaryIO | None]:
        """安全打开可选普通文件; 句柄存活期内拒绝写入和替换."""
        with _open_regular_stream_if_exists_nofollow(
            root, path, private_acl_access=False
        ) as file:
            yield file


    @contextlib.contextmanager
    def open_private_regular_file_if_exists_nofollow(
        root: Path, path: Path
    ) -> Iterator[BinaryIO | None]:
        """打开可在内容校验后用同一句柄迁移 DACL 的可选文件."""
        with _open_regular_stream_if_exists_nofollow(
            root, path, private_acl_access=True
        ) as file:
            yield file


    def ensure_directory_path_nofollow(path: Path) -> Path:
        """从卷根逐分量打开或创建, 仅收紧本次新建目录的 DACL."""
        candidate = _absolute_path(path)
        anchor = Path(candidate.anchor)
        if not candidate.anchor:
            raise UnsafePathError(
                f"protected directory has no absolute anchor: {candidate}"
            )
        _, _, parts = _relative_parts(anchor, candidate)
        handles: list[int] = []
        current = anchor
        with _open_chain(
            anchor,
            anchor,
            final_access=_FILE_READ_ATTRIBUTES,
            final_expected="directory",
        ) as (_, root_handle):
            parent_handle = root_handle
            try:
                for part in parts:
                    current /= part
                    handle = _try_open_leaf(
                        parent_handle,
                        part,
                        current,
                        expected="directory",
                    )
                    if handle is None:
                        try:
                            handle = _open_relative_handle(
                                parent_handle,
                                part,
                                current,
                                access=(
                                    _DELETE
                                    | _READ_CONTROL
                                    | _WRITE_DAC
                                    | _FILE_READ_ATTRIBUTES
                                ),
                                creation=_CREATE_NEW,
                                expected="directory",
                                private_creation="directory",
                            )
                        except OSError as create_error:
                            code = (
                                getattr(create_error, "winerror", None)
                                or getattr(create_error, "errno", None)
                            )
                            if code not in (_ERROR_FILE_EXISTS, _ERROR_ALREADY_EXISTS):
                                raise
                            handle = _try_open_leaf(
                                parent_handle,
                                part,
                                current,
                                expected="directory",
                            )
                            if handle is None:
                                raise FileNotFoundError(current)
                        else:
                            # 先进入 cleanup 列表再收紧 ACL. 失败时用同一
                            # DELETE 句柄删除未发布的空目录, 不得留下
                            # 继承 ACL 的“既有祖先”影响下次重试.
                            handles.append(handle)
                            try:
                                _tighten_private_handle(
                                    handle, current, expected="directory"
                                )
                            except BaseException as harden_error:
                                cleanup_error: BaseException | None = None
                                try:
                                    _delete_handle(
                                        handle, current, required=True
                                    )
                                except BaseException as error:
                                    cleanup_error = error
                                finally:
                                    handles.pop()
                                    _close_handle(handle)
                                if cleanup_error is not None:
                                    raise cleanup_error from harden_error
                                raise
                            parent_handle = handle
                            continue
                    handles.append(handle)
                    parent_handle = handle
                return candidate
            finally:
                for handle in reversed(handles):
                    _close_handle(handle)


    def directory_identity_nofollow(
        root: Path, path: Path
    ) -> tuple[int, ...]:
        """逐级固定目录链并返回叶目录身份."""
        with _open_chain(
            root,
            path,
            final_access=_FILE_READ_ATTRIBUTES,
            final_expected="directory",
        ) as (candidate, handle):
            return _handle_identity(handle, candidate)


    def validate_directory_path_nofollow(path: Path) -> tuple[int, ...]:
        """从卷根开始逐级拒绝绝对目录路径中的 reparse point."""
        candidate = _absolute_path(path)
        anchor = Path(candidate.anchor)
        if not candidate.anchor:
            raise UnsafePathError(f"protected directory has no absolute anchor: {candidate}")
        return directory_identity_nofollow(anchor, candidate)


    def directory_exists_nofollow(root: Path, path: Path) -> bool:
        """安全区分目录缺失与 reparse/类型错误."""
        try:
            directory_identity_nofollow(root, path)
            return True
        except OSError as error:
            if _is_missing_windows_error(error):
                return False
            raise


    def list_directory_nofollow(root: Path, path: Path) -> list[tuple[str, str]]:
        """通过固定目录句柄枚举直接成员并拒绝 reparse point.

        返回 ``(name, kind)``，其中 kind 为 ``file``、``directory`` 或
        ``other``。调用方随后仍须用 no-follow 原语打开叶对象，以封闭枚举与
        使用之间的竞态窗口.
        """
        with _open_chain(
            root,
            path,
            final_access=_FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES,
            final_expected="directory",
        ) as (candidate, directory_handle):
            entries: list[tuple[str, str]] = []
            restart = True
            while True:
                buffer = ctypes.create_string_buffer(1 << 16)
                io_status = _IO_STATUS_BLOCK()
                status = _ntdll.NtQueryDirectoryFile(
                    directory_handle,
                    None,
                    None,
                    None,
                    ctypes.byref(io_status),
                    buffer,
                    len(buffer),
                    _NT_FILE_NAMES_INFORMATION_CLASS,
                    False,
                    None,
                    restart,
                )
                unsigned = _unsigned_ntstatus(status)
                if unsigned == _STATUS_NO_MORE_FILES:
                    break
                if status < 0:
                    _raise_nt_error("cannot enumerate protected directory", status, candidate)
                restart = False
                offset = 0
                used = int(io_status.Information)
                while offset + 12 <= used:
                    next_offset = wintypes.ULONG.from_buffer(buffer, offset).value
                    name_length = wintypes.ULONG.from_buffer(buffer, offset + 8).value
                    end = offset + 12 + name_length
                    if end > used:
                        raise UnsafePathError(
                            f"invalid directory enumeration result: {candidate}"
                        )
                    name = buffer.raw[offset + 12:end].decode("utf-16-le")
                    if name not in (".", ".."):
                        child_path = candidate / name
                        try:
                            child = _try_open_leaf(
                                directory_handle,
                                name,
                                child_path,
                                expected="any",
                            )
                        except OSError as error:
                            if _is_missing_windows_error(error):
                                child = None
                            else:
                                raise
                        if child is not None:
                            try:
                                info = _attribute_info(child, child_path)
                                kind = (
                                    "directory"
                                    if info.FileAttributes & _FILE_ATTRIBUTE_DIRECTORY
                                    else "file"
                                )
                                entries.append((name, kind))
                            finally:
                                _close_handle(child)
                    if not next_offset:
                        break
                    offset += int(next_offset)
            return entries


    def ensure_private_directory_nofollow(root: Path, path: Path) -> Path:
        """逐级创建或打开目录，并把新旧叶目录都收紧为私有 DACL."""
        root_absolute, candidate, parts = _relative_parts(root, path)
        handles: list[int] = []
        current = root_absolute
        with _open_chain(
            root_absolute,
            root_absolute,
            final_access=_FILE_READ_ATTRIBUTES,
            final_expected="directory",
        ) as (_, root_handle):
            parent_handle = root_handle
            try:
                for part in parts:
                    current /= part
                    handle = _try_open_leaf(
                        parent_handle,
                        part,
                        current,
                        access=(
                            _READ_CONTROL | _WRITE_DAC | _FILE_READ_ATTRIBUTES
                        ),
                        expected="directory",
                    )
                    if handle is None:
                        try:
                            handle = _open_relative_handle(
                                parent_handle,
                                part,
                                current,
                                access=(
                                    _READ_CONTROL
                                    | _WRITE_DAC
                                    | _FILE_READ_ATTRIBUTES
                                ),
                                creation=_CREATE_NEW,
                                expected="directory",
                                private_creation="directory",
                            )
                        except OSError as create_error:
                            code = (
                                getattr(create_error, "winerror", None)
                                or getattr(create_error, "errno", None)
                            )
                            if code not in (_ERROR_FILE_EXISTS, _ERROR_ALREADY_EXISTS):
                                raise
                            handle = _try_open_leaf(
                                parent_handle,
                                part,
                                current,
                                access=(
                                    _READ_CONTROL
                                    | _WRITE_DAC
                                    | _FILE_READ_ATTRIBUTES
                                ),
                                expected="directory",
                            )
                            if handle is None:
                                raise FileNotFoundError(current)
                    handles.append(handle)
                    _tighten_private_handle(handle, current, expected="directory")
                    parent_handle = handle
                return candidate
            finally:
                for handle in reversed(handles):
                    _close_handle(handle)


    @contextlib.contextmanager
    def open_private_append_file_nofollow(
        root: Path, path: Path
    ) -> Iterator[BinaryIO]:
        """安全创建/打开私有普通文件并在固定 append 句柄上读写.

        句柄不共享 DELETE，所以从读取去重状态到追加完成期间，路径不能被
        rename/replace。只授予 ``FILE_APPEND_DATA`` 而不授予普通写入权限，
        让每次底层 WriteFile 都定位到当时 EOF，保留并发 append 语义.
        """
        root_absolute, candidate, parts = _relative_parts(root, path)
        if not parts:
            raise UnsafePathError(f"protected file cannot be the root: {candidate}")
        with _open_chain(
            root_absolute,
            candidate.parent,
            final_access=_FILE_READ_ATTRIBUTES,
            final_expected="directory",
        ) as (_, parent_handle):
            handle = _open_or_create_regular_file_handle(
                parent_handle,
                candidate.name,
                candidate,
                access=(
                    _GENERIC_READ
                    | _FILE_APPEND_DATA
                    | _READ_CONTROL
                    | _WRITE_DAC
                    | _FILE_READ_ATTRIBUTES
                ),
            )
            file: BinaryIO | None = None
            try:
                _tighten_private_handle(handle, candidate, expected="file")
                flags = os.O_RDWR | os.O_APPEND | os.O_BINARY
                descriptor = msvcrt.open_osfhandle(handle, flags)
                handle = _INVALID_HANDLE_VALUE
                file = os.fdopen(descriptor, "a+b", buffering=0)
                yield file
                file.flush()
                os.fsync(file.fileno())
            finally:
                if file is not None:
                    file.close()
                else:
                    _close_handle(handle)


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
            existing = _try_open_leaf(
                parent_handle, candidate.name, candidate, expected="file"
            )
            if existing is not None:
                _close_handle(existing)
                if not replace:
                    raise FileExistsError(f"protected file already exists: {candidate}")

            temp_name = f".{candidate.name}.{os.getpid()}.{time.time_ns()}.tmp"
            temp_path = parent / temp_name
            temp_handle = _open_relative_handle(
                parent_handle,
                temp_name,
                temp_path,
                access=(
                    _GENERIC_WRITE
                    | _DELETE
                    | _READ_CONTROL
                    | _WRITE_DAC
                    | _FILE_READ_ATTRIBUTES
                ),
                creation=_CREATE_NEW,
                expected="file",
                share_delete=False,
                private_creation="file",
            )
            moved = False
            try:
                identity = _handle_identity(temp_handle, temp_path)
                _tighten_private_handle(temp_handle, temp_path, expected="file")
                _write_handle(temp_handle, temp_path, text.encode("utf-8"))
                _rename_handle(
                    temp_handle,
                    parent_handle,
                    candidate,
                    replace=replace,
                )
                moved = True
                replacement = _try_open_leaf(
                    parent_handle,
                    candidate.name,
                    candidate,
                    expected="file",
                    share_delete=True,
                )
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
            collision = _try_open_leaf(
                parent_handle, candidate.name, candidate, expected="any"
            )
            if collision is not None:
                _close_handle(collision)
                raise FileExistsError(f"protected directory already exists: {candidate}")

            temporary_name = (
                f".{candidate.name}.{os.getpid()}.{time.time_ns()}.tmp"
            )
            temporary = parent / temporary_name
            directory_handle: int | None = _open_relative_handle(
                parent_handle,
                temporary_name,
                temporary,
                access=(
                    _DELETE
                    | _READ_CONTROL
                    | _WRITE_DAC
                    | _FILE_READ_ATTRIBUTES
                ),
                creation=_CREATE_NEW,
                expected="directory",
                private_creation="directory",
            )
            moved = False
            try:
                identity = _handle_identity(directory_handle, temporary)
                _tighten_private_handle(
                    directory_handle, temporary, expected="directory"
                )
                child_path = temporary / filename
                child = _open_relative_handle(
                    directory_handle,
                    filename,
                    child_path,
                    access=(
                        _GENERIC_WRITE
                        | _DELETE
                        | _READ_CONTROL
                        | _WRITE_DAC
                        | _FILE_READ_ATTRIBUTES
                    ),
                    creation=_CREATE_NEW,
                    expected="file",
                    private_creation="file",
                )
                try:
                    _tighten_private_handle(child, child_path, expected="file")
                    _write_handle(child, child_path, text.encode("utf-8"))
                finally:
                    _close_handle(child)
                _rename_handle(
                    directory_handle,
                    parent_handle,
                    candidate,
                    replace=False,
                )
                moved = True
                published = _try_open_leaf(
                    parent_handle,
                    candidate.name,
                    candidate,
                    expected="directory",
                    share_delete=True,
                )
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
                        child = _try_open_leaf(
                            directory_handle,
                            filename,
                            temporary / filename,
                            access=_DELETE | _FILE_READ_ATTRIBUTES,
                            expected="file",
                            share_delete=False,
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
            source_absolute.parent,
            final_access=_FILE_READ_ATTRIBUTES,
            final_expected="directory",
        ) as (_, source_parent_handle):
            source_handle = _try_open_leaf(
                source_parent_handle,
                source_absolute.name,
                source_absolute,
                access=_DELETE | _FILE_READ_ATTRIBUTES,
                expected="any",
            )
            if source_handle is None:
                raise FileNotFoundError(source_absolute)
            try:
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
                    final_access=_FILE_TRAVERSE | _FILE_READ_ATTRIBUTES,
                    final_expected="directory",
                ) as (_, target_parent_handle):
                    collision = _try_open_leaf(
                        target_parent_handle,
                        target_absolute.name,
                        target_absolute,
                        expected="any",
                    )
                    if collision is not None:
                        _close_handle(collision)
                        raise FileExistsError(
                            f"rename target already exists: {target_absolute}"
                        )
                    _rename_handle(
                        source_handle,
                        target_parent_handle,
                        target_absolute,
                        replace=False,
                    )
                    moved = _try_open_leaf(
                        target_parent_handle,
                        target_absolute.name,
                        target_absolute,
                        expected=source_expected,
                        share_delete=True,
                    )
                    if moved is None:
                        raise UnsafePathError(
                            f"renamed path is missing after move: {target_absolute}"
                        )
                    try:
                        if _handle_identity(moved, target_absolute) != source_identity:
                            raise UnsafePathError(
                                "renamed path identity changed after move: "
                                f"{target_absolute}"
                            )
                    finally:
                        _close_handle(moved)
                    leftover = _try_open_leaf(
                        source_parent_handle,
                        source_absolute.name,
                        source_absolute,
                        expected="any",
                        share_delete=True,
                    )
                    if leftover is not None:
                        _close_handle(leftover)
                        raise UnsafePathError(
                            f"source path still exists after move: {source_absolute}"
                        )
            finally:
                _close_handle(source_handle)


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


    @contextlib.contextmanager
    def _open_posix_directory(root: Path, path: Path) -> Iterator[tuple[Path, int]]:
        root_absolute, candidate, parts = _relative_parts(root, path)
        dir_fd = os.open(
            root_absolute,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            for part in parts:
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


    def read_regular_file_with_identity_nofollow(
        root: Path, path: Path
    ) -> tuple[tuple[int, ...], bytes]:
        with _open_posix_parent(root, path) as (candidate, parent_fd):
            fd = _openat_nofollow(parent_fd, candidate.name, os.O_RDONLY)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise UnsafePathError(
                        f"task document is not a regular file: {candidate}"
                    )
                chunks: list[bytes] = []
                while True:
                    piece = os.read(fd, 1 << 20)
                    if not piece:
                        return (info.st_dev, info.st_ino), b"".join(chunks)
                    chunks.append(piece)
            finally:
                os.close(fd)


    def read_regular_file_if_exists_nofollow(
        root: Path, path: Path
    ) -> bytes | None:
        try:
            return read_regular_file_nofollow(root, path)
        except FileNotFoundError:
            return None


    @contextlib.contextmanager
    def open_regular_file_if_exists_nofollow(
        root: Path, path: Path
    ) -> Iterator[BinaryIO | None]:
        stack = contextlib.ExitStack()
        fd = -1
        try:
            candidate, parent_fd = stack.enter_context(_open_posix_parent(root, path))
            fd = _openat_nofollow(parent_fd, candidate.name, os.O_RDONLY)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise UnsafePathError(
                    f"task document is not a regular file: {candidate}"
                )
        except FileNotFoundError:
            if fd >= 0:
                os.close(fd)
            stack.close()
            yield None
            return
        file: BinaryIO | None = None
        try:
            file = os.fdopen(fd, "rb", buffering=0)
            fd = -1
            yield file
        finally:
            try:
                if file is not None:
                    file.close()
                elif fd >= 0:
                    os.close(fd)
            finally:
                stack.close()


    @contextlib.contextmanager
    def open_private_regular_file_if_exists_nofollow(
        root: Path, path: Path
    ) -> Iterator[BinaryIO | None]:
        with open_regular_file_if_exists_nofollow(root, path) as file:
            yield file


    def ensure_directory_path_nofollow(path: Path) -> Path:
        candidate = _absolute_path(path)
        anchor = Path(candidate.anchor)
        if not candidate.anchor:
            raise UnsafePathError(
                f"protected directory has no absolute anchor: {candidate}"
            )
        _, _, parts = _relative_parts(anchor, candidate)
        dir_fd = os.open(
            anchor,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            for part in parts:
                try:
                    next_fd = _openat_nofollow(
                        dir_fd,
                        part,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                    )
                except FileNotFoundError:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=dir_fd)
                    except FileExistsError:
                        pass
                    next_fd = _openat_nofollow(
                        dir_fd,
                        part,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                    )
                os.close(dir_fd)
                dir_fd = next_fd
            return candidate
        finally:
            os.close(dir_fd)


    def directory_identity_nofollow(
        root: Path, path: Path
    ) -> tuple[int, ...]:
        with _open_posix_directory(root, path) as (_, dir_fd):
            info = os.fstat(dir_fd)
            return info.st_dev, info.st_ino


    def validate_directory_path_nofollow(path: Path) -> tuple[int, ...]:
        candidate = _absolute_path(path)
        anchor = Path(candidate.anchor)
        if not candidate.anchor:
            raise UnsafePathError(f"protected directory has no absolute anchor: {candidate}")
        return directory_identity_nofollow(anchor, candidate)


    def directory_exists_nofollow(root: Path, path: Path) -> bool:
        try:
            directory_identity_nofollow(root, path)
            return True
        except FileNotFoundError:
            return False


    def list_directory_nofollow(root: Path, path: Path) -> list[tuple[str, str]]:
        with _open_posix_directory(root, path) as (_, dir_fd):
            entries: list[tuple[str, str]] = []
            with os.scandir(dir_fd) as iterator:
                for entry in iterator:
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode):
                        raise UnsafePathError(f"symlink is not allowed: {path / entry.name}")
                    if stat.S_ISREG(info.st_mode):
                        kind = "file"
                    elif stat.S_ISDIR(info.st_mode):
                        kind = "directory"
                    else:
                        kind = "other"
                    entries.append((entry.name, kind))
            return entries


    def ensure_private_directory_nofollow(root: Path, path: Path) -> Path:
        root_absolute, candidate, parts = _relative_parts(root, path)
        dir_fd = os.open(
            root_absolute,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            for part in parts:
                try:
                    next_fd = _openat_nofollow(
                        dir_fd,
                        part,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                    )
                except FileNotFoundError:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=dir_fd)
                    except FileExistsError:
                        # 另一合并进程可能刚创建同一状态目录；重新按 no-follow
                        # 打开并验证，而不是把正常首次并发误判为失败.
                        pass
                    next_fd = _openat_nofollow(
                        dir_fd,
                        part,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                    )
                os.fchmod(next_fd, 0o700)
                os.close(dir_fd)
                dir_fd = next_fd
            return candidate
        finally:
            os.close(dir_fd)


    @contextlib.contextmanager
    def open_private_append_file_nofollow(
        root: Path, path: Path
    ) -> Iterator[BinaryIO]:
        with _open_posix_parent(root, path) as (candidate, parent_fd):
            fd = _openat_nofollow(
                parent_fd,
                candidate.name,
                os.O_RDWR | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            file: BinaryIO | None = None
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise UnsafePathError(
                        f"task document is not a regular file: {candidate}"
                    )
                os.fchmod(fd, 0o600)
                file = os.fdopen(fd, "a+b", buffering=0)
                fd = -1
                yield file
                file.flush()
                os.fsync(file.fileno())
            finally:
                if file is not None:
                    file.close()
                elif fd >= 0:
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


def _tighten_private_handle(handle: int, path: Path, *, expected: str) -> None:
    """Windows: 直接收紧已固定对象句柄的 DACL."""
    if os.name != "nt":
        raise RuntimeError("private handle ACL is only available on Windows")
    with _private_security_descriptor(expected) as security_descriptor:  # type: ignore[name-defined]
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


def tighten_private_open_file_permissions(file: BinaryIO, path: Path) -> None:
    """收紧已打开普通文件, 避免再次按路径解析时被替换."""
    if os.name == "nt":
        handle = msvcrt.get_osfhandle(file.fileno())  # type: ignore[name-defined]
        _validate_handle_kind(handle, path, "file")  # type: ignore[name-defined]
        _tighten_private_handle(handle, path, expected="file")
        return
    info = os.fstat(file.fileno())
    if not stat.S_ISREG(info.st_mode):
        raise UnsafePathError(f"task document is not a regular file: {path}")
    os.fchmod(file.fileno(), 0o600)


def _tighten_private_permissions(path: Path, *, expected: str) -> None:
    if os.name != "nt":
        os.chmod(path, 0o700 if expected == "directory" else 0o600)
        return

    candidate = _absolute_path(path)
    anchor = Path(candidate.anchor)
    if not candidate.anchor:
        raise UnsafePathError(f"protected path has no absolute anchor: {candidate}")
    with _open_chain(  # type: ignore[name-defined]
        anchor,
        candidate,
        final_access=(
            _READ_CONTROL | _WRITE_DAC | _FILE_READ_ATTRIBUTES  # type: ignore[name-defined]
        ),
        final_expected=expected,
    ) as (_, handle):
        _tighten_private_handle(handle, candidate, expected=expected)


def tighten_private_file_permissions(path: Path) -> None:
    """把文件权限收紧为 POSIX 0600 或 Windows 当前用户独占 DACL.

    Windows ACL 会关闭继承，只给当前进程 token 的用户 SID 显式完全控制.
    任一步失败都抛错，调用方不得把失败当成权限已经收紧.
    """
    _tighten_private_permissions(path, expected="file")


def tighten_private_directory_permissions(path: Path) -> None:
    """把目录权限收紧为 POSIX 0700 或 Windows 当前用户独占 DACL."""
    _tighten_private_permissions(path, expected="directory")
