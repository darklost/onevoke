#!/usr/bin/env python3

import importlib.machinery
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock


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

    def test_read_write_and_move_use_verified_handles(self) -> None:
        source = self.root / "todo" / "20260823-safe-task.md"
        source.write_bytes(b"old\n")

        self.assertEqual(
            b"old\n",
            onevoke_fs.read_regular_file_nofollow(self.root, source),
        )
        onevoke_fs.write_text_atomic_nofollow(self.root, source, "new\n")
        target = self.root / "working" / source.name
        onevoke_fs.rename_nofollow(self.root, source, target)

        self.assertFalse(os.path.lexists(source))
        self.assertEqual(b"new\n", onevoke_fs.read_regular_file_nofollow(self.root, target))
        self.assert_private_acl(target)

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
