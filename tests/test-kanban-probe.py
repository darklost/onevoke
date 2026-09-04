#!/usr/bin/env python3

import subprocess
import sys
import unittest
import runpy
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "bin"))

import kanban_probe


class KanbanProbeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.kanban = runpy.run_path(str(PROJECT_ROOT / "bin" / "kanban"))

    def test_tmux_display_gone_preserves_detail(self) -> None:
        gone = subprocess.CompletedProcess([], 1, "", "can't find pane: %9")
        with mock.patch.object(kanban_probe.subprocess, "run", return_value=gone):
            probe = kanban_probe.probe_tmux_pane("tmux", "%9")
        self.assertIsNone(probe.facts)
        self.assertEqual("can't find pane: %9", probe.gone_detail)
        with mock.patch.object(kanban_probe.subprocess, "run", return_value=gone):
            self.assertIsNone(kanban_probe.tmux_pane_facts("tmux", "%9"))

    def test_tmux_identity_gone_preserves_detail(self) -> None:
        display = subprocess.CompletedProcess([], 0, "codex\t0\t0\n", "")
        gone = subprocess.CompletedProcess([], 1, "", "no server running")
        with mock.patch.object(
            kanban_probe.subprocess, "run", side_effect=(display, gone)
        ):
            probe = kanban_probe.probe_tmux_pane("tmux", "%9")
        self.assertIsNone(probe.facts)
        self.assertEqual("no server running", probe.gone_detail)

    def test_tmux_identity_missing_and_failure_are_distinct(self) -> None:
        display = subprocess.CompletedProcess([], 0, "codex\t0\t0\n", "")
        missing = subprocess.CompletedProcess([], 1, "", "invalid option: @onevoke_session")
        with mock.patch.object(
            kanban_probe.subprocess, "run", side_effect=(display, missing)
        ):
            facts = kanban_probe.tmux_pane_facts("tmux", "%9")
        self.assertIsNotNone(facts)
        self.assertEqual("", facts.session_marker)

        failure = subprocess.CompletedProcess([], 1, "", "transport failed")
        with mock.patch.object(
            kanban_probe.subprocess, "run", side_effect=(display, failure)
        ):
            with self.assertRaisesRegex(kanban_probe.KanbanError, "transport failed"):
                kanban_probe.tmux_pane_facts("tmux", "%9")

        for detail in ("invalid option: -p", "unknown option: -v"):
            with self.subTest(detail=detail):
                unsupported = subprocess.CompletedProcess([], 1, "", detail)
                with mock.patch.object(
                    kanban_probe.subprocess, "run", side_effect=(display, unsupported)
                ):
                    with self.assertRaisesRegex(kanban_probe.KanbanError, detail):
                        kanban_probe.tmux_pane_facts("tmux", "%9")

    def test_herdr_gone_preserves_detail(self) -> None:
        detail = '{"error":{"code":"pane_not_found","message":"gone"}}'
        gone = subprocess.CompletedProcess([], 1, "", detail)
        with mock.patch.object(kanban_probe.subprocess, "run", return_value=gone):
            probe = kanban_probe.probe_herdr_pane("herdr", "w1:p9")
        self.assertIsNone(probe.pane)
        self.assertEqual(detail, probe.gone_detail)
        with mock.patch.object(kanban_probe.subprocess, "run", return_value=gone):
            self.assertIsNone(kanban_probe.herdr_probe_pane("herdr", "w1:p9"))

    def test_notify_wrappers_preserve_gone_diagnostics(self) -> None:
        tmux_detail = "can't find pane: %9"
        tmux_gone = subprocess.CompletedProcess([], 1, "", tmux_detail)
        with mock.patch.object(kanban_probe.subprocess, "run", return_value=tmux_gone):
            with self.assertRaises(self.kanban["KanbanError"]) as caught:
                self.kanban["tmux_notify_target"](
                    "tmux", "%9", self.kanban["AgentSession"]("codex", "session")
                )
        self.assertEqual(
            self.kanban["t"](
                f"tmux pane 不存在: %9: {tmux_detail}",
                f"tmux pane does not exist: %9: {tmux_detail}",
            ),
            str(caught.exception),
        )

        herdr_detail = '{"error":{"code":"pane_not_found","message":"gone"}}'
        herdr_gone = subprocess.CompletedProcess([], 1, "", herdr_detail)
        with mock.patch.object(kanban_probe.subprocess, "run", return_value=herdr_gone):
            with self.assertRaises(self.kanban["KanbanError"]) as caught:
                self.kanban["herdr_pane_info"]("herdr", "w1:p9")
        self.assertEqual(
            self.kanban["t"](
                f"pane 不存在: w1:p9: {herdr_detail}",
                f"pane does not exist: w1:p9: {herdr_detail}",
            ),
            str(caught.exception),
        )


if __name__ == "__main__":
    unittest.main()
