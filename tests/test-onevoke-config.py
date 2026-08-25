#!/usr/bin/env python3

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = PROJECT_ROOT / "bin"


def _same_real_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(
        os.path.realpath(right)
    )


class InstallContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.saved = {
            name: os.environ.get(name)
            for name in (
                "HOME",
                "USERPROFILE",
                "ONEVOKE_CONFIG",
                "GIT_DIR",
                "GIT_WORK_TREE",
                "GIT_COMMON_DIR",
            )
        }
        os.environ["HOME"] = str(self.home)
        os.environ["USERPROFILE"] = str(self.home)
        os.environ.pop("ONEVOKE_CONFIG", None)
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
            os.environ.pop(name, None)
        sys.path.insert(0, str(BIN_DIR))
        import onevoke_config

        self.config = onevoke_config

    def tearDown(self) -> None:
        sys.path.pop(0)
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.temp.cleanup()

    def init_git_repo(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "-q", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(path), "config", "user.email", "onevoke@example.com"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(path), "config", "user.name", "Onevoke Test"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(path), "commit", "--allow-empty", "-q", "-m", "init"],
            check=True,
            capture_output=True,
            text=True,
        )
        return path

    def assert_same_real_path(self, left: Path, right: Path) -> None:
        self.assertTrue(_same_real_path(left, right), f"{left} != {right}")

    def test_source_tree_entry_keeps_global_home_paths(self) -> None:
        paths = self.config.install_paths(entry=BIN_DIR / "onevoke_config.py")
        self.assertEqual("global", paths.mode)
        self.assertIsNone(paths.project_root)
        self.assertIsNone(paths.install_root)
        self.assertEqual(self.home / ".config" / "onevoke" / "config.json", paths.config_path)
        self.assertEqual(self.home / ".agents", paths.rules_dir)
        self.assertEqual(self.home / ".local" / "bin", paths.bin_dir)
        self.assertEqual(self.home / ".local" / "share" / "onevoke", paths.share_dir)

    def test_posix_uppercase_onevoke_dir_is_not_project(self) -> None:
        if os.name == "nt":
            self.skipTest("Windows treats .ONEVOKE as the project directory")
        source = self.root / "cased"
        entry = source / ".ONEVOKE" / "bin" / "onevoke_config.py"
        entry.parent.mkdir(parents=True)
        entry.write_text("# not a project layout on POSIX\n", encoding="utf-8")
        paths = self.config.install_paths(entry=entry)
        self.assertEqual("global", paths.mode)

    def test_source_layout_with_bin_and_rules_is_not_project(self) -> None:
        source = self.root / "onevoke-src"
        (source / "bin").mkdir(parents=True)
        (source / "rules").mkdir()
        entry = source / "bin" / "onevoke_config.py"
        entry.write_text("# source tree entry\n", encoding="utf-8")
        paths = self.config.install_paths(entry=entry)
        self.assertEqual("global", paths.mode)
        self.assertEqual(self.home / ".config" / "onevoke" / "config.json", paths.config_path)

    def test_project_entry_resolves_main_worktree_layout(self) -> None:
        project = self.init_git_repo(self.root / "app")
        entry = project / ".onevoke" / "bin" / "onevoke"
        entry.parent.mkdir(parents=True)
        entry.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

        paths = self.config.install_paths(entry=entry)

        self.assertEqual("project", paths.mode)
        self.assertIsNotNone(paths.project_root)
        self.assertIsNotNone(paths.install_root)
        assert paths.project_root is not None
        assert paths.install_root is not None
        self.assert_same_real_path(project, paths.project_root)
        self.assertEqual(paths.install_root, paths.project_root / ".onevoke")
        self.assertEqual(paths.config_path, paths.install_root / "config.json")
        self.assertEqual(paths.rules_dir, paths.install_root / "rules")
        self.assertEqual(paths.bin_dir, paths.install_root / "bin")
        self.assertEqual(paths.share_dir, paths.install_root / "share")

    def test_project_install_paths_from_linked_worktree_uses_main(self) -> None:
        main = self.init_git_repo(self.root / "app")
        linked = self.root / "app-linked"
        subprocess.run(
            ["git", "-C", str(main), "worktree", "add", "-q", str(linked), "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )

        paths = self.config.project_install_paths(linked)

        self.assertEqual("project", paths.mode)
        assert paths.project_root is not None
        self.assert_same_real_path(main, paths.project_root)
        self.assertFalse(_same_real_path(linked, paths.project_root))
        self.assertEqual(paths.config_path, paths.project_root / ".onevoke" / "config.json")

    def test_config_path_keeps_env_override(self) -> None:
        override = self.root / "override-config.json"
        os.environ["ONEVOKE_CONFIG"] = str(override)
        self.assertEqual(Path(str(override)).expanduser(), self.config.config_path())

    def test_config_path_without_override_follows_global_scope(self) -> None:
        os.environ.pop("ONEVOKE_CONFIG", None)
        self.assertEqual(
            self.home / ".config" / "onevoke" / "config.json",
            self.config.config_path(),
        )

    def test_git_dir_env_does_not_divert_project_paths(self) -> None:
        project = self.init_git_repo(self.root / "app")
        other = self.init_git_repo(self.root / "other")
        os.environ["GIT_DIR"] = str(other / ".git")
        entry = project / ".onevoke" / "bin" / "onevoke"
        entry.parent.mkdir(parents=True)
        entry.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

        paths = self.config.project_install_paths(project)
        runtime = self.config.install_paths(entry=entry)
        returned = self.config.ensure_project_git_exclude(project)

        self.assertEqual("project", paths.mode)
        assert paths.project_root is not None
        self.assert_same_real_path(project, paths.project_root)
        assert runtime.project_root is not None
        self.assert_same_real_path(project, runtime.project_root)
        exclude = project / ".git" / "info" / "exclude"
        self.assert_same_real_path(exclude, returned)
        self.assertIn(
            "/.onevoke/",
            exclude.read_text(encoding="utf-8").splitlines(),
        )
        other_exclude = (other / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        self.assertNotIn("/.onevoke/", other_exclude.splitlines())

    def test_install_paths_rejects_project_entry_without_git(self) -> None:
        project = self.root / "not-git"
        entry = project / ".onevoke" / "bin" / "onevoke"
        entry.parent.mkdir(parents=True)
        entry.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        with self.assertRaises(self.config.ConfigError):
            self.config.install_paths(entry=entry)

    def test_project_install_paths_rejects_non_git(self) -> None:
        directory = self.root / "not-git"
        directory.mkdir()
        with self.assertRaises(self.config.ConfigError):
            self.config.project_install_paths(directory)

    def test_project_install_paths_rejects_missing_directory(self) -> None:
        with self.assertRaises(self.config.ConfigError):
            self.config.project_install_paths(self.root / "missing")

    def test_git_exclude_is_idempotent_and_preserves_mode(self) -> None:
        project = self.init_git_repo(self.root / "app")
        exclude = project / ".git" / "info" / "exclude"
        exclude.write_text("# local\n", encoding="utf-8")
        os.chmod(exclude, 0o644)

        returned = None
        for _ in range(2):
            returned = self.config.ensure_project_git_exclude(project)

        self.assertIsNotNone(returned)
        assert returned is not None
        self.assert_same_real_path(exclude, returned)
        lines = exclude.read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, lines.count("/.onevoke/"))
        self.assertEqual("# local", lines[0])
        if os.name != "nt":
            self.assertEqual(0o644, exclude.stat().st_mode & 0o777)

    @unittest.skipUnless(os.name == "posix", "POSIX symlink rejection")
    def test_git_exclude_rejects_info_symlink(self) -> None:
        project = self.init_git_repo(self.root / "app")
        info = project / ".git" / "info"
        original = project / ".git" / "info-original"
        info.rename(original)
        outside = self.root / "outside"
        outside.mkdir()
        outside_exclude = outside / "exclude"
        outside_exclude.write_text("keep\n", encoding="utf-8")
        info.symlink_to(outside)

        with self.assertRaises(self.config.ConfigError):
            self.config.ensure_project_git_exclude(project)

        self.assertEqual("keep\n", outside_exclude.read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "posix", "POSIX symlink rejection")
    def test_install_paths_rejects_onevoke_symlink(self) -> None:
        project = self.init_git_repo(self.root / "app")
        payload = self.root / "payload"
        (payload / "bin").mkdir(parents=True)
        (payload / "bin" / "onevoke").write_text("entry\n", encoding="utf-8")
        onevoke = project / ".onevoke"
        onevoke.symlink_to(payload)
        entry = onevoke / "bin" / "onevoke"

        with self.assertRaises(self.config.ConfigError):
            self.config.install_paths(entry=entry)

    @unittest.skipUnless(os.name == "posix", "POSIX symlink rejection")
    def test_project_install_paths_rejects_symlink_target(self) -> None:
        project = self.init_git_repo(self.root / "app")
        link = self.root / "app-link"
        link.symlink_to(project)
        with self.assertRaises(self.config.ConfigError):
            self.config.project_install_paths(link)


if __name__ == "__main__":
    unittest.main()
