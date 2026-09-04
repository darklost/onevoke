#!/usr/bin/env python3

"""Regression-test mixin for Kanban window-location writeback."""

import argparse
import os
import re
from pathlib import Path
from unittest import mock


class WindowWritebackTests:
    def test_resume_writes_back_the_new_window_location(self) -> None:
        task_id, review = self.make_herdr_review("resume-window-writeback")
        review.write_text(
            re.sub(
                r"(?m)^- 窗口:.*$", "- 窗口: herdr:stale-tab:stale-pane",
                review.read_text(encoding="utf-8"), count=1,
            ), encoding="utf-8",
        )
        self.run_command("resume", "--launcher", "herdr", task_id, "--message", "x")
        working = self.root / "working" / review.name
        self.assertIn("- 窗口: herdr:w1:t9:w1:p9\n", working.read_text(encoding="utf-8"))
        result = self.run_command("notify", task_id, "--message", "继续", "--timeout", "61")
        self.assertIn("通道=herdr-direct", result.stdout)
        self.assertEqual("w1:p9", self.herdr_arguments("prompt")[2])
        message_path = Path(re.search(r"消息文件=(\S+)", result.stdout).group(1))
        message_path.unlink()
        message_path.parent.rmdir()

    def test_resume_tmux_writes_back_the_new_window_location(self) -> None:
        self.install_fake_launchers()
        task_id, task = self.make_todo("resume-tmux-window-writeback")
        self.run_command("start", "--launcher", "tmux", "--agent", "claude", task_id)
        working = self.root / "working" / task.name
        self.set_branch(working, "resume-tmux-window-writeback")
        self.run_command("move", task_id, "review")
        review = self.root / "review" / task.name
        review.write_text(re.sub(
            r"(?m)^- 窗口:.*$", "- 窗口: tmux:$42:@old:%old",
            review.read_text(encoding="utf-8"), count=1,
        ), encoding="utf-8")
        self.env["KANBAN_TMUX_CURRENT_COMMAND"] = "claude"
        self.run_command("resume", "--launcher", "tmux", task_id, "--message", "x")
        working = self.root / "working" / task.name
        self.assertIn("- 窗口: tmux:$42:@9:%9\n", working.read_text(encoding="utf-8"))
        result = self.run_command("notify", task_id, "--message", "继续", "--timeout", "61")
        self.assertIn("通道=tmux-direct", result.stdout)
        self.assertIn("%9", (self.root / "tmux.log.send").read_text(encoding="utf-8"))
        message_path = Path(re.search(r"消息文件=(\S+)", result.stdout).group(1))
        message_path.unlink()
        message_path.parent.rmdir()

    def test_resume_foreground_normalizes_a_stale_terminal_window_field(self) -> None:
        kanban = self.load_kanban_module()
        self.install_fake_launchers()
        task_id, task = self.make_todo("resume-foreground-window")
        self.run_command("start", "--agent", "claude", task_id)
        working = self.root / "working" / task.name
        working.write_text(re.sub(
            r"(?m)^- 窗口:.*$", "- 窗口: herdr:stale-tab:stale-pane",
            working.read_text(encoding="utf-8"), count=1,
        ), encoding="utf-8")
        args = argparse.Namespace(
            task=task_id, message="x", message_file=None, launcher="foreground", timeout=61,
        )
        outcome = kanban.LaunchOutcome(process=mock.Mock())
        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch.object(
                kanban, "prepare_launch",
                return_value=kanban.LaunchPlan("foreground", self.root.parent),
            ):
                with mock.patch.object(kanban, "launch_agent", return_value=outcome):
                    with mock.patch.object(kanban, "validate_resumed_agent"):
                        with mock.patch.object(kanban, "report_launch"):
                            kanban.command_resume(args, self.root)
        self.assertIn("- 窗口: foreground\n", working.read_text(encoding="utf-8"))

    def test_resume_process_window_write_failure_restores_review(self) -> None:
        kanban = self.load_kanban_module()
        self.install_fake_launchers()
        task_id, task = self.make_todo("resume-process-write-failure")
        self.run_command("start", "--agent", "claude", task_id)
        working = self.root / "working" / task.name
        self.set_branch(working, "resume-process-write-failure")
        self.run_command("move", task_id, "review")
        review = self.root / "review" / task.name
        before = review.read_text(encoding="utf-8")
        args = argparse.Namespace(
            task=task_id, message="x", message_file=None, launcher="foreground", timeout=61,
        )
        original_write = kanban.write_text_atomic

        def fail_normalization(path, updated, *, entry=None):
            if updated != before:
                raise OSError("window write denied")
            return original_write(path, updated, entry=entry)

        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch.object(
                kanban, "prepare_launch",
                return_value=kanban.LaunchPlan("foreground", self.root.parent),
            ):
                with mock.patch.object(kanban, "write_text_atomic", side_effect=fail_normalization):
                    with mock.patch.object(kanban, "launch_agent") as launch:
                        with self.assertRaisesRegex(OSError, "window write denied"):
                            kanban.command_resume(args, self.root)
        launch.assert_not_called()
        self.assertEqual(before, review.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "working" / task.name).exists())

    def test_notify_resume_channel_writes_back_the_new_window_location(self) -> None:
        task_id, review = self.make_herdr_review("notify-resume-window-writeback")
        review.write_text(re.sub(
            r"(?m)^- 窗口:.*$", "- 窗口: foreground",
            review.read_text(encoding="utf-8"), count=1,
        ), encoding="utf-8")
        result = self.run_command("notify", task_id, "--message", "继续", "--timeout", "61")
        self.assertIn("通道=resume", result.stdout)
        working = self.root / "working" / review.name
        self.assertIn("- 窗口: herdr:w1:t9:w1:p9\n", working.read_text(encoding="utf-8"))

    def test_notify_resume_channel_restores_the_window_when_liveness_fails(self) -> None:
        kanban = self.load_kanban_module()
        task_id, review = self.make_herdr_review("notify-resume-window-rollback")
        entry = kanban.locate(kanban.load_board(self.root), task_id)
        text = review.read_text(encoding="utf-8")

        def launch_with_writeback(
            plan, _root, _name, _invocation, location_callback=None, **_kwargs
        ):
            outcome = kanban.LaunchOutcome(tab="new-tab", pane="new-pane")
            location_callback(outcome)
            return outcome

        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch.object(kanban, "launch_agent", side_effect=launch_with_writeback):
                with mock.patch.object(
                    kanban, "validate_resumed_agent",
                    side_effect=kanban.KanbanError("liveness failed"),
                ):
                    with mock.patch.object(kanban, "cleanup_failed_resume"):
                        with self.assertRaisesRegex(kanban.KanbanError, "liveness failed"):
                            kanban.notify_via_resume(entry, text, "x", self.root, 61)
        self.assertEqual(text, review.read_text(encoding="utf-8"))
        original_write = kanban.write_text_atomic

        def fail_restore(path, updated, *, entry=None):
            if updated == text:
                raise OSError("restore denied")
            return original_write(path, updated, entry=entry)

        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch.object(kanban, "launch_agent", side_effect=launch_with_writeback):
                with mock.patch.object(
                    kanban, "validate_resumed_agent",
                    side_effect=kanban.KanbanError("liveness failed"),
                ):
                    with mock.patch.object(kanban, "cleanup_failed_resume"):
                        with mock.patch.object(kanban, "write_text_atomic", side_effect=fail_restore):
                            with self.assertRaisesRegex(
                                kanban.KanbanError,
                                "liveness failed; 窗口回滚=restore denied",
                            ):
                                kanban.notify_via_resume(entry, text, "x", self.root, 61)
