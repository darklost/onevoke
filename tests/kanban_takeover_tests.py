#!/usr/bin/env python3

"""Reusable takeover regression mixin for tests/test-kanban.py."""

import argparse
import io
import os
import re
from unittest import mock


class TakeoverTests:
    def test_resume_with_agent_takes_over_with_a_fresh_session(self) -> None:
        self.install_fake_launchers()
        task_id, task = self.make_todo("takeover-fresh")
        self.run_command("start", "--agent", "claude", task_id)
        working = self.root / "working" / task.name
        old_session = re.search(
            r"(?m)^- 会话: claude (\S+)$", working.read_text(encoding="utf-8")
        ).group(1)

        result = self.run_command(
            "resume", "--agent", "grok", task_id, "--message", "继续实现"
        )

        self.assertIn(f"已接管: {task_id}", result.stdout)
        command = self.last_launch_command()
        new_session = re.search(r"--session-id ([0-9a-f-]{36})", command).group(1)
        self.assertNotEqual(old_session, new_session)
        self.assertNotIn("--resume", command)

    def test_takeover_rewrites_assignee_session_and_window_fields(self) -> None:
        self.install_fake_launchers()
        task_id, task = self.make_todo("takeover-fields")
        self.run_command("start", "--agent", "claude", task_id)
        working = self.root / "working" / task.name
        started = re.search(
            r"(?m)^- 开始时间: (.+)$", working.read_text(encoding="utf-8")
        ).group(1)

        self.run_command("resume", "--agent", "grok", task_id, "--message", "继续")

        text = working.read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^- 负责人: grok$")
        self.assertRegex(text, r"(?m)^- 会话: grok [0-9a-f-]{36}$")
        self.assertIn("- 窗口: tmux:$42:@9:%9\n", text)
        self.assertIn(f"- 开始时间: {started}\n", text)

    def test_takeover_same_agent_name_still_allocates_a_new_session(self) -> None:
        self.install_fake_launchers()
        task_id, task = self.make_todo("takeover-same")
        self.run_command("start", "--agent", "claude", task_id)
        working = self.root / "working" / task.name
        old = re.search(
            r"(?m)^- 会话: claude (\S+)$", working.read_text(encoding="utf-8")
        ).group(1)

        self.run_command(
            "resume", "--agent", "claude", task_id, "--message", "新会话接管"
        )

        new = re.search(
            r"(?m)^- 会话: claude (\S+)$", working.read_text(encoding="utf-8")
        ).group(1)
        self.assertNotEqual(old, new)
        self.assertIn(f"--session-id {new}", self.last_launch_command())

    def test_takeover_cursor_create_chat_failure_leaves_the_card_untouched(self) -> None:
        self.install_fake_launchers()
        task_id, task = self.make_todo("takeover-cursor-fail")
        self.run_command("start", "--agent", "claude", task_id)
        working = self.root / "working" / task.name
        before = working.read_bytes()
        self.env["KANBAN_CURSOR_CHAT_FAIL"] = "1"

        result = self.run_command(
            "resume", "--agent", "cursor", task_id, "--message", "继续",
            succeeds=False,
        )

        self.assertIn("create-chat", result.stderr)
        self.assertEqual(before, working.read_bytes())

    def test_takeover_launch_failure_restores_fields_and_review_state(self) -> None:
        self.install_fake_launchers()
        task_id, task = self.make_todo("takeover-launch-fail")
        self.run_command("start", "--agent", "claude", task_id)
        working = self.root / "working" / task.name
        self.set_branch(working, "takeover-launch-fail")
        self.run_command("move", task_id, "review")
        review = self.root / "review" / task.name
        before = review.read_bytes()
        self.env["KANBAN_TMUX_FAIL"] = "1"

        self.run_command(
            "resume", "--agent", "grok", task_id, "--message", "继续",
            succeeds=False,
        )

        self.assertEqual(before, review.read_bytes())
        self.assertFalse(working.exists())

    def test_takeover_liveness_failure_cleans_new_container_and_rolls_back(self) -> None:
        kanban = self.load_kanban_module()
        self.install_fake_launchers()
        task_id, task = self.make_todo("takeover-liveness-fail")
        self.run_command("start", "--agent", "claude", task_id)
        working = self.root / "working" / task.name
        before = working.read_bytes()
        args = argparse.Namespace(
            task=task_id, agent="grok", message="继续", message_file=None,
            launcher="tmux", timeout=61,
        )
        outcome = kanban.LaunchOutcome(window="@new", pane="%new")

        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch.object(kanban, "launch_agent", return_value=outcome):
                with mock.patch.object(
                    kanban, "validate_resumed_agent",
                    side_effect=kanban.KanbanError("not alive"),
                ):
                    with mock.patch.object(kanban, "cleanup_failed_resume") as cleanup:
                        with self.assertRaisesRegex(kanban.KanbanError, "not alive"):
                            kanban.command_resume(args, self.root)

        cleanup.assert_called_once()
        self.assertEqual(before, working.read_bytes())

    def test_takeover_prompt_and_task_file_mark_the_previous_agent(self) -> None:
        self.install_fake_launchers()
        task_id, _task = self.make_todo("takeover-prompt")
        self.run_command("start", "--agent", "claude", task_id)

        self.run_command(
            "resume", "--agent", "grok", task_id, "--message", "只处理剩余项"
        )

        command = self.last_launch_command()
        self.assertIn(f"接管 Kanban 任务 {task_id}", command)
        self.assertNotIn("只处理剩余项", command)
        task_file = self.task_file_from_command(command)
        self.assertIn("takeover-", task_file.name)
        content = task_file.read_text(encoding="utf-8")
        self.assertIn("原执行 Agent claude 已停止", content)
        self.assertIn("本会话是全新会话", content)
        self.assertIn("git status、git log", content)
        self.assertIn("只处理剩余项", content)

    def test_takeover_extends_codex_prompt_prefixes_for_rollout_lookup(self) -> None:
        kanban = self.load_kanban_module()
        task_id = "20260903-prefix-task"

        prefixes = kanban.codex_prompt_prefixes(task_id)

        self.assertIn(
            f"接管 Kanban 任务 {task_id}; full instructions are in the UTF-8 task file at ",
            prefixes,
        )
        self.assertIn(f"接管 Kanban 任务 {task_id}.", prefixes)

    def test_takeover_tmux_codex_writes_the_discovered_session_id(self) -> None:
        kanban = self.load_kanban_module()
        self.install_fake_launchers()
        task_id, task = self.make_todo("takeover-codex-id")
        self.run_command("start", "--agent", "claude", task_id)
        working = self.root / "working" / task.name
        args = argparse.Namespace(
            task=task_id, agent="codex", message="继续", message_file=None,
            launcher="tmux", timeout=61,
        )
        discovered = "new-codex-session"

        def launch(_plan, _root, _name, _invocation, location, **kwargs):
            outcome = kanban.LaunchOutcome(window="@new", pane="%new")
            location(outcome)
            self.assertEqual(discovered, kwargs["pane_session_callback"]().reference)
            return outcome

        cleanup = mock.Mock(
            cleaned=True, old_window="N/A", channel="N/A", container="N/A", detail=""
        )
        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch.object(
                kanban, "codex_sessions_for_task",
                side_effect=(("old-codex-session",), ("old-codex-session", discovered)),
            ):
                with mock.patch.object(kanban, "launch_agent", side_effect=launch):
                    with mock.patch.object(kanban, "validate_resumed_agent"):
                        with mock.patch.object(
                            kanban, "cleanup_takeover_container", return_value=cleanup
                        ):
                            kanban.command_resume(args, self.root)

        self.assertIn(
            f"- 会话: codex {discovered}\n", working.read_text(encoding="utf-8")
        )

    def test_takeover_herdr_codex_accepts_a_reported_session_id(self) -> None:
        kanban = self.load_kanban_module()
        plan = kanban.LaunchPlan("herdr", self.root.parent, herdr_bin="herdr")
        outcome = kanban.LaunchOutcome(tab="w1:t9", pane="w1:p9")
        pane = {
            "agent": "codex", "agent_status": "idle",
            "agent_session": {"value": "reported-after-launch"},
        }

        with mock.patch.object(kanban, "herdr_pane_info", return_value=pane):
            kanban.validate_resumed_agent(
                plan, outcome, kanban.AgentSession("codex", ""), 61
            )

    def takeover_cleanup_operations(self, kanban, **overrides):
        defaults = {
            "probe_herdr_pane": mock.Mock(),
            "herdr_tab_panes": mock.Mock(return_value=["w1:p1"]),
            "validate_herdr_container": mock.Mock(),
            "herdr_close_tab": mock.Mock(return_value=None),
            "herdr_agent_prompt": mock.Mock(),
            "herdr_wait_agent_exit": mock.Mock(return_value=True),
            "probe_tmux_pane": mock.Mock(),
            "validate_tmux_container": mock.Mock(),
            "tmux_close_window": mock.Mock(return_value=None),
            "tmux_send_agent_exit": mock.Mock(),
            "tmux_wait_agent_exit": mock.Mock(return_value=True),
            "tmux_window_exists": mock.Mock(return_value=False),
            "agent_exit_command": mock.Mock(return_value="/exit"),
        }
        defaults.update(overrides)
        return kanban.CleanupOperations(**defaults)

    def test_takeover_closes_the_old_herdr_tab_of_a_dead_agent(self) -> None:
        kanban = self.load_kanban_module()
        pane = {"pane_id": "w1:p1", "tab_id": "w1:t1", "agent_status": "unknown"}
        operations = self.takeover_cleanup_operations(
            kanban, probe_herdr_pane=mock.Mock(return_value=mock.Mock(pane=pane))
        )
        cleanup_shutil = kanban.cleanup_takeover_container.__globals__["shutil"]

        with mock.patch.object(cleanup_shutil, "which", return_value="herdr"):
            result = kanban.cleanup_takeover_container(
                "herdr:w1:t1:w1:p1", kanban.AgentSession("claude", "old"),
                "herdr:w1:t2:w1:p2", 61, operations,
            )

        self.assertTrue(result.cleaned)
        operations.herdr_agent_prompt.assert_not_called()
        operations.herdr_close_tab.assert_called_once_with("herdr", "w1:t1")

    def test_takeover_gracefully_exits_a_live_matching_old_agent_before_close(self) -> None:
        kanban = self.load_kanban_module()
        pane = {
            "pane_id": "w1:p1", "tab_id": "w1:t1", "agent": "claude",
            "agent_status": "idle", "agent_session": {"value": "old"},
        }
        operations = self.takeover_cleanup_operations(
            kanban, probe_herdr_pane=mock.Mock(return_value=mock.Mock(pane=pane))
        )
        cleanup_shutil = kanban.cleanup_takeover_container.__globals__["shutil"]

        with mock.patch.object(cleanup_shutil, "which", return_value="herdr"):
            result = kanban.cleanup_takeover_container(
                "herdr:w1:t1:w1:p1", kanban.AgentSession("claude", "old"),
                "herdr:w1:t2:w1:p2", 61, operations,
            )

        self.assertTrue(result.cleaned)
        operations.herdr_agent_prompt.assert_called_once_with(
            "herdr", "w1:p1", "/exit"
        )
        operations.herdr_wait_agent_exit.assert_called_once()
        operations.herdr_close_tab.assert_called_once()

    def test_takeover_keeps_a_dirty_or_mismatched_old_container_and_reports(self) -> None:
        kanban = self.load_kanban_module()
        pane = {
            "pane_id": "w1:p1", "tab_id": "w1:t1", "agent": "claude",
            "agent_status": "idle", "agent_session": {"value": "other"},
        }
        operations = self.takeover_cleanup_operations(
            kanban, probe_herdr_pane=mock.Mock(return_value=mock.Mock(pane=pane))
        )
        cleanup_shutil = kanban.cleanup_takeover_container.__globals__["shutil"]

        with mock.patch.object(cleanup_shutil, "which", return_value="herdr"):
            result = kanban.cleanup_takeover_container(
                "herdr:w1:t1:w1:p1", kanban.AgentSession("claude", "old"),
                "herdr:w1:t2:w1:p2", 61, operations,
            )

        self.assertFalse(result.cleaned)
        self.assertIn("身份不匹配", result.detail)
        operations.herdr_agent_prompt.assert_not_called()
        operations.herdr_close_tab.assert_not_called()

    def test_takeover_herdr_empty_reference_retains_unrelated_live_codex(self) -> None:
        kanban = self.load_kanban_module()
        pane = {
            "pane_id": "w1:p1", "tab_id": "w1:t1", "agent": "codex",
            "agent_status": "idle",
            "agent_session": {"value": "unrelated-live-session"},
        }
        operations = self.takeover_cleanup_operations(
            kanban, probe_herdr_pane=mock.Mock(return_value=mock.Mock(pane=pane))
        )
        cleanup_shutil = kanban.cleanup_takeover_container.__globals__["shutil"]

        with mock.patch.object(cleanup_shutil, "which", return_value="herdr"):
            result = kanban.cleanup_takeover_container(
                "herdr:w1:t1:w1:p1", kanban.AgentSession("codex", ""),
                "herdr:w1:t2:w1:p2", 61, operations,
            )

        self.assertFalse(result.cleaned)
        self.assertIn("身份不匹配", result.detail)
        operations.herdr_agent_prompt.assert_not_called()
        operations.herdr_wait_agent_exit.assert_not_called()
        operations.herdr_close_tab.assert_not_called()

    def test_herdr_wait_for_exit_rejects_a_disappeared_session_identity(self) -> None:
        kanban = self.load_kanban_module()
        response = mock.Mock(
            returncode=0, stderr="",
            stdout=(
                '{"result":{"pane":{"pane_id":"w1:p1","tab_id":"w1:t1",'
                '"agent":"codex","agent_status":"idle","agent_session":{}}}}'
            ),
        )

        with mock.patch.object(kanban, "herdr_capture", return_value=response):
            with self.assertRaisesRegex(kanban.KanbanError, "会话已变更"):
                kanban.herdr_wait_agent_exit(
                    "herdr", "w1:t1", "w1:p1",
                    kanban.AgentSession("codex", "expected-session"), 61,
                )

    def test_tmux_wait_for_exit_rejects_an_empty_expected_session(self) -> None:
        kanban = self.load_kanban_module()
        process = mock.Mock(returncode=0, stdout="codex\t0\n", stderr="")
        identity = mock.Mock(
            returncode=0, stdout="unrelated-live-session\n", stderr=""
        )

        with mock.patch.object(
            kanban, "tmux_capture", side_effect=(process, identity)
        ):
            with self.assertRaisesRegex(kanban.KanbanError, "会话已变更"):
                kanban.tmux_wait_agent_exit(
                    "tmux", "tmux", "$1", "@1", "%1",
                    kanban.AgentSession("codex", ""), 61,
                )

    def test_takeover_foreground_card_reports_no_old_container(self) -> None:
        self.install_fake_launchers()
        task_id, task = self.make_todo("takeover-foreground-old")
        self.run_command("start", "--agent", "claude", task_id)
        working = self.root / "working" / task.name
        text = re.sub(
            r"(?m)^- 窗口:.*$", "- 窗口: foreground",
            working.read_text(encoding="utf-8"), count=1,
        )
        working.write_text(text, encoding="utf-8")

        result = self.run_command(
            "resume", "--agent", "grok", task_id, "--message", "继续"
        )

        self.assertIn(
            "已清理原容器: N/A\t通道=N/A\t关闭容器=N/A", result.stdout
        )
        self.assertIn(f"已接管: {task_id}", result.stdout)

    def test_takeover_tmux_matching_agent_exits_before_closing(self) -> None:
        kanban = self.load_kanban_module()
        facts = mock.Mock(dead="0", command="claude", session_marker="old", in_mode="0")
        operations = self.takeover_cleanup_operations(
            kanban, probe_tmux_pane=mock.Mock(return_value=mock.Mock(facts=facts))
        )
        cleanup_shutil = kanban.cleanup_takeover_container.__globals__["shutil"]

        with mock.patch.object(cleanup_shutil, "which", return_value="tmux"):
            result = kanban.cleanup_takeover_container(
                "tmux:$1:@1:%1", kanban.AgentSession("claude", "old"),
                "tmux:$2:@2:%2", 61, operations,
            )

        self.assertTrue(result.cleaned)
        operations.tmux_send_agent_exit.assert_called_once_with("tmux", "%1", "/exit")
        operations.tmux_wait_agent_exit.assert_called_once()
        operations.tmux_close_window.assert_called_once_with("tmux", "@1")

    def test_takeover_tmux_mismatched_command_retains_the_old_window(self) -> None:
        kanban = self.load_kanban_module()
        facts = mock.Mock(dead="0", command="vim", session_marker="old", in_mode="0")
        operations = self.takeover_cleanup_operations(
            kanban, probe_tmux_pane=mock.Mock(return_value=mock.Mock(facts=facts))
        )
        cleanup_shutil = kanban.cleanup_takeover_container.__globals__["shutil"]

        with mock.patch.object(cleanup_shutil, "which", return_value="tmux"):
            result = kanban.cleanup_takeover_container(
                "tmux:$1:@1:%1", kanban.AgentSession("claude", "old"),
                "tmux:$2:@2:%2", 61, operations,
            )

        self.assertFalse(result.cleaned)
        self.assertIn("身份不匹配", result.detail)
        operations.tmux_send_agent_exit.assert_not_called()
        operations.tmux_close_window.assert_not_called()

    def test_takeover_tmux_empty_reference_retains_unrelated_live_codex(self) -> None:
        kanban = self.load_kanban_module()
        facts = mock.Mock(
            dead="0", command="codex", session_marker="unrelated-live-session",
            in_mode="0",
        )
        operations = self.takeover_cleanup_operations(
            kanban, probe_tmux_pane=mock.Mock(return_value=mock.Mock(facts=facts))
        )
        cleanup_shutil = kanban.cleanup_takeover_container.__globals__["shutil"]

        with mock.patch.object(cleanup_shutil, "which", return_value="tmux"):
            result = kanban.cleanup_takeover_container(
                "tmux:$1:@1:%1", kanban.AgentSession("codex", ""),
                "tmux:$2:@2:%2", 61, operations,
            )

        self.assertFalse(result.cleaned)
        self.assertIn("身份不匹配", result.detail)
        operations.tmux_send_agent_exit.assert_not_called()
        operations.tmux_wait_agent_exit.assert_not_called()
        operations.tmux_close_window.assert_not_called()

    def test_resume_uses_a_recorded_codex_session_without_scanning_rollouts(self) -> None:
        kanban = self.load_kanban_module()
        self.install_fake_launchers()
        task_id, task = self.make_todo("resume-recorded-codex")
        self.run_command("start", "--agent", "claude", task_id)
        working = self.root / "working" / task.name
        text = re.sub(
            r"(?m)^- 会话:.*$", "- 会话: codex recorded-session",
            working.read_text(encoding="utf-8"), count=1,
        )
        working.write_text(text, encoding="utf-8")
        args = argparse.Namespace(
            task=task_id, agent=None, message="继续", message_file=None,
            launcher="tmux", timeout=61,
        )

        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch.object(kanban, "find_codex_session") as find:
                with mock.patch.object(kanban, "validate_resumed_agent"):
                    kanban.command_resume(args, self.root)

        find.assert_not_called()
        self.assertIn("recorded-session", self.last_launch_command())

    def test_takeover_cleanup_read_failure_is_reported_without_failing(self) -> None:
        kanban = self.load_kanban_module()
        self.install_fake_launchers()
        task_id, task = self.make_todo("takeover-cleanup-read-fail")
        self.run_command("start", "--agent", "claude", task_id)
        working = self.root / "working" / task.name
        args = argparse.Namespace(
            task=task_id, agent="grok", message="继续", message_file=None,
            launcher="tmux", timeout=61,
        )
        real_read = kanban.read_document
        reads = 0

        def fail_after_liveness(entry):
            nonlocal reads
            reads += 1
            if reads >= 4:
                raise OSError("late card read failed")
            return real_read(entry)

        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch.object(kanban, "read_document", side_effect=fail_after_liveness):
                with mock.patch.object(kanban, "validate_resumed_agent"):
                    with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                        kanban.command_resume(args, self.root)

        self.assertIn("原容器保留", output.getvalue())
        self.assertIn("late card read failed", output.getvalue())
        self.assertIn(f"已接管: {task_id}", output.getvalue())
