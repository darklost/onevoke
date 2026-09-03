#!/usr/bin/env python3

"""Reusable stale-dismiss regression mixin for tests/test-kanban.py."""

import json
import re


class DismissTests:
    def test_dismiss_stale_herdr_address_reverse_looks_up_without_writing_window(self) -> None:
        task_id, done = self.make_done("dismiss-stale-herdr", agent="claude")
        before = done.read_bytes()
        session = self.env["KANBAN_HERDR_SESSION"]
        self.env["KANBAN_HERDR_STALE_PANE"] = "w1:p9"
        self.env["KANBAN_HERDR_TAB_ID"] = "w1:t8"
        self.env["KANBAN_HERDR_LIST_JSON"] = json.dumps({
            "id": "cli:pane:list",
            "result": {"type": "pane_list", "panes": [{
                "pane_id": "w1:p8", "tab_id": "w1:t8", "agent": "claude",
                "agent_status": "idle", "agent_session": {"value": session},
            }]},
        })

        result = self.run_command("dismiss", task_id, "--timeout", "61")

        self.assertEqual(before, done.read_bytes())
        self.assertIn("\t关闭容器=w1:t8", result.stdout)
        self.assertEqual(
            ["agent", "prompt", "w1:p8", "/exit"],
            self.herdr_arguments("prompt"),
        )
        self.assertEqual(["tab", "close", "w1:t8"], self.herdr_arguments("close"))

    def test_dismiss_stale_tmux_address_revalidates_the_discovered_container(self) -> None:
        task_id, done = self.make_done(
            "dismiss-stale-tmux", agent="claude", launcher="tmux"
        )
        before = done.read_bytes()
        session = re.search(
            r"(?m)^- 会话: claude (\S+)$", done.read_text(encoding="utf-8")
        ).group(1)
        self.env.update({
            "KANBAN_TMUX_STALE_PANE": "%9",
            "KANBAN_TMUX_CURRENT_COMMAND": "claude",
            "KANBAN_TMUX_PANE_SESSION": session,
            "KANBAN_TMUX_LIST_PANES": f"%8\t$88\trelocated\t@8\tclaude\t0\t{session}",
            "KANBAN_TMUX_TARGET_SESSION": "$88",
            "KANBAN_TMUX_TARGET_SESSION_NAME": "relocated",
            "KANBAN_TMUX_TARGET_WINDOW": "@8",
        })

        result = self.run_command("dismiss", task_id, "--timeout", "61")

        self.assertEqual(before, done.read_bytes())
        self.assertIn("通道=tmux\t关闭容器=@8", result.stdout)
        self.assertEqual(
            "/exit",
            (self.root / "tmux.log.instruction").read_text(encoding="utf-8").strip(),
        )
        self.assertEqual(
            ["kill-window", "-t", "@8"],
            (self.root / "tmux.log.kill").read_text(encoding="utf-8").splitlines(),
        )
