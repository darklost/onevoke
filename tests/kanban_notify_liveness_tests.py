#!/usr/bin/env python3

"""Regression-test mixin for notify target lookup and busy retry."""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from unittest import mock


COMMAND = Path(os.environ.get(
    "KANBAN_COMMAND", Path(__file__).resolve().parent.parent / "bin" / "kanban"
)).resolve()


class NotifyLivenessTests:
    @staticmethod
    def set_agent_location(path: Path, session: str, window: str) -> None:
        text = path.read_text(encoding="utf-8")
        text = re.sub(r"(?m)^- 会话:.*$", f"- 会话: {session}", text)
        text = re.sub(r"(?m)^- 窗口:.*$", f"- 窗口: {window}", text)
        path.write_text(text, encoding="utf-8")

    def test_check_reports_alive_stopped_and_unknown_working_agents(self) -> None:
        self.install_fake_launchers()
        alive_id, alive = self.make_todo("liveness-alive")
        stopped_id, stopped = self.make_todo("liveness-stopped")
        unknown_id, unknown = self.make_todo("liveness-unknown")
        for task_id, task in ((alive_id, alive), (stopped_id, stopped), (unknown_id, unknown)):
            self.run_command("move", task_id, "working")
            task = self.root / "working" / task.name
            window = {
                alive_id: "tmux:$1:@1:%1",
                stopped_id: "tmux:$1:@2:%2",
                unknown_id: "foreground",
            }[task_id]
            self.set_agent_location(task, "codex session-1", window)
        self.env["KANBAN_TMUX_PANE_SESSION"] = "session-1"
        self.env["KANBAN_TMUX_STALE_PANE"] = "%2"

        result = self.run_command("check")

        self.assertIn(f"存活: {alive_id}\tAgent=codex\t状态=alive", result.stdout)
        self.assertIn(f"存活: {stopped_id}\tAgent=codex\t状态=stopped", result.stdout)
        self.assertIn(f"存活: {unknown_id}\tAgent=codex\t状态=unknown", result.stdout)
        self.assertEqual("通过: 3 个任务", result.stdout.splitlines()[-1])

    def test_check_reports_a_drifted_agent_with_its_new_address(self) -> None:
        self.install_fake_launchers()
        task_id, task = self.make_todo("liveness-drifted")
        self.run_command("move", task_id, "working")
        task = self.root / "working" / task.name
        self.set_agent_location(task, "codex session-2", "tmux:$1:@1:%1")
        self.env["KANBAN_TMUX_STALE_PANE"] = "%1"
        self.env["KANBAN_TMUX_LIST_PANES"] = "%9\t$9\tnew-session\t@9\tcodex\t0\tsession-2"

        result = self.run_command("check", task_id)

        self.assertIn("状态=drifted", result.stdout)
        self.assertIn("新地址=tmux:$9:@9:%9", result.stdout)
        self.assertIn(f"建议=kanban notify {task_id}", result.stdout)

    def test_check_unknown_reverse_looked_up_herdr_status_is_not_drifted(self) -> None:
        self.install_fake_herdr()
        task_id, task = self.make_todo("liveness-drifted-unknown")
        self.run_command("move", task_id, "working")
        task = self.root / "working" / task.name
        self.set_agent_location(task, "claude session-unknown", "herdr:w0:t0:w0:p0")
        self.env["KANBAN_HERDR_STALE_PANE"] = "w0:p0"
        self.env["KANBAN_HERDR_AGENT"] = "claude"
        self.env["KANBAN_HERDR_SESSION"] = "session-unknown"
        self.env["KANBAN_HERDR_STATUS"] = "unknown"
        self.env["KANBAN_HERDR_LIST_JSON"] = json.dumps({
            "result": {"panes": [{
                "pane_id": "w9:p9", "tab_id": "w9:t9", "agent": "claude",
                "agent_status": "unknown",
                "agent_session": {"value": "session-unknown"},
            }]},
        })

        result = self.run_command("check", task_id)

        self.assertIn("状态=unknown", result.stdout)
        self.assertNotIn("状态=drifted", result.stdout)
        self.assertNotIn("新地址=", result.stdout)
        self.assertIn("反查 pane 的 Agent 状态不可判定", result.stdout)

    def test_check_liveness_does_not_affect_the_exit_code(self) -> None:
        self.install_fake_launchers()
        task_id, task = self.make_todo("liveness-exit")
        self.run_command("move", task_id, "working")
        task = self.root / "working" / task.name
        self.set_agent_location(task, "codex session-3", "tmux:$1:@1:%1")
        self.env["KANBAN_TMUX_STALE_PANE"] = "%1"

        result = self.run_command("check")

        self.assertEqual(0, result.returncode)
        self.assertIn("状态=stopped", result.stdout)

    def test_check_skips_review_and_probes_only_working_cards(self) -> None:
        self.install_fake_launchers()
        working_id, working = self.make_todo("liveness-working-only")
        review_id, review = self.make_todo("liveness-review-skip")
        for task_id, task in ((working_id, working), (review_id, review)):
            self.run_command("move", task_id, "working")
            task = self.root / "working" / task.name
            self.set_agent_location(task, "codex session-4", "tmux:$1:@1:%1")
        review = self.root / "working" / review.name
        review.write_text(
            review.read_text(encoding="utf-8").replace("- 任务分支:\n", "- 任务分支: task/review\n"),
            encoding="utf-8",
        )
        self.run_command("move", review_id, "review")
        self.env["KANBAN_TMUX_PANE_SESSION"] = "session-4"

        result = self.run_command("check", working_id, review_id)

        self.assertIn(f"存活: {working_id}", result.stdout)
        self.assertNotIn(f"存活: {review_id}", result.stdout)

    def test_check_probe_failures_degrade_to_unknown(self) -> None:
        self.install_fake_herdr()
        task_id, task = self.make_todo("liveness-probe-failure")
        self.run_command("move", task_id, "working")
        task = self.root / "working" / task.name
        self.set_agent_location(task, "codex session-5", "herdr:w1:t1:w1:p1")
        self.env["KANBAN_HERDR_GET_FAIL"] = "1"

        result = self.run_command("check", task_id)

        self.assertEqual(0, result.returncode)
        self.assertIn("状态=unknown", result.stdout)

    def test_check_flags_an_unreported_herdr_session_as_undeliverable(self) -> None:
        self.install_fake_herdr()
        task_id, task = self.make_todo("liveness-unreported")
        self.run_command("move", task_id, "working")
        task = self.root / "working" / task.name
        self.set_agent_location(task, "codex session-6", "herdr:w1:t1:w1:p1")
        self.env["KANBAN_HERDR_SESSION"] = ""

        result = self.run_command("check", task_id)

        self.assertIn("状态=alive", result.stdout)
        self.assertIn("会话身份未上报", result.stdout)
        self.assertIn("直投不可用", result.stdout)

    def test_check_codex_without_reference_uses_agent_identity_only(self) -> None:
        self.install_fake_herdr()
        task_id, task = self.make_todo("liveness-codex-reference")
        self.run_command("move", task_id, "working")
        task = self.root / "working" / task.name
        self.set_agent_location(task, "codex", "herdr:w1:t1:w1:p1")
        self.env["KANBAN_HERDR_SESSION"] = "reported-session"

        result = self.run_command("check", task_id)

        self.assertIn("状态=alive", result.stdout)

    def test_subscribe_heartbeat_includes_working_member_liveness(self) -> None:
        self.install_fake_launchers()
        group_id = f"{datetime.now().strftime('%Y%m%d')}-liveness-heartbeat-group"
        task_id, task = self.make_todo("liveness-heartbeat")
        self.set_task_group(task, group_id)
        self.run_command("move", task_id, "working")
        task = self.root / "working" / task.name
        self.set_agent_location(task, "codex session-7", "tmux:$1:@1:%1")
        self.env["KANBAN_TMUX_PANE_SESSION"] = "session-7"

        process = subprocess.Popen(
            [sys.executable, str(COMMAND),
             "subscribe", group_id, task_id, "--refresh", "0.1", "--heartbeat", "0.2"],
            env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            snapshot = self.read_process_json(process)
            heartbeat = self.read_process_json(process)
            self.assertNotIn("liveness", snapshot)
            self.assertEqual("heartbeat", heartbeat["event"])
            self.assertEqual("alive", heartbeat["liveness"][task_id]["status"])
            self.assertEqual("tmux", heartbeat["liveness"][task_id]["channel"])
        finally:
            process.terminate()
            process.communicate(timeout=5)

    def test_subscribe_heartbeat_probe_failure_reports_unknown_without_exiting(self) -> None:
        self.install_fake_herdr()
        group_id = f"{datetime.now().strftime('%Y%m%d')}-liveness-failure-group"
        task_id, task = self.make_todo("liveness-heartbeat-failure")
        self.set_task_group(task, group_id)
        self.run_command("move", task_id, "working")
        task = self.root / "working" / task.name
        self.set_agent_location(task, "codex session-8", "herdr:w1:t1:w1:p1")
        self.env["KANBAN_HERDR_GET_FAIL"] = "1"

        process = subprocess.Popen(
            [sys.executable, str(COMMAND),
             "subscribe", group_id, task_id, "--refresh", "0.1", "--heartbeat", "0.2"],
            env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            self.read_process_json(process)
            heartbeat = self.read_process_json(process)
            self.assertEqual("unknown", heartbeat["liveness"][task_id]["status"])
            self.assertIsNone(process.poll())
        finally:
            process.terminate()
            process.communicate(timeout=5)

    def test_notify_stale_herdr_window_reverse_lookup_redelivers_and_rewrites(self) -> None:
        task_id, review = self.make_herdr_review("notify-stale-herdr")
        review.write_text(re.sub(
            r"(?m)^- 窗口:.*$", "- 窗口: herdr:w0:t0:w0:p0",
            review.read_text(encoding="utf-8"), count=1,
        ), encoding="utf-8")
        self.env["KANBAN_HERDR_STALE_PANE"] = "w0:p0"
        result = self.run_command("notify", task_id, "--message", "x", "--timeout", "61")
        self.assertIn("通道=herdr-direct", result.stdout)
        working = self.root / "working" / review.name
        self.assertIn("- 窗口: herdr:w1:t9:w1:p9\n", working.read_text(encoding="utf-8"))
        self.assertEqual("w1:p9", self.herdr_arguments("prompt")[2])
        message_path = Path(re.search(r"消息文件=(\S+)", result.stdout).group(1))
        message_path.unlink()
        message_path.parent.rmdir()

    def test_notify_stale_tmux_window_uses_pane_option_reverse_lookup(self) -> None:
        self.install_fake_launchers()
        task_id, task = self.make_todo("notify-stale-tmux")
        self.run_command("start", "--launcher", "tmux", "--agent", "claude", task_id)
        working = self.root / "working" / task.name
        session_id = re.search(
            r"(?m)^- 会话: claude (\S+)$", working.read_text(encoding="utf-8")
        ).group(1)
        self.set_branch(working, "notify-stale-tmux")
        self.run_command("move", task_id, "review")
        review = self.root / "review" / task.name
        review.write_text(re.sub(
            r"(?m)^- 窗口:.*$", "- 窗口: tmux:$42:@old:%old",
            review.read_text(encoding="utf-8"), count=1,
        ), encoding="utf-8")
        self.env["KANBAN_TMUX_STALE_PANE"] = "%old"
        self.env["KANBAN_TMUX_CURRENT_COMMAND"] = "claude"
        self.env["KANBAN_TMUX_LIST_PANES"] = (
            f"%new\t$42\tfake-session\t@new\tclaude\t0\t{session_id}"
        )
        result = self.run_command("notify", task_id, "--message", "x", "--timeout", "61")
        self.assertIn("通道=tmux-direct", result.stdout)
        working = self.root / "working" / task.name
        self.assertIn("- 窗口: tmux:$42:@new:%new\n", working.read_text(encoding="utf-8"))
        self.assertIn("%new", (self.root / "tmux.log.send").read_text(encoding="utf-8"))
        message_path = Path(re.search(r"消息文件=(\S+)", result.stdout).group(1))
        message_path.unlink()
        message_path.parent.rmdir()

    def test_notify_reverse_lookup_ambiguity_degrades_to_resume_with_both_reasons(self) -> None:
        task_id, review = self.make_herdr_review("notify-stale-ambiguous")
        review.write_text(re.sub(
            r"(?m)^- 窗口:.*$", "- 窗口: herdr:w0:t0:w0:p0",
            review.read_text(encoding="utf-8"), count=1,
        ), encoding="utf-8")
        session_id = self.env["KANBAN_HERDR_SESSION"]
        self.env["KANBAN_HERDR_STALE_PANE"] = "w0:p0"
        self.env["KANBAN_HERDR_LIST_JSON"] = json.dumps({
            "id": "cli:pane:list",
            "result": {
                "type": "pane_list",
                "panes": [
                    {
                        "pane_id": pane,
                        "tab_id": tab,
                        "agent": "claude",
                        "agent_status": "idle",
                        "agent_session": {"value": session_id},
                    }
                    for pane, tab in (("w1:p9", "w1:t9"), ("w2:p8", "w2:t8"))
                ],
            },
        })
        result = self.run_command("notify", task_id, "--message", "x", "--timeout", "61")
        self.assertIn("通道=resume", result.stdout)
        self.assertIn("直投原因=地址过期: pane 不存在: w0:p0", result.stdout)
        self.assertIn("反查=herdr 会话反查匹配不唯一", result.stdout)

    def test_tmux_reverse_lookup_requires_unique_marker_and_matching_command(self) -> None:
        self.load_kanban_module()
        notify = sys.modules["kanban_notify"]
        self.install_fake_launchers()
        session = argparse.Namespace(agent="claude", reference="wanted")
        self.env["KANBAN_TMUX_LIST_PANES"] = "\n".join((
            "%1\t$1\tone\t@1\tclaude\t0\tother",
            "%2\t$2\ttwo\t@2\tcodex\t0\twanted",
            "%3\t$3\tthree\t@3\tclaude\t1\twanted",
            "%4\t$4\tfour\t@4\tclaude\t0\twanted",
        ))
        with mock.patch.dict(os.environ, self.env, clear=True):
            location = notify.tmux_reverse_lookup("tmux", session)
        self.assertEqual(notify.TmuxPaneLocation("$4", "four", "@4", "%4"), location)
        self.assertEqual("tmux:$4:@4:%4", notify.render_tmux_window("tmux", location))
        self.assertEqual(
            "tmux-session:four:@4:%4",
            notify.render_tmux_window("tmux-session", location),
        )
        self.env["KANBAN_TMUX_LIST_PANES"] += "\n%5\t$5\tfive\t@5\tclaude\t0\twanted"
        with mock.patch.dict(os.environ, self.env, clear=True):
            with self.assertRaisesRegex(notify.KanbanError, "匹配不唯一.*2 个 pane"):
                notify.tmux_reverse_lookup("tmux", session)

    def test_notify_copy_mode_pane_does_not_trigger_reverse_lookup(self) -> None:
        kanban = self.load_kanban_module()
        notify = sys.modules["kanban_notify"]
        self.install_fake_launchers()
        task_id, task = self.make_todo("notify-copy-no-lookup")
        self.run_command("start", "--launcher", "tmux", "--agent", "claude", task_id)
        working = self.root / "working" / task.name
        self.set_branch(working, "notify-copy-no-lookup")
        self.run_command("move", task_id, "review")
        self.env["KANBAN_TMUX_CURRENT_COMMAND"] = "claude"
        self.env["KANBAN_TMUX_IN_MODE"] = "1"
        args = argparse.Namespace(
            task=task_id, message="x", message_file=None, timeout=61, pane=None
        )
        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch.object(notify.time, "monotonic", side_effect=(0.0, 62.0)):
                with mock.patch.object(notify, "tmux_reverse_lookup") as reverse_lookup:
                    with mock.patch.object(kanban, "notify_via_resume") as resume:
                        with mock.patch.object(kanban, "write_notify_message") as writer:
                            with self.assertRaisesRegex(notify.NotifyBusyError, "目标 Agent 忙"):
                                kanban.command_notify(args, self.root)
        reverse_lookup.assert_not_called()
        resume.assert_not_called()
        writer.assert_not_called()

    def test_notify_busy_pane_retries_within_timeout_instead_of_resuming(self) -> None:
        task_id, _review = self.make_herdr_review("notify-busy-retry")
        self.env["KANBAN_HERDR_BUSY_ONCE"] = "1"
        result = self.run_command("notify", task_id, "--message", "x", "--timeout", "61")
        self.assertIn("通道=herdr-direct", result.stdout)
        self.assertEqual(1, self.herdr_arguments("order").count("tab create"))
        self.assertNotIn("--resume", "\n".join(self.herdr_arguments("run")))
        message_path = Path(re.search(r"消息文件=(\S+)", result.stdout).group(1))
        message_path.unlink()
        message_path.parent.rmdir()

    def test_notify_busy_pane_timeout_fails_without_touching_the_card(self) -> None:
        kanban = self.load_kanban_module()
        notify = sys.modules["kanban_notify"]
        task_id, review = self.make_herdr_review("notify-busy-timeout")
        before = review.read_text(encoding="utf-8")
        self.env["KANBAN_HERDR_STATUS"] = "working"
        args = argparse.Namespace(
            task=task_id, message="x", message_file=None, timeout=61, pane=None
        )
        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch.object(notify.time, "monotonic", side_effect=(0.0, 62.0)):
                with mock.patch.object(kanban, "notify_via_resume") as resume:
                    with mock.patch.object(kanban, "write_notify_message") as writer:
                        with self.assertRaisesRegex(notify.NotifyBusyError, "目标 Agent 忙, 未投递"):
                            kanban.command_notify(args, self.root)
        resume.assert_not_called()
        writer.assert_not_called()
        self.assertEqual(before, review.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "working" / review.name).exists())
        self.assertEqual(1, self.herdr_arguments("order").count("tab create"))
