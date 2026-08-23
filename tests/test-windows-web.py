#!/usr/bin/env python3

"""原生 Windows Web 看板启动与 HTTP 端到端测试."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parent.parent
KANBAN = PROJECT_ROOT / "bin" / "kanban"
WEB_ASSETS = PROJECT_ROOT / "share" / "kanban-web"


@unittest.skipUnless(os.name == "nt", "native Windows Web smoke only")
class WindowsWebTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.board = self.root / "kanban"
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "KANBAN_DIR": str(self.board),
                "ONEVOKE_CONFIG": str(self.root / "config.json"),
                "ONEVOKE_LANG": "cn",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_kanban(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, "-X", "utf8", str(KANBAN), *arguments],
            env=self.environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return result

    def test_web_serves_utf8_board_and_static_assets(self) -> None:
        self.run_kanban("init")
        created = self.run_kanban(
            "new", "feature", "windows-web", "Windows 中文看板"
        )
        task_path = Path(created.stdout.strip())
        task_id = task_path.name.removesuffix(".md")

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]

        process = subprocess.Popen(
            [
                sys.executable,
                "-X",
                "utf8",
                str(KANBAN),
                "web",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--refresh",
                "1",
                "--assets",
                str(WEB_ASSETS),
            ],
            env=self.environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            base_url = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 10
            while True:
                if process.poll() is not None:
                    stdout, stderr = process.communicate()
                    self.fail(stderr or stdout or "Web server exited before startup")
                try:
                    with urllib.request.urlopen(base_url + "/api/board", timeout=1) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                    break
                except (urllib.error.URLError, TimeoutError):
                    if time.monotonic() >= deadline:
                        self.fail("Web server did not accept HTTP requests within 10 seconds")
                    time.sleep(0.05)

            self.assertEqual(task_id, payload["tasks"][0]["task_id"])
            self.assertEqual("Windows 中文看板", payload["tasks"][0]["title"])
            self.assertEqual("backlog", payload["tasks"][0]["state"])

            with urllib.request.urlopen(base_url + "/", timeout=2) as response:
                page = response.read().decode("utf-8")
            with urllib.request.urlopen(
                base_url + "/static/board.js", timeout=2
            ) as response:
                script = response.read().decode("utf-8")
            self.assertIn("任务看板", page)
            self.assertIn("/static/board.js", page)
            self.assertIn("/api/board", script)
            self.assertIn('new EventSource("/api/events")', script)
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)

    def test_missing_default_assets_reports_windows_recovery_command(self) -> None:
        sys.path.insert(0, str(KANBAN.parent))
        try:
            import kanban_web
        finally:
            sys.path.pop(0)

        missing_home = self.root / "missing-home"
        missing_script = self.root / "missing-source" / "bin" / "kanban_web.py"
        with (
            mock.patch.object(kanban_web.Path, "home", return_value=missing_home),
            mock.patch.object(kanban_web, "__file__", str(missing_script)),
            mock.patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("ONEVOKE_SHARE", None)
            for language, expected in (
                ("cn", "请在 Onevoke 仓库根目录运行"),
                ("en", "from the Onevoke repository root"),
            ):
                with self.subTest(language=language), mock.patch.dict(
                    os.environ, {"ONEVOKE_LANG": language}, clear=False
                ):
                    with self.assertRaises(kanban_web.KanbanWebError) as caught:
                        kanban_web.resolve_share_dir()
                    message = str(caught.exception)
                    self.assertIn(expected, message)
                    self.assertIn("powershell.exe -NoProfile", message)
                    self.assertIn(r".\install.ps1", message)
                    self.assertNotIn("./install.sh", message)


if __name__ == "__main__":
    unittest.main()
