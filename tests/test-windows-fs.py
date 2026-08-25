#!/usr/bin/env python3

import importlib.machinery
import importlib.util
import contextlib
import ctypes
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

if os.name == "nt":
    from ctypes import wintypes


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = PROJECT_ROOT / "bin"
KANBAN = BIN_DIR / "kanban"
sys.path.insert(0, str(BIN_DIR))
import onevoke_fs
import onevoke_config
sys.path.pop(0)


def load_kanban_module():
    name = "kanban_windows_fs_under_test"
    loader = importlib.machinery.SourceFileLoader(name, str(KANBAN))
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None:
        raise RuntimeError("cannot load kanban test module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    sys.path.insert(0, str(BIN_DIR))
    try:
        loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


class PortableFileSystemTest(unittest.TestCase):
    def test_exclusive_lock_blocks_another_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / "lock"
            marker = root / "acquired"
            child = (
                "import sys; from pathlib import Path; "
                "sys.path.insert(0, sys.argv[1]); import onevoke_fs; "
                "lock=Path(sys.argv[2]); marker=Path(sys.argv[3]); "
                "file=lock.open('a+b'); "
                "guard=onevoke_fs.exclusive_file_lock(file); guard.__enter__(); "
                "marker.write_text('acquired', encoding='utf-8'); "
                "guard.__exit__(None, None, None); file.close()"
            )
            with lock_path.open("a+b") as lock:
                with onevoke_fs.exclusive_file_lock(lock):
                    process = subprocess.Popen(
                        [
                            sys.executable,
                            "-c",
                            child,
                            str(BIN_DIR),
                            str(lock_path),
                            str(marker),
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    time.sleep(0.25)
                    self.assertIsNone(process.poll(), "exclusive lock did not block")
                    self.assertFalse(marker.exists())
                stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(0, process.returncode, stderr.decode("utf-8", "replace"))
            self.assertEqual("acquired", marker.read_text(encoding="utf-8"))

    def test_private_permission_helpers_keep_current_user_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private_directory = root / "private"
            private_directory.mkdir()
            private_file = private_directory / "secret.json"
            private_file.write_text("secret", encoding="utf-8")

            onevoke_fs.tighten_private_directory_permissions(private_directory)
            onevoke_fs.tighten_private_file_permissions(private_file)

            private_file.write_text("updated", encoding="utf-8")
            self.assertEqual("updated", private_file.read_text(encoding="utf-8"))
            if os.name == "nt":
                for path in (private_directory, private_file):
                    acl = subprocess.run(
                        ["icacls.exe", str(path)],
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(0, acl.returncode, acl.stderr)
                    self.assertNotIn("(I)", acl.stdout)
                    self.assertEqual(1, acl.stdout.count("(F)"), acl.stdout)
            else:
                self.assertEqual(0o700, private_directory.stat().st_mode & 0o777)
                self.assertEqual(0o600, private_file.stat().st_mode & 0o777)

    @unittest.skipUnless(os.name == "posix", "POSIX no-follow backend only")
    def test_posix_atomic_create_is_private_and_rejects_symlink_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "board"
            state = root / "backlog"
            state.mkdir(parents=True)
            document = state / "20260823-private-task.md"

            onevoke_fs.write_text_atomic_nofollow(
                root, document, "private\n", replace=False
            )
            self.assertEqual(0o600, document.stat().st_mode & 0o777)

            root_link = base / "board-link"
            root_link.symlink_to(root, target_is_directory=True)
            with self.assertRaises(OSError):
                onevoke_fs.read_regular_file_nofollow(
                    root_link, root_link / "backlog" / document.name
                )

    @unittest.skipUnless(os.name == "posix", "POSIX no-follow backend only")
    def test_posix_neutral_append_and_inherited_directories_preserve_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git_directory = root / ".git"
            git_directory.mkdir()
            git_directory.chmod(0o755)
            info = git_directory / "info"

            previous_umask = os.umask(0o022)
            try:
                onevoke_fs.ensure_inherited_directory_path_nofollow(info)
            finally:
                os.umask(previous_umask)

            self.assertEqual(0o755, git_directory.stat().st_mode & 0o777)
            self.assertEqual(0o755, info.stat().st_mode & 0o777)
            exclude = info / "exclude"
            exclude.write_bytes(b"# local\n")
            exclude.chmod(0o644)

            for _ in range(2):
                with onevoke_fs.open_append_file_nofollow(
                    root, exclude
                ) as stream:
                    stream.seek(0)
                    existing = stream.read()
                    if b"/kanban/" not in existing.splitlines():
                        stream.write(b"/kanban/\n")

            self.assertEqual(b"# local\n/kanban/\n", exclude.read_bytes())
            self.assertEqual(0o644, exclude.stat().st_mode & 0o777)

    @unittest.skipUnless(os.name == "posix", "POSIX no-follow backend only")
    def test_posix_private_directory_create_is_strict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / "private"

            onevoke_fs.create_private_directory_nofollow(root, private)
            self.assertEqual(0o700, private.stat().st_mode & 0o777)
            with self.assertRaises(FileExistsError):
                onevoke_fs.create_private_directory_nofollow(root, private)

            missing = root / "missing"
            with self.assertRaises(FileNotFoundError):
                onevoke_fs.ensure_private_directory_nofollow(
                    root, missing, create=False
                )
            self.assertFalse(missing.exists())

            existing = root / "existing"
            existing.mkdir(mode=0o755)
            existing.chmod(0o755)
            onevoke_fs.ensure_private_directory_nofollow(
                root, existing, create=False
            )
            self.assertEqual(0o700, existing.stat().st_mode & 0o777)

    @unittest.skipUnless(os.name == "posix", "POSIX no-follow backend only")
    def test_posix_private_runtime_cleanup_never_follows_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            parent = base / "runtime-parent"
            parent.mkdir()
            victim = base / "victim.txt"
            victim.write_text("outside\n", encoding="utf-8")
            victim.chmod(0o644)

            with onevoke_fs.private_temporary_directory_nofollow(
                parent, prefix="codex-review."
            ) as runtime:
                (runtime / "planted-link").symlink_to(victim)
                locked = runtime / "locked"
                locked.mkdir()
                (locked / "inside.txt").write_text("inside\n", encoding="utf-8")
                locked.chmod(0o000)
                runtime.chmod(0o500)

            self.assertFalse(runtime.exists())
            self.assertEqual("outside\n", victim.read_text(encoding="utf-8"))
            self.assertEqual(0o644, victim.stat().st_mode & 0o777)


@unittest.skipUnless(os.name == "nt", "Windows handle backend only")
class WindowsSafeFileSystemTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.kanban = load_kanban_module()

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "board"
        self.outside = self.base / "outside"
        self.root.mkdir()
        self.outside.mkdir()
        for state in ("backlog", "todo", "working", "done", "archived", "trash"):
            (self.root / state).mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

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
        self.addCleanup(self.remove_junction, link)

    @staticmethod
    def remove_junction(link: Path) -> None:
        if os.path.lexists(link):
            os.rmdir(link)

    @staticmethod
    def set_junction_reparse(directory: Path, target: Path) -> None:
        """将已存在的空目录原地转为 mount-point reparse point."""
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.DeviceIoControl.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        kernel32.DeviceIoControl.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateFileW(
            str(directory),
            0x40000000,  # GENERIC_WRITE
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,  # OPEN_EXISTING
            0x02000000 | 0x00200000,
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            print_name = str(target.resolve())
            substitute_name = "\\??\\" + print_name
            substitute = substitute_name.encode("utf-16-le")
            printable = print_name.encode("utf-16-le")
            path_buffer = substitute + b"\x00\x00" + printable + b"\x00\x00"
            reparse = struct.pack(
                "<IHHHHHH",
                0xA0000003,  # IO_REPARSE_TAG_MOUNT_POINT
                8 + len(path_buffer),
                0,
                0,
                len(substitute),
                len(substitute) + 2,
                len(printable),
            ) + path_buffer
            returned = wintypes.DWORD()
            input_buffer = ctypes.create_string_buffer(reparse)
            if not kernel32.DeviceIoControl(
                handle,
                0x000900A4,  # FSCTL_SET_REPARSE_POINT
                input_buffer,
                len(reparse),
                None,
                0,
                ctypes.byref(returned),
                None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(handle)

    @staticmethod
    def clear_junction_reparse(directory: Path) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.DeviceIoControl.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        kernel32.DeviceIoControl.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateFileW(
            str(directory),
            0x40000000,
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,
            0x02000000 | 0x00200000,
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            reparse = struct.pack("<IHH", 0xA0000003, 0, 0)
            returned = wintypes.DWORD()
            input_buffer = ctypes.create_string_buffer(reparse)
            if not kernel32.DeviceIoControl(
                handle,
                0x000900AC,  # FSCTL_DELETE_REPARSE_POINT
                input_buffer,
                len(reparse),
                None,
                0,
                ctypes.byref(returned),
                None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(handle)

    def assert_private_acl(self, path: Path) -> None:
        output = self.acl_text(path)
        self.assertNotIn("(I)", output, output)
        self.assertEqual(1, output.count("(F)"), output)

    def acl_text(self, path: Path) -> str:
        acl = subprocess.run(
            ["icacls.exe", str(path)],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, acl.returncode, acl.stderr)
        return acl.stdout

    @staticmethod
    def append_exclude_pattern(exclude: Path) -> None:
        onevoke_fs.ensure_inherited_directory_path_nofollow(exclude.parent)
        anchor = Path(exclude.anchor)
        with onevoke_fs.open_append_file_nofollow(anchor, exclude) as stream:
            stream.seek(0)
            existing = stream.read()
            if b"/kanban/" not in existing.splitlines():
                if existing and not existing.endswith(b"\n"):
                    stream.write(b"\n")
                stream.write(b"/kanban/\n")

    def make_completed_large_entry(self, task_id: str):
        task = self.root / "working" / task_id
        task.mkdir()
        text = self.kanban.render_contract("Windows large task", "chore").replace(
            "- 结果:\n", "- 结果: completed\n", 1
        )
        spec = task / "spec.md"
        spec.write_text(text, encoding="utf-8")
        return (
            self.kanban.Entry(task_id, "working", task, spec, "large"),
            text,
            task / "report.md",
        )

    def test_read_write_and_move_use_verified_handles(self) -> None:
        source = self.root / "todo" / "20260823-safe-task.md"
        source.write_bytes(b"old\n")

        self.assertEqual(
            b"old\n",
            onevoke_fs.read_regular_file_nofollow(self.root, source),
        )
        self.assertEqual(
            b"old\n",
            onevoke_fs.read_regular_file_if_exists_nofollow(self.root, source),
        )
        self.assertIsNone(
            onevoke_fs.read_regular_file_if_exists_nofollow(
                self.root, self.root / "todo" / "missing.md"
            )
        )
        onevoke_fs.write_text_atomic_nofollow(self.root, source, "new\n")
        target = self.root / "working" / source.name
        onevoke_fs.rename_nofollow(self.root, source, target)

        self.assertFalse(os.path.lexists(source))
        self.assertEqual(b"new\n", onevoke_fs.read_regular_file_nofollow(self.root, target))
        self.assert_private_acl(target)

    def test_neutral_append_preserves_acl_and_deduplicates(self) -> None:
        git_directory = self.base / "neutral-project" / ".git"
        git_directory.mkdir(parents=True)
        git_acl = self.acl_text(git_directory)
        exclude = git_directory / "info" / "exclude"

        self.append_exclude_pattern(exclude)
        info_acl = self.acl_text(exclude.parent)
        exclude_acl = self.acl_text(exclude)
        self.append_exclude_pattern(exclude)

        self.assertEqual(b"/kanban/\n", exclude.read_bytes())
        self.assertEqual(git_acl, self.acl_text(git_directory))
        self.assertEqual(info_acl, self.acl_text(exclude.parent))
        self.assertEqual(exclude_acl, self.acl_text(exclude))
        self.assertIn("(I)", info_acl, info_acl)
        self.assertIn("(I)", exclude_acl, exclude_acl)

    def test_kanban_git_exclude_preserves_acl_and_deduplicates(self) -> None:
        project = self.base / "kanban-git-project"
        project.mkdir()
        subprocess.run(
            ["git", "init", "-q", str(project)],
            text=True,
            capture_output=True,
            check=True,
        )
        exclude = project / ".git" / "info" / "exclude"
        original_acl = self.acl_text(exclude)

        for _ in range(2):
            returned = self.kanban.add_git_exclude(project / "kanban")
            self.assertEqual(exclude, returned)

        self.assertEqual(
            1,
            exclude.read_text(encoding="utf-8").splitlines().count("/kanban/"),
        )
        self.assertEqual(original_acl, self.acl_text(exclude))

    def test_kanban_git_exclude_rejects_info_junction(self) -> None:
        project = self.base / "kanban-junction-project"
        project.mkdir()
        subprocess.run(
            ["git", "init", "-q", str(project)],
            text=True,
            capture_output=True,
            check=True,
        )
        info = project / ".git" / "info"
        info.rename(project / ".git" / "info-original")
        outside_exclude = self.outside / "exclude"
        outside_bytes = b"outside exclude must stay unchanged\n"
        outside_exclude.write_bytes(outside_bytes)
        outside_acl = self.acl_text(outside_exclude)
        self.make_junction(info, self.outside)

        with self.assertRaises(OSError):
            self.kanban.add_git_exclude(project / "kanban")

        self.assertEqual(outside_bytes, outside_exclude.read_bytes())
        self.assertEqual(outside_acl, self.acl_text(outside_exclude))
        self.assertEqual([outside_exclude], list(self.outside.iterdir()))

    def test_project_git_exclude_preserves_acl_and_deduplicates(self) -> None:
        project = self.base / "project-git-exclude"
        project.mkdir()
        subprocess.run(
            ["git", "init", "-q", str(project)],
            text=True,
            capture_output=True,
            check=True,
        )
        exclude = project / ".git" / "info" / "exclude"
        original_acl = self.acl_text(exclude)

        for _ in range(2):
            returned = onevoke_config.ensure_project_git_exclude(project)
            self.assertEqual(exclude, returned)

        self.assertEqual(
            1,
            exclude.read_text(encoding="utf-8").splitlines().count("/.onevoke/"),
        )
        self.assertEqual(original_acl, self.acl_text(exclude))

    def test_project_git_exclude_rejects_info_junction(self) -> None:
        project = self.base / "project-exclude-junction"
        project.mkdir()
        subprocess.run(
            ["git", "init", "-q", str(project)],
            text=True,
            capture_output=True,
            check=True,
        )
        info = project / ".git" / "info"
        info.rename(project / ".git" / "info-original")
        outside_exclude = self.outside / "exclude"
        outside_bytes = b"outside exclude must stay unchanged\n"
        outside_exclude.write_bytes(outside_bytes)
        outside_acl = self.acl_text(outside_exclude)
        self.make_junction(info, self.outside)

        with self.assertRaises(onevoke_config.ConfigError):
            onevoke_config.ensure_project_git_exclude(project)

        self.assertEqual(outside_bytes, outside_exclude.read_bytes())
        self.assertEqual(outside_acl, self.acl_text(outside_exclude))
        self.assertEqual([outside_exclude], list(self.outside.iterdir()))

    def test_install_paths_rejects_onevoke_junction(self) -> None:
        project = self.base / "project-onevoke-junction"
        project.mkdir()
        subprocess.run(
            ["git", "init", "-q", str(project)],
            text=True,
            capture_output=True,
            check=True,
        )
        payload = self.base / "payload"
        (payload / "bin").mkdir(parents=True)
        (payload / "bin" / "onevoke").write_text("entry\n", encoding="utf-8")
        self.make_junction(project / ".onevoke", payload)

        with self.assertRaises(onevoke_config.ConfigError):
            onevoke_config.install_paths(
                entry=project / ".onevoke" / "bin" / "onevoke"
            )

    def test_neutral_append_rejects_static_info_junction(self) -> None:
        git_directory = self.base / "junction-project" / ".git"
        git_directory.mkdir(parents=True)
        info = git_directory / "info"
        outside_exclude = self.outside / "exclude"
        outside_bytes = b"outside exclude must stay unchanged\n"
        outside_exclude.write_bytes(outside_bytes)
        outside_acl = self.acl_text(outside_exclude)
        self.make_junction(info, self.outside)

        with self.assertRaises(OSError):
            self.append_exclude_pattern(info / "exclude")

        self.assertEqual(outside_bytes, outside_exclude.read_bytes())
        self.assertEqual(outside_acl, self.acl_text(outside_exclude))
        self.assertEqual([outside_exclude], list(self.outside.iterdir()))

    def test_neutral_append_fails_closed_when_info_gets_reparse_tag(self) -> None:
        git_directory = self.base / "fsctl-project" / ".git"
        info = git_directory / "info"
        info.mkdir(parents=True)
        exclude = info / "exclude"
        outside_exclude = self.outside / "exclude"
        outside_bytes = b"outside exclude must stay unchanged\n"
        outside_exclude.write_bytes(outside_bytes)
        outside_acl = self.acl_text(outside_exclude)
        original_try_open = onevoke_fs._try_open_leaf
        info_opens = 0
        swapped = False

        def open_then_swap(parent_handle, name, path, **kwargs):
            nonlocal info_opens, swapped
            handle = original_try_open(parent_handle, name, path, **kwargs)
            if path == info:
                info_opens += 1
                if info_opens == 2:
                    self.set_junction_reparse(info, self.outside)
                    swapped = True
            return handle

        try:
            with mock.patch.object(
                onevoke_fs, "_try_open_leaf", side_effect=open_then_swap
            ):
                with self.assertRaises(OSError):
                    self.append_exclude_pattern(exclude)

            self.assertTrue(swapped, "the info FSCTL race hook did not run")
            self.assertTrue(onevoke_fs.is_reparse_point(info))
            self.assertEqual(outside_bytes, outside_exclude.read_bytes())
            self.assertEqual(outside_acl, self.acl_text(outside_exclude))
            self.assertEqual([outside_exclude], list(self.outside.iterdir()))
        finally:
            if onevoke_fs.is_reparse_point(info):
                self.clear_junction_reparse(info)

        self.assertFalse(exclude.exists())
        self.assertEqual([], list(info.iterdir()))

    def test_neutral_append_pins_leaf_and_preserves_existing_acl(self) -> None:
        info = self.base / "leaf-project" / ".git" / "info"
        info.mkdir(parents=True)
        exclude = info / "exclude"
        original_bytes = b"# local exclude"
        exclude.write_bytes(original_bytes)
        original_acl = self.acl_text(exclude)
        replacement = self.outside / "replacement"
        replacement_bytes = b"replacement must stay outside\n"
        replacement.write_bytes(replacement_bytes)
        original_open = onevoke_fs._open_or_create_regular_file_handle
        replacement_errors: list[OSError] = []

        def open_then_replace(*args, **kwargs):
            handle = original_open(*args, **kwargs)
            try:
                os.replace(replacement, exclude)
            except OSError as error:
                replacement_errors.append(error)
            else:
                self.fail("exclude replacement succeeded while leaf was pinned")
            return handle

        with mock.patch.object(
            onevoke_fs,
            "_open_or_create_regular_file_handle",
            side_effect=open_then_replace,
        ):
            self.append_exclude_pattern(exclude)

        self.assertEqual(1, len(replacement_errors))
        self.assertEqual(
            original_bytes + b"\n/kanban/\n", exclude.read_bytes()
        )
        self.assertEqual(original_acl, self.acl_text(exclude))
        self.assertEqual(replacement_bytes, replacement.read_bytes())

    def test_private_directory_create_is_atomic_strict_and_cleanup_safe(self) -> None:
        parent = self.base / "strict-private-parent"
        parent.mkdir()
        private = parent / "private"
        original_open = onevoke_fs._open_relative_handle
        observed_creation_acl: list[Path] = []

        def inspect_creation(parent_handle, name, path, **kwargs):
            handle = original_open(parent_handle, name, path, **kwargs)
            if path == private:
                self.assertEqual("directory", kwargs.get("private_creation"))
                self.assertEqual(onevoke_fs._CREATE_NEW, kwargs.get("creation"))
                self.assert_private_acl(path)
                observed_creation_acl.append(path)
            return handle

        with mock.patch.object(
            onevoke_fs, "_open_relative_handle", side_effect=inspect_creation
        ):
            onevoke_fs.create_private_directory_nofollow(parent, private)

        self.assertEqual([private], observed_creation_acl)
        self.assert_private_acl(private)
        with mock.patch.object(
            onevoke_fs,
            "_tighten_private_handle",
            side_effect=AssertionError("collision unexpectedly reused directory"),
        ):
            with self.assertRaises(FileExistsError):
                onevoke_fs.create_private_directory_nofollow(parent, private)

        failed = parent / "failed"
        original_tighten = onevoke_fs._tighten_private_handle

        def fail_hardening(handle, path, *, expected):
            if path == failed:
                raise OSError("forced directory ACL failure")
            return original_tighten(handle, path, expected=expected)

        with mock.patch.object(
            onevoke_fs, "_tighten_private_handle", side_effect=fail_hardening
        ):
            with self.assertRaisesRegex(OSError, "forced directory ACL failure"):
                onevoke_fs.create_private_directory_nofollow(parent, failed)

        self.assertFalse(failed.exists())
        moved = self.base / "strict-private-parent-moved"
        parent.rename(moved)
        moved.rename(parent)

    def test_private_directory_ensure_without_create_never_creates(self) -> None:
        parent = self.base / "ensure-existing-parent"
        parent.mkdir()
        missing = parent / "missing"

        with self.assertRaises(FileNotFoundError):
            onevoke_fs.ensure_private_directory_nofollow(
                parent, missing, create=False
            )
        self.assertFalse(missing.exists())

        existing = parent / "existing"
        existing.mkdir()
        self.assertIn("(I)", self.acl_text(existing))
        onevoke_fs.ensure_private_directory_nofollow(
            parent, existing, create=False
        )
        self.assert_private_acl(existing)

    def test_kanban_init_and_new_make_private_board_entries(self) -> None:
        board = self.base / "private-board"
        env = os.environ.copy()
        env.update({
            "KANBAN_DIR": str(board),
            "ONEVOKE_LANG": "en",
            "ONEVOKE_CONFIG": str(self.base / "missing-config.json"),
        })

        for arguments in (
            ["init"],
            ["init"],
            ["new", "chore", "private-small", "Private small"],
            ["new", "--large", "chore", "private-large", "Private large"],
        ):
            result = subprocess.run(
                [sys.executable, str(KANBAN), *arguments],
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)

        small = next((board / "backlog").glob("*-private-small-task.md"))
        large = next((board / "backlog").glob("*-private-large-task"))
        for path in (
            board,
            *(board / state for state in ("backlog", "todo", "working", "done", "archived", "trash")),
            small,
            large,
            large / "spec.md",
        ):
            self.assert_private_acl(path)

    def test_kanban_init_board_directories_are_private_when_create_returns(self) -> None:
        project = self.base / "creation-time-board-project"
        project.mkdir()
        board = project / "kanban"
        expected = {board, *(board / state for state in self.kanban.STATES)}
        observed: set[Path] = set()
        original_open = onevoke_fs._open_relative_handle

        def inspect_created_acl(parent_handle, name, path, **kwargs):
            handle = original_open(parent_handle, name, path, **kwargs)
            if path in expected and kwargs.get("private_creation") == "directory":
                self.assertEqual(onevoke_fs._CREATE_NEW, kwargs.get("creation"))
                self.assert_private_acl(path)
                observed.add(path)
            return handle

        with mock.patch.object(
            onevoke_fs, "_open_relative_handle", side_effect=inspect_created_acl
        ), mock.patch("builtins.print"):
            self.kanban.command_init(Namespace(project=str(project)))

        self.assertEqual(expected, observed)

    def test_kanban_init_fails_closed_on_board_create_collision(self) -> None:
        project = self.base / "board-create-race-project"
        project.mkdir()
        board = project / "kanban"
        original_open = onevoke_fs._open_relative_handle
        injected = False

        def create_collision(parent_handle, name, path, **kwargs):
            nonlocal injected
            if (
                not injected
                and path == board
                and kwargs.get("creation") == onevoke_fs._CREATE_NEW
                and kwargs.get("private_creation") == "directory"
            ):
                board.mkdir()
                injected = True
            return original_open(parent_handle, name, path, **kwargs)

        with mock.patch.object(
            onevoke_fs, "_open_relative_handle", side_effect=create_collision
        ), mock.patch("builtins.print"):
            with self.assertRaises(FileExistsError):
                self.kanban.command_init(Namespace(project=str(project)))

        self.assertTrue(injected, "board create collision hook did not run")
        self.assertTrue(board.is_dir())
        self.assertIn("(I)", self.acl_text(board))
        self.assertEqual([], list(board.iterdir()))

    def test_new_rejects_backlog_junction_swapped_after_board_scan(self) -> None:
        for large in (False, True):
            with self.subTest(large=large):
                board_root = self.base / f"race-board-{large}"
                outside = self.base / f"race-outside-{large}"
                outside.mkdir()
                for state in self.kanban.STATES:
                    (board_root / state).mkdir(parents=True, exist_ok=True)

                original_load_board = self.kanban.load_board

                def swap_backlog(root: Path):
                    board = original_load_board(root)
                    backlog = root / "backlog"
                    backlog.rename(root / "backlog-before-swap")
                    self.make_junction(backlog, outside)
                    return board

                args = Namespace(
                    type="chore",
                    slug=f"race-{'large' if large else 'small'}",
                    title=["Race", "must", "stay", "inside"],
                    large=large,
                )
                with mock.patch.object(
                    self.kanban, "load_board", side_effect=swap_backlog
                ):
                    with self.assertRaises(self.kanban.KanbanError) as raised:
                        self.kanban.command_new(args, board_root)

                self.assertIn("reparse point", str(raised.exception))
                self.assertEqual([], list(outside.iterdir()))

    def test_atomic_write_rejects_junction_component_and_preserves_outside_file(self) -> None:
        outside_document = self.outside / "spec.md"
        outside_document.write_text("do not touch\n", encoding="utf-8")
        junction = self.root / "todo" / "20260823-junction-task"
        self.make_junction(junction, self.outside)

        with self.assertRaises(onevoke_fs.UnsafePathError):
            onevoke_fs.write_text_atomic_nofollow(
                self.root, junction / "spec.md", "overwritten\n"
            )

        self.assertEqual("do not touch\n", outside_document.read_text(encoding="utf-8"))

    def test_atomic_write_stays_on_pinned_parent_after_in_place_junction_swap(self) -> None:
        parent = self.root / "todo"
        document = parent / "20260823-pinned-parent-task.md"
        outside_document = self.outside / document.name
        original_open = onevoke_fs._open_relative_handle
        swapped = False

        def open_and_swap(parent_handle, name, path, **kwargs):
            nonlocal swapped
            handle = original_open(parent_handle, name, path, **kwargs)
            if not swapped and path == parent:
                self.set_junction_reparse(parent, self.outside)
                swapped = True
            return handle

        try:
            with mock.patch.object(
                onevoke_fs, "_open_relative_handle", side_effect=open_and_swap
            ):
                with self.assertRaises(onevoke_fs.UnsafePathError):
                    onevoke_fs.write_text_atomic_nofollow(
                        self.root, document, "pinned parent\n", replace=False
                    )

            self.assertTrue(swapped, "the FSCTL race hook did not run")
            self.assertTrue(onevoke_fs.is_reparse_point(parent))
            self.assertFalse(outside_document.exists())
        finally:
            if onevoke_fs.is_reparse_point(parent):
                self.clear_junction_reparse(parent)

        self.assertFalse(document.exists())
        self.assertEqual([], list(parent.iterdir()))

    def test_config_rejects_parent_junction_without_touching_outside(self) -> None:
        outside_config = self.outside / "config.json"
        outside_payload = onevoke_config.default_config()
        outside_payload["welcome_complete"] = True
        outside_payload["language"] = "en"
        outside_bytes = (
            json.dumps(outside_payload, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        outside_config.write_bytes(outside_bytes)
        outside_acl = self.acl_text(outside_config)
        parent = self.base / "config-parent"
        self.make_junction(parent, self.outside)
        configured = parent / "config.json"

        with mock.patch.dict(
            os.environ, {"ONEVOKE_CONFIG": str(configured)}, clear=False
        ):
            self.assertIsNone(onevoke_config.configured_language())
            with self.assertRaises(onevoke_config.ConfigError):
                onevoke_config.load_config()
            with self.assertRaises(OSError):
                onevoke_config.save_config(onevoke_config.default_config())

        self.assertEqual(outside_bytes, outside_config.read_bytes())
        self.assertEqual(outside_acl, self.acl_text(outside_config))
        self.assertEqual([outside_config], list(self.outside.iterdir()))

    def test_config_save_fails_closed_when_parent_gets_reparse_tag(self) -> None:
        parent = self.base / "config-fsctl-parent"
        parent.mkdir()
        configured = parent / "config.json"
        sentinel = self.outside / "sentinel.txt"
        sentinel_bytes = b"outside must remain unchanged\n"
        sentinel.write_bytes(sentinel_bytes)
        original_open = onevoke_fs._open_relative_handle
        parent_opens = 0
        swapped = False

        def open_then_swap(parent_handle, name, path, **kwargs):
            nonlocal parent_opens, swapped
            handle = original_open(parent_handle, name, path, **kwargs)
            if path == parent:
                parent_opens += 1
                if parent_opens == 2:
                    self.set_junction_reparse(parent, self.outside)
                    swapped = True
            return handle

        try:
            with mock.patch.dict(
                os.environ, {"ONEVOKE_CONFIG": str(configured)}, clear=False
            ), mock.patch.object(
                onevoke_fs, "_open_relative_handle", side_effect=open_then_swap
            ):
                with self.assertRaises(OSError):
                    onevoke_config.save_config(onevoke_config.default_config())

            self.assertTrue(swapped, "the config FSCTL race hook did not run")
            self.assertTrue(onevoke_fs.is_reparse_point(parent))
            self.assertEqual(sentinel_bytes, sentinel.read_bytes())
            self.assertEqual([sentinel], list(self.outside.iterdir()))
        finally:
            if onevoke_fs.is_reparse_point(parent):
                self.clear_junction_reparse(parent)

        self.assertEqual([], list(parent.iterdir()))

    def test_config_dangling_leaf_junction_is_not_treated_as_missing(self) -> None:
        parent = self.base / "dangling-config-parent"
        parent.mkdir()
        missing_target = self.base / "missing-config-target"
        configured = parent / "config.json"
        self.make_junction(configured, missing_target)

        with mock.patch.dict(
            os.environ, {"ONEVOKE_CONFIG": str(configured)}, clear=False
        ):
            with self.assertRaises(onevoke_config.ConfigError):
                onevoke_config.load_config(missing_ok=True)
            with self.assertRaises(OSError):
                onevoke_config.save_config(onevoke_config.default_config())

        self.assertFalse(missing_target.exists())

    def test_config_validation_pins_file_against_replacement(self) -> None:
        parent = self.base / "pinned-config-parent"
        parent.mkdir()
        configured = parent / "config.json"
        original_payload = onevoke_config.default_config()
        original_payload["welcome_complete"] = True
        original_payload["language"] = "en"
        original_bytes = json.dumps(original_payload).encode("utf-8")
        configured.write_bytes(original_bytes)
        replacement = self.outside / "replacement.json"
        replacement_bytes = b'{"attacker": true}\n'
        replacement.write_bytes(replacement_bytes)
        original_validate = onevoke_config.validate_config
        replacement_error: list[OSError] = []
        overwrite_error: list[OSError] = []

        def validate_while_replacing(raw):
            validated = original_validate(raw)
            try:
                os.replace(replacement, configured)
            except OSError as error:
                replacement_error.append(error)
            else:
                self.fail("config replacement succeeded while validation handle was pinned")
            try:
                configured.write_bytes(b'{"overwritten": true}\n')
            except OSError as error:
                overwrite_error.append(error)
            else:
                self.fail("config overwrite succeeded while validation handle was pinned")
            return validated

        with mock.patch.dict(
            os.environ, {"ONEVOKE_CONFIG": str(configured)}, clear=False
        ), mock.patch.object(
            onevoke_config, "validate_config", side_effect=validate_while_replacing
        ):
            loaded = onevoke_config.load_config()

        self.assertEqual("en", loaded["language"])
        self.assertEqual(1, len(replacement_error))
        self.assertEqual(1, len(overwrite_error))
        self.assertEqual(original_bytes, configured.read_bytes())
        self.assertEqual(replacement_bytes, replacement.read_bytes())
        self.assert_private_acl(configured)

    def test_invalid_config_does_not_migrate_inherited_acl(self) -> None:
        parent = self.base / "invalid-config-parent"
        parent.mkdir()
        configured = parent / "config.json"
        invalid = onevoke_config.default_config()
        invalid["schema_version"] = 999
        configured.write_text(json.dumps(invalid), encoding="utf-8")
        before = self.acl_text(configured)
        self.assertIn("(I)", before, before)

        with mock.patch.dict(
            os.environ, {"ONEVOKE_CONFIG": str(configured)}, clear=False
        ):
            with self.assertRaises(onevoke_config.ConfigError):
                onevoke_config.load_config()

        after = self.acl_text(configured)
        self.assertEqual(before, after)

    def test_config_save_creates_private_children_without_tightening_ancestor(self) -> None:
        ancestor = self.base / "existing-config-ancestor"
        ancestor.mkdir()
        before = self.acl_text(ancestor)
        self.assertIn("(I)", before, before)
        configured = ancestor / "new-one" / "new-two" / "config.json"
        original_write = onevoke_fs._write_handle
        checked_private_before_write: list[Path] = []

        def write_after_acl(handle, path, data):
            self.assert_private_acl(path)
            checked_private_before_write.append(path)
            return original_write(handle, path, data)

        with mock.patch.dict(
            os.environ, {"ONEVOKE_CONFIG": str(configured)}, clear=False
        ), mock.patch.object(
            onevoke_fs, "_write_handle", side_effect=write_after_acl
        ):
            onevoke_config.save_config(onevoke_config.default_config())
            loaded = onevoke_config.load_config()

        self.assertFalse(loaded["welcome_complete"])
        self.assertEqual(before, self.acl_text(ancestor))
        self.assert_private_acl(ancestor / "new-one")
        self.assert_private_acl(ancestor / "new-one" / "new-two")
        self.assert_private_acl(configured)
        self.assertEqual(1, len(checked_private_before_write))

    def test_private_creations_apply_acl_before_open_returns(self) -> None:
        original_open = onevoke_fs._open_relative_handle
        observed: list[tuple[Path, str]] = []

        def inspect_created_acl(parent_handle, name, path, **kwargs):
            handle = original_open(parent_handle, name, path, **kwargs)
            kind = kwargs.get("private_creation")
            if kind is not None:
                self.assertEqual(onevoke_fs._CREATE_NEW, kwargs.get("creation"))
                self.assertEqual(kind, kwargs.get("expected"))
                # 此 hook 位于 NtCreateFile 返回后、调用方后验 tighten 前.
                self.assert_private_acl(path)
                observed.append((path, kind))
            return handle

        private_directory = self.root / "private-one" / "private-two"
        append_file = self.root / "todo" / "append.log"
        atomic_file = self.root / "todo" / "atomic.md"
        published = self.root / "backlog" / "20260824-private-create-task"
        config_ancestor = self.base / "creation-config-ancestor"
        config_ancestor.mkdir()
        configured = config_ancestor / "new-one" / "config.json"

        with mock.patch.object(
            onevoke_fs, "_open_relative_handle", side_effect=inspect_created_acl
        ):
            onevoke_fs.ensure_private_directory_nofollow(
                self.root, private_directory
            )
            with onevoke_fs.open_private_append_file_nofollow(
                self.root, append_file
            ) as stream:
                stream.write(b"append\n")
            onevoke_fs.write_text_atomic_nofollow(
                self.root, atomic_file, "atomic\n", replace=False
            )
            onevoke_fs.create_directory_with_text_file_nofollow(
                self.root, published, "spec.md", "private\n"
            )
            with mock.patch.dict(
                os.environ, {"ONEVOKE_CONFIG": str(configured)}, clear=False
            ):
                onevoke_config.save_config(onevoke_config.default_config())

        observed_kinds = {kind for _, kind in observed}
        self.assertEqual({"file", "directory"}, observed_kinds)
        self.assertIn((private_directory.parent, "directory"), observed)
        self.assertIn((private_directory, "directory"), observed)
        self.assertIn((append_file, "file"), observed)
        self.assertIn((config_ancestor / "new-one", "directory"), observed)
        self.assertTrue(
            any(
                path.parent == atomic_file.parent
                and path.name.startswith(f".{atomic_file.name}.")
                and path.name.endswith(".tmp")
                and kind == "file"
                for path, kind in observed
            )
        )
        self.assertTrue(
            any(
                path.parent == published.parent
                and path.name.startswith(f".{published.name}.")
                and path.name.endswith(".tmp")
                and kind == "directory"
                for path, kind in observed
            )
        )
        self.assertTrue(
            any(
                path.name == "spec.md"
                and path.parent.name.startswith(f".{published.name}.")
                and kind == "file"
                for path, kind in observed
            )
        )
        self.assertTrue(
            any(
                path.parent == configured.parent
                and path.name.startswith(f".{configured.name}.")
                and path.name.endswith(".tmp")
                and kind == "file"
                for path, kind in observed
            )
        )

        with self.assertRaisesRegex(ValueError, "requires CREATE_NEW"):
            onevoke_fs._open_relative_handle(
                0,
                "unused",
                self.base / "unused",
                access=onevoke_fs._FILE_READ_ATTRIBUTES,
                creation=onevoke_fs._OPEN_EXISTING,
                expected="file",
                private_creation="file",
            )

    def test_config_load_hardens_the_same_open_stream(self) -> None:
        parent = self.base / "same-handle-config-parent"
        parent.mkdir()
        configured = parent / "config.json"
        configured.write_text(
            json.dumps(onevoke_config.default_config()), encoding="utf-8"
        )
        original_open = onevoke_config.open_private_regular_file_if_exists_nofollow
        original_tighten = onevoke_config.tighten_private_open_file_permissions
        yielded: list[object] = []
        tightened: list[object] = []

        @contextlib.contextmanager
        def capture_stream(root, path):
            with original_open(root, path) as stream:
                yielded.append(stream)
                yield stream

        def assert_same_open_stream(stream, path):
            self.assertIs(stream, yielded[-1])
            self.assertFalse(stream.closed)
            tightened.append(stream)
            return original_tighten(stream, path)

        with mock.patch.dict(
            os.environ, {"ONEVOKE_CONFIG": str(configured)}, clear=False
        ), mock.patch.object(
            onevoke_config,
            "open_private_regular_file_if_exists_nofollow",
            side_effect=capture_stream,
        ), mock.patch.object(
            onevoke_config,
            "tighten_private_open_file_permissions",
            side_effect=assert_same_open_stream,
        ):
            loaded = onevoke_config.load_config()

        self.assertFalse(loaded["welcome_complete"])
        self.assertEqual(1, len(yielded))
        self.assertEqual(yielded, tightened)
        self.assertTrue(yielded[0].closed)

    def test_config_directory_acl_failure_removes_child_and_releases_handle(self) -> None:
        ancestor = self.base / "acl-failure-ancestor"
        ancestor.mkdir()
        new_directory = ancestor / "new-one"
        configured = new_directory / "nested" / "config.json"
        original_tighten = onevoke_fs._tighten_private_handle
        failures = 0

        def fail_new_directory(handle, path, *, expected):
            nonlocal failures
            if path == new_directory:
                failures += 1
                raise OSError("forced directory ACL failure")
            return original_tighten(handle, path, expected=expected)

        with mock.patch.dict(
            os.environ, {"ONEVOKE_CONFIG": str(configured)}, clear=False
        ), mock.patch.object(
            onevoke_fs, "_tighten_private_handle", side_effect=fail_new_directory
        ):
            with self.assertRaisesRegex(OSError, "forced directory ACL failure"):
                onevoke_config.save_config(onevoke_config.default_config())

        self.assertEqual(1, failures)
        self.assertFalse(new_directory.exists())
        moved_ancestor = self.base / "acl-failure-ancestor-moved"
        ancestor.rename(moved_ancestor)
        moved_ancestor.rename(ancestor)

        with mock.patch.dict(
            os.environ, {"ONEVOKE_CONFIG": str(configured)}, clear=False
        ):
            onevoke_config.save_config(onevoke_config.default_config())

        self.assert_private_acl(new_directory)
        self.assert_private_acl(new_directory / "nested")
        self.assert_private_acl(configured)

    def test_config_temp_acl_failure_writes_and_publishes_nothing(self) -> None:
        parent = self.base / "temp-acl-failure-parent"
        parent.mkdir()
        configured = parent / "config.json"
        original_bytes = b'{"existing": true}\n'
        configured.write_bytes(original_bytes)
        original_acl = self.acl_text(configured)
        original_tighten = onevoke_fs._tighten_private_handle
        original_write = onevoke_fs._write_handle
        writes: list[Path] = []
        failures = 0

        def fail_temp_file(handle, path, *, expected):
            nonlocal failures
            if path.parent == parent and path.name.startswith(".config.json."):
                failures += 1
                raise OSError("forced temp ACL failure")
            return original_tighten(handle, path, expected=expected)

        def record_write(handle, path, data):
            writes.append(path)
            return original_write(handle, path, data)

        with mock.patch.dict(
            os.environ, {"ONEVOKE_CONFIG": str(configured)}, clear=False
        ), mock.patch.object(
            onevoke_fs, "_tighten_private_handle", side_effect=fail_temp_file
        ), mock.patch.object(
            onevoke_fs, "_write_handle", side_effect=record_write
        ):
            with self.assertRaisesRegex(OSError, "forced temp ACL failure"):
                onevoke_config.save_config(onevoke_config.default_config())

        self.assertEqual(1, failures)
        self.assertEqual([], writes)
        self.assertEqual(original_bytes, configured.read_bytes())
        self.assertEqual(original_acl, self.acl_text(configured))
        self.assertEqual([configured], list(parent.iterdir()))

    def test_config_relative_override_uses_current_directory_boundary(self) -> None:
        relative = Path("relative-config") / "nested" / "config.json"
        configured = self.base / relative
        previous_cwd = Path.cwd()
        try:
            os.chdir(self.base)
            with mock.patch.dict(
                os.environ, {"ONEVOKE_CONFIG": str(relative)}, clear=False
            ):
                onevoke_config.save_config(onevoke_config.default_config())
                loaded = onevoke_config.load_config()
        finally:
            os.chdir(previous_cwd)

        self.assertFalse(loaded["welcome_complete"])
        self.assert_private_acl(configured)

    def test_large_report_validation_uses_verified_handle_reader(self) -> None:
        entry, text, report = self.make_completed_large_entry(
            "20260823-report-helper-task"
        )

        with mock.patch.object(
            self.kanban,
            "read_regular_file_nofollow",
            return_value=b"verified report\n",
        ) as reader:
            self.kanban.validate_target(entry, "done", text)

        reader.assert_called_once_with(self.root, report)

        with mock.patch.object(
            self.kanban,
            "read_regular_file_nofollow",
            side_effect=onevoke_fs.UnsafePathError("reparse point is not allowed"),
        ):
            with self.assertRaises(self.kanban.KanbanError) as raised:
                self.kanban.validate_target(entry, "done", text)
        self.assertIn("reparse point", str(raised.exception))

    def test_large_report_validation_handles_missing_empty_and_invalid_utf8(self) -> None:
        entry, text, report = self.make_completed_large_entry(
            "20260823-report-content-task"
        )

        with self.assertRaises(self.kanban.KanbanError) as missing:
            self.kanban.validate_target(entry, "done", text)
        self.assertIn("report.md", str(missing.exception))

        report.write_bytes(b" \r\n\t")
        with self.assertRaises(self.kanban.KanbanError) as empty:
            self.kanban.validate_target(entry, "done", text)
        self.assertIn("report.md", str(empty.exception))

        report.write_bytes(b"\xff\xfe\x80")
        with self.assertRaises(self.kanban.KanbanError) as invalid_utf8:
            self.kanban.validate_target(entry, "done", text)
        self.assertIn("UTF-8", str(invalid_utf8.exception))

    def test_large_report_file_symlink_is_rejected_without_touching_target(self) -> None:
        entry, _, report = self.make_completed_large_entry(
            "20260823-report-symlink-task"
        )
        outside_report = self.outside / "report.md"
        outside_report.write_bytes(b"outside report must stay unchanged\n")
        try:
            report.symlink_to(outside_report)
        except (NotImplementedError, OSError) as error:
            self.skipTest(f"cannot create a Windows file symlink: {error}")

        with self.assertRaises(self.kanban.KanbanError) as raised:
            self.kanban.move_entry(entry, self.root, "done")

        self.assertIn("reparse point", str(raised.exception))
        self.assertEqual(
            b"outside report must stay unchanged\n", outside_report.read_bytes()
        )
        self.assertTrue(entry.path.exists())
        self.assertFalse((self.root / "done" / entry.task_id).exists())

    def test_move_rejects_junction_entry_and_preserves_outside_directory(self) -> None:
        outside_document = self.outside / "spec.md"
        outside_document.write_text("outside\n", encoding="utf-8")
        source = self.root / "todo" / "20260823-junction-task"
        self.make_junction(source, self.outside)

        with self.assertRaises(onevoke_fs.UnsafePathError):
            onevoke_fs.rename_nofollow(
                self.root, source, self.root / "working" / source.name
            )

        self.assertTrue(source.exists())
        self.assertEqual("outside\n", outside_document.read_text(encoding="utf-8"))

    def test_move_refuses_existing_target_without_changing_either_file(self) -> None:
        source = self.root / "todo" / "20260823-collision-task.md"
        target = self.root / "working" / source.name
        source.write_text("source\n", encoding="utf-8")
        target.write_text("target\n", encoding="utf-8")

        with self.assertRaises(FileExistsError):
            onevoke_fs.rename_nofollow(self.root, source, target)

        self.assertEqual("source\n", source.read_text(encoding="utf-8"))
        self.assertEqual("target\n", target.read_text(encoding="utf-8"))

    def test_move_stays_outside_after_target_parent_becomes_junction(self) -> None:
        source = self.root / "todo" / "20260823-rename-race-task.md"
        target_parent = self.root / "working"
        target = target_parent / source.name
        outside_target = self.outside / source.name
        source.write_text("source\n", encoding="utf-8")
        original_try_open = onevoke_fs._try_open_leaf
        swapped = False

        def open_collision_then_swap(parent_handle, name, path, **kwargs):
            nonlocal swapped
            result = original_try_open(parent_handle, name, path, **kwargs)
            if not swapped and path == target:
                self.set_junction_reparse(target_parent, self.outside)
                swapped = True
            return result

        try:
            with mock.patch.object(
                onevoke_fs, "_try_open_leaf", side_effect=open_collision_then_swap
            ):
                with self.assertRaises(onevoke_fs.UnsafePathError):
                    onevoke_fs.rename_nofollow(self.root, source, target)

            self.assertTrue(swapped, "the target-parent rename race hook did not run")
            self.assertTrue(onevoke_fs.is_reparse_point(target_parent))
            self.assertFalse(outside_target.exists())
        finally:
            if onevoke_fs.is_reparse_point(target_parent):
                self.clear_junction_reparse(target_parent)

        self.assertTrue(source.exists())
        self.assertFalse(target.exists())

    def test_large_create_stays_outside_after_parent_becomes_junction(self) -> None:
        parent = self.root / "backlog"
        directory = parent / "20260823-create-race-task"
        outside_directory = self.outside / directory.name
        original_try_open = onevoke_fs._try_open_leaf
        swapped = False

        def open_collision_then_swap(parent_handle, name, path, **kwargs):
            nonlocal swapped
            result = original_try_open(parent_handle, name, path, **kwargs)
            if not swapped and path == directory:
                self.set_junction_reparse(parent, self.outside)
                swapped = True
            return result

        try:
            with mock.patch.object(
                onevoke_fs, "_try_open_leaf", side_effect=open_collision_then_swap
            ):
                with self.assertRaises(onevoke_fs.UnsafePathError):
                    onevoke_fs.create_directory_with_text_file_nofollow(
                        self.root, directory, "spec.md", "safe\n"
                    )

            self.assertTrue(swapped, "the parent publish race hook did not run")
            self.assertTrue(onevoke_fs.is_reparse_point(parent))
            self.assertFalse(outside_directory.exists())
        finally:
            if onevoke_fs.is_reparse_point(parent):
                self.clear_junction_reparse(parent)

        self.assertFalse(directory.exists())
        self.assertEqual([], list(parent.iterdir()))

    def test_move_directory_renames_the_opened_task_entry(self) -> None:
        source = self.root / "todo" / "20260823-large-task"
        source.mkdir()
        (source / "spec.md").write_bytes(b"large task\n")
        target = self.root / "working" / source.name

        onevoke_fs.rename_nofollow(self.root, source, target)

        self.assertFalse(os.path.lexists(source))
        self.assertEqual(b"large task\n", (target / "spec.md").read_bytes())

    def test_path_escape_is_rejected(self) -> None:
        outside_document = self.outside / "secret.md"
        outside_document.write_text("secret\n", encoding="utf-8")

        with self.assertRaises(onevoke_fs.UnsafePathError):
            onevoke_fs.read_regular_file_nofollow(self.root, outside_document)
        with self.assertRaises(onevoke_fs.UnsafePathError):
            onevoke_fs.write_text_atomic_nofollow(
                self.root, self.root / "todo" / "task.md:alternate", "escape\n"
            )

    def test_executable_search_never_uses_the_repository_cwd(self) -> None:
        planted = self.base / "untrusted-repository"
        planted.mkdir()
        harmless_executable = Path(os.environ["SystemRoot"]) / "System32" / "where.exe"
        shutil.copy2(harmless_executable, planted / "git.exe")
        shutil.copy2(harmless_executable, planted / "codex.exe")

        previous_cwd = Path.cwd()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NoDefaultCurrentDirectoryInExePath", None)
            os.chdir(planted)
            try:
                onevoke_config.configure_stdio()
                self.assertEqual(
                    "1", os.environ["NoDefaultCurrentDirectoryInExePath"]
                )
                git = shutil.which("git")
                codex = shutil.which("codex")
                result = subprocess.run(
                    ["git", "--version"],
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    check=False,
                )
            finally:
                os.chdir(previous_cwd)

        self.assertIsNotNone(git)
        self.assertNotEqual((planted / "git.exe").resolve(), Path(git).resolve())
        if codex is not None:
            self.assertNotEqual((planted / "codex.exe").resolve(), Path(codex).resolve())
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(result.stdout.startswith("git version "), result.stdout)

    def test_kanban_commands_use_windows_backend(self) -> None:
        task_id = "20260823-command-task"
        source = self.root / "backlog" / f"{task_id}.md"
        source.write_text(
            "# Windows command\n\n"
            "- 类型: Chore\n- 任务组:\n- 创建时间: 2026-08-23 00:00\n"
            "- 负责人:\n- 开始时间:\n- 完成时间:\n- 任务分支:\n- 结果:\n\n"
            "## 任务目标\n\ngoal\n\n## 用户决策\n\nN/A\n\n"
            "## 预期成果\n\nresult\n\n## 验收条件\n\n- [ ] accepted\n\n"
            "## 威胁模型\n\nN/A\n\n## 不在本轮范围\n\n- none\n\n"
            "## 讨论与决策\n\nN/A\n\n## 实施与验证\n\nN/A\n\n"
            "## 完成总结\n\nN/A\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env.update({
            "KANBAN_DIR": str(self.root),
            "ONEVOKE_LANG": "en",
            "ONEVOKE_CONFIG": str(self.base / "missing-config.json"),
        })

        moved = subprocess.run(
            [sys.executable, str(KANBAN), "move", task_id, "todo"],
            env=env,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        listing = subprocess.run(
            [sys.executable, str(KANBAN), "list"],
            env=env,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        shown = subprocess.run(
            [sys.executable, str(KANBAN), "show", task_id],
            env=env,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, moved.returncode, moved.stderr)
        self.assertEqual(0, listing.returncode, listing.stderr)
        self.assertEqual(0, shown.returncode, shown.stderr)
        self.assertIn(task_id, listing.stdout)
        self.assertIn("# Windows command", shown.stdout)

    def test_kanban_check_reports_junction_task_entry(self) -> None:
        junction = self.root / "backlog" / "20260823-junction-task"
        self.make_junction(junction, self.outside)
        env = os.environ.copy()
        env.update({
            "KANBAN_DIR": str(self.root),
            "ONEVOKE_LANG": "en",
            "ONEVOKE_CONFIG": str(self.base / "missing-config.json"),
        })

        result = subprocess.run(
            [sys.executable, str(KANBAN), "check"],
            env=env,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("reparse point", result.stderr)

    def test_kanban_rejects_junction_board_root_and_ancestor(self) -> None:
        root_junction = self.base / "board-junction"
        self.make_junction(root_junction, self.root)
        real_parent = self.base / "real-parent"
        nested_board = real_parent / "nested-board"
        shutil.copytree(self.root, nested_board)
        parent_junction = self.base / "parent-junction"
        self.make_junction(parent_junction, real_parent)

        for configured_root in (root_junction, parent_junction / "nested-board"):
            for command in ("list", "init"):
                with self.subTest(configured_root=configured_root, command=command):
                    env = os.environ.copy()
                    env.update({
                        "KANBAN_DIR": str(configured_root),
                        "ONEVOKE_LANG": "en",
                        "ONEVOKE_CONFIG": str(self.base / "missing-config.json"),
                    })

                    result = subprocess.run(
                        [sys.executable, str(KANBAN), command],
                        env=env,
                        text=True,
                        encoding="utf-8",
                        capture_output=True,
                        check=False,
                    )

                    self.assertNotEqual(0, result.returncode)
                    self.assertIn("reparse point", result.stderr)


if __name__ == "__main__":
    unittest.main()
