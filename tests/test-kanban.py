#!/usr/bin/env python3

import argparse
import base64
from contextlib import contextmanager, redirect_stderr
import hashlib
import io
import json
import math
import os
import re
import runpy
import select
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import unittest
from datetime import datetime
from pathlib import Path
from typing import Optional
from unittest import mock

if os.name == "posix":
    import fcntl
    import pty
    import termios
else:
    fcntl = None
    pty = None
    termios = None


PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 默认测当前工作树; 回落到已安装命令会让改动后的代码看起来仍然通过.
COMMAND = Path(
    os.environ.get("KANBAN_COMMAND", PROJECT_ROOT / "bin" / "kanban")
).resolve()
INSTALLER = PROJECT_ROOT / "install.sh"
INSTALLED_ZH = "Onevoke 已安装\n"
INSTALLED_EN = "Onevoke installed\n"
_LOCALE_VARS = ("ONEVOKE_LANG", "LC_ALL", "LC_MESSAGES", "LANG")


def install_env(home: Path, **extra: str) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _LOCALE_VARS
    }
    env["HOME"] = str(home)
    env["ONEVOKE_CONFIG"] = str(home / ".config" / "onevoke" / "config.json")
    env.update(extra)
    return env
RULES_DIR = PROJECT_ROOT / "rules"
RULES = RULES_DIR / "KANBAN-RULES.md"
AGENT_RULES = RULES_DIR / "ONEVOKE-AGENTS.md"
STATES = ("backlog", "todo", "working", "review", "done", "archived", "trash")


@unittest.skipUnless(os.name == "posix", "PTY, flock, tmux, and shell tests require POSIX")
class KanbanCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for state in STATES:
            (self.root / state).mkdir()
        self.home = self.root / "home"
        self.config = self.home / ".config" / "onevoke" / "config.json"
        self.environment = mock.patch.dict(
            os.environ,
            {
                "ONEVOKE_LANG": "zh",
                "ONEVOKE_CONFIG": str(self.config),
            },
        )
        self.environment.start()
        rules_dir = self.home / ".agents"
        rules_dir.mkdir(parents=True)
        (rules_dir / "KANBAN-RULES.md").write_bytes(RULES.read_bytes())
        self.env = os.environ.copy()
        self.env["HOME"] = str(self.home)
        self.env["KANBAN_DIR"] = str(self.root)
        self.env.pop("TMUX", None)
        self.env.pop("TMUX_PANE", None)
        for name in (
            "HERDR_ENV",
            "HERDR_WORKSPACE_ID",
            "HERDR_TAB_ID",
            "HERDR_PANE_ID",
            "HERDR_SOCKET_PATH",
        ):
            self.env.pop(name, None)

    def tearDown(self) -> None:
        self.environment.stop()
        self.temp.cleanup()

    def run_command(
        self,
        *args: str,
        succeeds: bool = True,
        input_text: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [sys.executable, str(COMMAND), *args],
            env=self.env,
            text=True,
            input=input_text,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
        if succeeds and result.returncode != 0:
            self.fail(result.stderr)
        if not succeeds and result.returncode == 0:
            self.fail(f"command unexpectedly succeeded: {' '.join(args)}")
        return result

    @staticmethod
    def make_ready(path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        replacements = ("实现目标", "产生可验证结果", "满足验收", "无额外范围")
        for replacement in replacements:
            text = text.replace("<填写>", replacement, 1)
        path.write_text(text, encoding="utf-8")

    @staticmethod
    def complete(path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        text = text.replace("- 结果:\n", "- 结果: completed\n", 1)
        text = text.replace("<填写>", "验证通过")
        path.write_text(text, encoding="utf-8")

    def make_todo(self, slug: str) -> tuple[str, Path]:
        task_id = f"{datetime.now().strftime('%Y%m%d')}-{slug}-task"
        self.run_command("new", "chore", slug, f"任务 {slug}")
        task = self.root / "backlog" / f"{task_id}.md"
        self.make_ready(task)
        self.run_command("move", task_id, "todo")
        return task_id, self.root / "todo" / task.name

    @staticmethod
    def set_task_group(path: Path, task_group: str) -> None:
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace("- 任务组:\n", f"- 任务组: {task_group}\n", 1),
            encoding="utf-8",
        )

    @staticmethod
    def set_prerequisites(path: Path, value: str) -> None:
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace(
                "## 讨论与决策\n\n",
                f"## 讨论与决策\n\n```text\n前置任务: {value}\n```\n\n",
                1,
            ),
            encoding="utf-8",
        )

    def read_process_json(
        self, process: subprocess.Popen, *, timeout: float = 5.0
    ) -> dict:
        assert process.stdout is not None
        ready, _, _ = select.select([process.stdout], [], [], timeout)
        self.assertTrue(ready, f"事件读取超时; returncode={process.poll()}")
        line = process.stdout.readline()
        self.assertTrue(line, f"事件流提前结束; returncode={process.poll()}")
        return json.loads(line)

    def install_fake_launchers(self) -> Path:
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir(exist_ok=True)
        tmux = fake_bin / "tmux"
        tmux.write_text(
            """#!/bin/sh
if [ "$1" = "display-message" ]; then
    if [ "${5:-}" = '#{pane_current_command}\t#{pane_in_mode}\t#{pane_dead}' ]; then
        current="${KANBAN_TMUX_CURRENT_COMMAND:-}"
        if [ -z "$current" ] && [ -f "$KANBAN_TMUX_LOG.current" ]; then
            current=$(sed -n '1p' "$KANBAN_TMUX_LOG.current")
        fi
        printf '%s\t%s\t%s\\n' "${current:-codex}" "${KANBAN_TMUX_IN_MODE:-0}" "${KANBAN_TMUX_DEAD:-0}"
        exit 0
    fi
    printf '%s\\n' '$42'
    exit 0
fi
if [ "$1" = "has-session" ]; then
    for name in ${KANBAN_TMUX_SESSIONS:-}; do
        [ "$name" = "${3#=}" ] && exit 0
    done
    exit 1
fi
if [ "$1" = "show-options" ]; then
    if [ "${2:-}" = "-p" ]; then
        [ "${KANBAN_TMUX_PANE_SESSION_MISSING:-}" != "1" ] || exit 1
        if [ -n "${KANBAN_TMUX_PANE_SESSION:-}" ]; then
            printf '%s\\n' "$KANBAN_TMUX_PANE_SESSION"
        elif [ -f "$KANBAN_TMUX_LOG.pane-session" ]; then
            sed -n '1p' "$KANBAN_TMUX_LOG.pane-session"
        else
            exit 1
        fi
        exit 0
    fi
    # 真实 tmux 对未设置的用户选项返回非零并报 invalid option.
    [ -n "${KANBAN_TMUX_PROJECT:-}" ] || exit 1
    printf '%s\\n' "$KANBAN_TMUX_PROJECT"
    exit 0
fi
if [ "$1" = "set-option" ]; then
    if [ "${2:-}" = "-p" ]; then
        printf '%s\\n' "$@" > "$KANBAN_TMUX_LOG.pane-setopt"
        if [ "${KANBAN_TMUX_PANE_SETOPT_FAIL:-}" = "1" ]; then
            printf '%s\\n' 'fake pane set-option failure' >&2
            exit 1
        fi
        printf '%s\\n' "$6" > "$KANBAN_TMUX_LOG.pane-session"
        exit 0
    fi
    printf '%s\\n' "$@" > "$KANBAN_TMUX_LOG.setopt"
    exit 0
fi
if [ "$1" = "send-keys" ]; then
    printf '%s\\n' "$@" >> "$KANBAN_TMUX_LOG.send"
    if [ "$4" = "-l" ]; then
        printf '%s\\n' "$5" > "$KANBAN_TMUX_LOG.instruction"
    fi
    [ "${KANBAN_TMUX_SEND_FAIL:-}" = "1" ] && exit 1
    exit 0
fi
if [ "$1" = "capture-pane" ]; then
    [ "${KANBAN_TMUX_CAPTURE_FAIL:-}" = "1" ] && exit 1
    if [ "${KANBAN_TMUX_ACK:-1}" = "1" ] && [ -f "$KANBAN_TMUX_LOG.instruction" ]; then
        sed -n 's/.*先原样输出 \\([^, ]*\\),.*/• \\1/p' "$KANBAN_TMUX_LOG.instruction"
    fi
    exit 0
fi
if [ "$1" = "kill-window" ]; then
    printf '%s\\n' "$@" > "$KANBAN_TMUX_LOG.kill"
    [ "${KANBAN_TMUX_KILL_FAIL:-}" = "1" ] && exit 1
    exit 0
fi
if [ "$1" = "respawn-pane" ]; then
    printf '%s\\n' "$5" > "$KANBAN_TMUX_LOG.command"
    current=${5%% *}
    printf '%s\\n' "${current##*/}" > "$KANBAN_TMUX_LOG.current"
    if [ "${current##*/}" = "codex" ]; then
        task=$(printf '%s\\n' "$5" | grep -Eo '[0-9]{8}-[a-z0-9-]+-task' | head -n 1)
        if [ -n "$task" ]; then
            session_dir="$HOME/.codex/sessions/fake"
            mkdir -p "$session_dir"
            printf '%s\\n' \
                '{"type":"session_meta","payload":{"id":"fake-codex-session"}}' \
                > "$session_dir/rollout-$task.jsonl"
            printf '{"type":"event_msg","payload":{"type":"user_message","message":"执行 Kanban 任务 %s."}}\\n' \
                "$task" >> "$session_dir/rollout-$task.jsonl"
        fi
    fi
    if [ -n "${KANBAN_TMUX_MUTATE_CARD:-}" ]; then
        printf '%s\\n' '# agent mutation' >> "$KANBAN_TMUX_MUTATE_CARD"
    fi
    if [ "${KANBAN_TMUX_RESPAWN_FAIL:-}" = "1" ]; then
        printf '%s\\n' 'fake tmux respawn failure' >&2
        exit 1
    fi
    exit 0
fi
printf '%s\\n' "$@" > "$KANBAN_TMUX_LOG"
if [ "${KANBAN_TMUX_FAIL:-}" = "1" ]; then
    printf '%s\\n' 'fake tmux failure' >&2
    exit 1
fi
printf '%s\t%s\\n' '@9' '%9'
""",
            encoding="utf-8",
        )
        tmux.chmod(0o755)
        for name in ("codex", "claude", "grok", "cursor-agent"):
            agent = fake_bin / name
            agent.write_text(
                """#!/bin/sh
if [ "$1" = "create-chat" ]; then
    if [ "${KANBAN_CURSOR_CHAT_FAIL:-}" = "1" ]; then
        printf '%s\\n' 'fake create-chat failure' >&2
        exit 1
    fi
    printf '%s\\n' "${KANBAN_CURSOR_CHAT_ID:-chat-fake-0001}"
    exit 0
fi
exit "${KANBAN_AGENT_EXIT:-0}"
""",
                encoding="utf-8",
            )
            agent.chmod(0o755)
        self.env["PATH"] = str(fake_bin) + os.pathsep + self.env.get("PATH", "")
        self.env["TMUX"] = "/tmp/fake-tmux,1,0"
        self.env["TMUX_PANE"] = "%7"
        self.env["KANBAN_TMUX_LOG"] = str(self.root / "tmux.log")
        return fake_bin

    def install_fake_herdr(self) -> Path:
        fake_bin = self.install_fake_launchers()
        herdr = fake_bin / "herdr"
        herdr.write_text(
            """#!/bin/sh
if [ "$1" = "tab" ] && [ "$2" = "create" ]; then
    printf '%s\\n' "$@" > "$KANBAN_HERDR_LOG.create"
    printf '%s\\n' "$1 $2" >> "$KANBAN_HERDR_LOG.order"
    if [ "${KANBAN_HERDR_CREATE_FAIL:-}" = "1" ]; then
        printf '%s\\n' 'fake herdr tab create failure' >&2
        exit 1
    fi
    if [ -n "${KANBAN_HERDR_CREATE_JSON:-}" ]; then
        printf '%s\\n' "$KANBAN_HERDR_CREATE_JSON"
        exit 0
    fi
    printf '%s\\n' '{"id":"cli:tab:create","result":{"type":"tab_created","tab":{"tab_id":"w1:t9","workspace_id":"w1","number":1,"label":"kb","focused":true,"pane_count":1,"agent_status":"idle"},"root_pane":{"pane_id":"w1:p9","terminal_id":"term1","workspace_id":"w1","tab_id":"w1:t9","focused":true,"agent_status":"idle","revision":1}}}'
    exit 0
fi
if [ "$1" = "pane" ] && [ "$2" = "wait-output" ]; then
    printf '%s\\n' "$@" > "$KANBAN_HERDR_LOG.wait"
    printf '%s\\n' "$1 $2" >> "$KANBAN_HERDR_LOG.order"
    if [ "${KANBAN_HERDR_WAIT_FAIL:-}" = "1" ]; then
        printf '%s\\n' 'fake herdr pane wait-output timeout' >&2
        exit 1
    fi
    if [ "${KANBAN_HERDR_ACK_FAIL:-}" = "1" ] && [ "$7" = "recent" ]; then
        printf '%s\\n' 'fake herdr acknowledgement timeout' >&2
        exit 1
    fi
    exit 0
fi
if [ "$1" = "pane" ] && [ "$2" = "send-keys" ]; then
    printf '%s\\n' "$@" > "$KANBAN_HERDR_LOG.send"
    printf '%s\\n' "$1 $2" >> "$KANBAN_HERDR_LOG.order"
    if [ "$#" -ne 4 ] || [ "$4" != "Enter" ]; then
        printf '%s\\n' "unsupported key ${4:-}" >&2
        exit 1
    fi
    if [ "${KANBAN_HERDR_SEND_FAIL:-}" = "1" ]; then
        printf '%s\\n' 'fake herdr send-keys failure' >&2
        exit 1
    fi
    exit 0
fi
if [ "$1" = "agent" ] && [ "$2" = "prompt" ]; then
    printf '%s\\n' "$@" > "$KANBAN_HERDR_LOG.prompt"
    printf '%s\\n' "$1 $2" >> "$KANBAN_HERDR_LOG.order"
    if [ "${KANBAN_HERDR_PROMPT_FAIL:-}" = "1" ]; then
        printf '%s\\n' 'fake herdr agent prompt failure' >&2
        exit 1
    fi
    exit 0
fi
if [ "$1" = "pane" ] && [ "$2" = "run" ]; then
    printf '%s\\n' "$@" > "$KANBAN_HERDR_LOG.run"
    printf '%s\\n' "$1 $2" >> "$KANBAN_HERDR_LOG.order"
    if [ "${KANBAN_HERDR_RUN_FAIL:-}" = "1" ]; then
        printf '%s\\n' 'fake herdr pane run failure' >&2
        if [ "${KANBAN_HERDR_CLOSE_CRASH:-}" = "1" ]; then
            /bin/rm -f "$0"
        fi
        exit 1
    fi
    exit 0
fi
if [ "$1" = "pane" ] && [ "$2" = "get" ]; then
    printf '%s\n' "$@" > "$KANBAN_HERDR_LOG.get"
    printf '%s\n' "$1 $2" >> "$KANBAN_HERDR_LOG.order"
    if [ "${KANBAN_HERDR_GET_FAIL:-}" = "1" ]; then
        printf '%s\n' 'fake pane not found' >&2
        exit 1
    fi
    printf '{"id":"cli:pane:get","result":{"type":"pane_info","pane":{"pane_id":"%s","tab_id":"w1:t9","agent":"%s","agent_status":"%s","agent_session":{"value":"%s"}}}}\n' \
        "$3" "${KANBAN_HERDR_AGENT:-codex}" "${KANBAN_HERDR_STATUS:-idle}" "${KANBAN_HERDR_SESSION:-}"
    exit 0
fi
if [ "$1" = "pane" ] && [ "$2" = "read" ]; then
    printf '%s\n' "${KANBAN_HERDR_OUTPUT:-}"
    exit 0
fi
if [ "$1" = "pane" ] && [ "$2" = "list" ]; then
    printf '%s\n' "$@" > "$KANBAN_HERDR_LOG.list"
    printf '%s\n' "$1 $2" >> "$KANBAN_HERDR_LOG.order"
    if [ "${KANBAN_HERDR_LIST_FAIL:-}" = "1" ]; then
        printf '%s\n' 'fake pane list failure' >&2
        exit 1
    fi
    if [ -n "${KANBAN_HERDR_LIST_JSON:-}" ]; then
        printf '%s\n' "$KANBAN_HERDR_LIST_JSON"
        exit 0
    fi
    printf '{"id":"cli:pane:list","result":{"type":"pane_list","panes":[{"pane_id":"w1:p9","tab_id":"w1:t9","agent":"%s","agent_status":"idle","agent_session":{"kind":"id","source":"herdr:%s","value":"%s"}}]}}\n' \
        "${KANBAN_HERDR_AGENT:-codex}" "${KANBAN_HERDR_AGENT:-codex}" "${KANBAN_HERDR_SESSION:-}"
    exit 0
fi
if [ "$1" = "tab" ] && [ "$2" = "close" ]; then
    printf '%s\\n' "$@" > "$KANBAN_HERDR_LOG.close"
    [ "${KANBAN_HERDR_CLOSE_FAIL:-}" = "1" ] && exit 1
    exit 0
fi
printf '%s\\n' "unexpected herdr args: $*" >&2
exit 1
""",
            encoding="utf-8",
        )
        herdr.chmod(0o755)
        # 测试 PATH 只含假二进制, 避免本机 herdr 改变行为.
        self.env["PATH"] = str(fake_bin)
        self.env["HERDR_ENV"] = "1"
        self.env["HERDR_WORKSPACE_ID"] = "w1"
        self.env["KANBAN_HERDR_LOG"] = str(self.root / "herdr.log")
        return fake_bin

    def herdr_arguments(self, suffix: str) -> list[str]:
        return (self.root / f"herdr.log.{suffix}").read_text(encoding="utf-8").splitlines()

    @contextmanager
    def fake_herdr_socket(
        self,
        response_type: str = "ok",
        response_chunks: int = 1,
        chunk_delay: float = 0.0,
    ):
        socket_path = self.root / "herdr.sock"
        requests = []
        stop = threading.Event()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(socket_path))
        server.listen()
        server.settimeout(0.1)

        def serve() -> None:
            while not stop.is_set():
                try:
                    connection, _address = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                with connection:
                    data = b""
                    while b"\n" not in data:
                        chunk = connection.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    request = json.loads(data.splitlines()[0])
                    requests.append(request)
                    response = {
                        "id": request["id"],
                        "result": {"type": response_type},
                    }
                    encoded = (json.dumps(response) + "\n").encode("utf-8")
                    chunk_size = max(1, math.ceil(len(encoded) / response_chunks))
                    try:
                        for offset in range(0, len(encoded), chunk_size):
                            if chunk_delay:
                                time.sleep(chunk_delay)
                            connection.sendall(encoded[offset:offset + chunk_size])
                    except OSError:
                        pass

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        previous = self.env.get("HERDR_SOCKET_PATH")
        self.env["HERDR_SOCKET_PATH"] = str(socket_path)
        try:
            yield requests
        finally:
            if previous is None:
                self.env.pop("HERDR_SOCKET_PATH", None)
            else:
                self.env["HERDR_SOCKET_PATH"] = previous
            stop.set()
            server.close()
            thread.join(timeout=1)

    def test_locale_selects_chinese_or_english(self) -> None:
        chinese = self.run_command("--help")
        self.assertIn("本地文件看板", chinese.stdout)
        self.assertIn("--lang {cn,en}", chinese.stdout)
        self.assertNotIn("usage:", chinese.stdout)
        self.assertEqual("通过: 0 个任务\n", self.run_command("check").stdout)

        chinese_error = self.run_command("nope", succeeds=False)
        self.assertIn("参数 命令: 无效选择", chinese_error.stderr)
        self.assertNotIn("argument command", chinese_error.stderr)

        self.assertIn("项目路径", self.run_command("init", "--help").stdout)
        self.assertIn("任务", self.run_command("show", "--help").stdout)
        self.assertIn("标题", self.run_command("new", "--help").stdout)
        option_error = self.run_command("list", "--mobile=foo", succeeds=False)
        self.assertIn("不接受显式参数 'foo'", option_error.stderr)
        self.assertNotIn("ignored explicit argument", option_error.stderr)

        self.env["ONEVOKE_LANG"] = "en"
        english = self.run_command("--help")
        self.assertIn("Local file kanban board", english.stdout)
        self.assertNotIn("本地文件看板", english.stdout)

        forced_chinese = self.run_command("--lang", "cn", "--help")
        self.assertIn("本地文件看板", forced_chinese.stdout)
        self.env["ONEVOKE_LANG"] = "zh"
        forced_english = self.run_command("--lang", "en", "--help")
        self.assertIn("Local file kanban board", forced_english.stdout)
        invalid = self.run_command("--lang", "fr", "--help", succeeds=False)
        self.assertIn("无效选择", invalid.stderr)
        missing = self.run_command("--lang", succeeds=False)
        self.assertIn("需要一个参数", missing.stderr)

        self.env["ONEVOKE_LANG"] = "en"
        rejected = self.run_command(
            "new", "chore", "Bad-Slug", "title", succeeds=False
        )
        self.assertIn("slug may contain only lowercase ASCII", rejected.stderr)

        checked = self.run_command("check")
        self.assertEqual("ok: 0 tasks\n", checked.stdout)

    def write_onevoke_config(
        self,
        agent: str,
        launcher: str,
        *,
        welcome_complete: bool = True,
        models: Optional[dict] = None,
        kanban_agents: Optional[dict] = None,
    ) -> None:
        config = self.home / ".config" / "onevoke" / "config.json"
        config.parent.mkdir(parents=True)
        payload = {
            "schema_version": 1,
            "welcome_complete": welcome_complete,
            "kanban_agent": agent,
            "launcher": launcher,
            "reviewers": {
                "PM": "codex",
                "CSA": "codex",
                "Hacker": "codex",
                "QA": "codex",
            },
            "memsearch": {"enabled": False},
        }
        if models is not None:
            payload["models"] = models
        if kanban_agents is not None:
            payload["kanban_agents"] = kanban_agents
        config.write_text(json.dumps(payload), encoding="utf-8")

    def test_small_and_large_lifecycle(self) -> None:
        today = datetime.now().strftime("%Y%m%d")
        small_id = f"{today}-small-fix-task"
        self.run_command("new", "bug", "small-fix", "修复小问题")
        small = self.root / "backlog" / f"{small_id}.md"
        self.run_command("move", small_id, "todo", succeeds=False)
        self.make_ready(small)
        self.run_command("move", small_id, "todo")
        self.run_command("move", small_id, "working")
        small = self.root / "working" / f"{small_id}.md"
        self.complete(small)
        self.run_command("move", small_id, "done")

        large_id = f"{today}-large-feature-task"
        self.run_command("new", "--large", "feature", "large-feature", "大型功能")
        spec = self.root / "backlog" / large_id / "spec.md"
        self.make_ready(spec)
        self.run_command("move", large_id, "todo")
        self.run_command("move", large_id, "working")
        spec = self.root / "working" / large_id / "spec.md"
        self.complete(spec)
        spec.write_text(
            spec.read_text(encoding="utf-8").replace("- 完成时间:\n", "", 1),
            encoding="utf-8",
        )
        self.run_command("move", large_id, "done", succeeds=False)
        (spec.parent / "report.md").write_text("# 完成报告\n\n验证通过.\n", encoding="utf-8")
        self.run_command("move", large_id, "done")

        listing = self.run_command("list", "done").stdout
        self.assertIn(small_id, listing)
        self.assertIn(large_id, listing)
        self.assertRegex(listing, r"done\s+small\s+\d{4}-\d{2}-\d{2} \d{2}:\d{2}")
        completed = self.root / "done" / small.name
        self.assertRegex(
            completed.read_text(encoding="utf-8"),
            r"(?m)^- 完成时间: \d{4}-\d{2}-\d{2} \d{2}:\d{2}$",
        )
        self.assertIn(
            "- 完成时间: ",
            (self.root / "done" / large_id / "spec.md").read_text(encoding="utf-8"),
        )
        self.assertEqual("通过: 0 个任务\n", self.run_command("check").stdout)
        self.assertEqual("通过: 2 个任务\n", self.run_command("check", "--all").stdout)

    def test_new_templates_include_optional_task_group_metadata(self) -> None:
        today = datetime.now().strftime("%Y%m%d")
        self.run_command("new", "chore", "small-group-field", "小任务组字段")
        self.run_command(
            "new", "--large", "chore", "large-group-field", "大任务组字段"
        )

        small = self.root / "backlog" / f"{today}-small-group-field-task.md"
        large = self.root / "backlog" / f"{today}-large-group-field-task" / "spec.md"
        for document in (small, large):
            text = document.read_text(encoding="utf-8")
            self.assertEqual(1, text.count("- 任务组:\n"))
            self.assertIn("- 类型: Chore\n- 任务组:\n- 创建时间:", text)

    def test_pick_moves_only_ready_backlog_task_to_todo(self) -> None:
        task_id = f"{datetime.now().strftime('%Y%m%d')}-pick-task"
        self.run_command("new", "chore", "pick", "挑选任务")
        task = self.root / "backlog" / f"{task_id}.md"

        result = self.run_command("pick", task_id, succeeds=False)
        self.assertIn("任务未满足 todo 条件", result.stderr)
        self.make_ready(task)
        self.run_command("pick", task_id)

        self.assertTrue((self.root / "todo" / task.name).exists())
        result = self.run_command("pick", task_id, succeeds=False)
        self.assertIn("不允许迁移: todo -> todo", result.stderr)

    def test_pick_without_task_prompts_for_backlog_selection(self) -> None:
        first_id = f"{datetime.now().strftime('%Y%m%d')}-alpha-pick-task"
        second_id = f"{datetime.now().strftime('%Y%m%d')}-beta-pick-task"
        self.run_command("new", "chore", "alpha-pick", "第一个任务")
        self.run_command("new", "chore", "beta-pick", "第二个任务")
        first = self.root / "backlog" / f"{first_id}.md"
        second = self.root / "backlog" / f"{second_id}.md"
        self.make_ready(first)
        self.make_ready(second)

        result = self.run_command("pick", input_text="2\n")

        self.assertIn(f"1. {first_id}", result.stdout)
        self.assertIn(f"2. {second_id}", result.stdout)
        self.assertTrue(first.exists())
        self.assertTrue((self.root / "todo" / second.name).exists())

    def test_pick_without_task_rejects_empty_backlog(self) -> None:
        result = self.run_command("pick", succeeds=False)

        self.assertIn("backlog 中没有任务", result.stderr)

    def test_done_metadata_error_keeps_task_in_working(self) -> None:
        task_id, task = self.make_todo("bad-done-metadata")
        self.run_command("move", task_id, "working")
        task = self.root / "working" / task.name
        self.complete(task)
        text = task.read_text(encoding="utf-8").replace(
            "- 完成时间:\n", "- 完成时间:\n- 完成时间:\n", 1
        )
        task.write_text(text, encoding="utf-8")

        result = self.run_command("move", task_id, "done", succeeds=False)

        self.assertIn("缺少唯一元数据字段: 完成时间", result.stderr)
        self.assertTrue(task.exists())
        self.assertFalse((self.root / "done" / task.name).exists())

    def test_done_write_error_restores_working_task_unchanged(self) -> None:
        task_id, task = self.make_todo("done-write-error")
        self.run_command("move", task_id, "working")
        task = self.root / "working" / task.name
        self.complete(task)
        original = task.read_text(encoding="utf-8")
        sys.path.insert(0, str(COMMAND.parent))
        try:
            kanban = runpy.run_path(str(COMMAND), run_name="kanban_test")
        finally:
            sys.path.pop(0)
        entry = kanban["Entry"](task_id, "working", task, task, "small")

        with mock.patch.object(kanban["os"], "replace", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(kanban["KanbanError"], "记录完成时间失败"):
                kanban["move_entry"](entry, self.root, "done")

        self.assertEqual(original, task.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "done" / task.name).exists())

    def test_list_formats_aligned_table(self) -> None:
        task_id = f"{datetime.now().strftime('%Y%m%d')}-list-table-task"
        self.run_command("new", "chore", "list-table", "表格输出")
        large_id = f"{datetime.now().strftime('%Y%m%d')}-list-large-task"
        self.run_command("new", "--large", "chore", "list-large", "大型表格输出")
        self.env.pop("NO_COLOR", None)
        self.env["CLICOLOR_FORCE"] = "1"
        self.env["COLORFGBG"] = "15;0"

        output = self.run_command("list", "backlog").stdout
        plain = re.sub(r"\033\[[0-9;]*m", "", output)

        lines = plain.splitlines()
        self.assertEqual("状态     规模   时间  任务 ID / 标题", lines[0])
        self.assertIn(f"backlog  small  -     {task_id}  表格输出", plain)
        self.assertIn(f"backlog  large  -     {large_id}  大型表格输出", plain)
        self.assertIn("\033[90mbacklog", output)
        self.assertIn("\033[90msmall", output)
        self.assertIn("\033[1;95mlarge", output)
        self.assertIn(f"\033[96m{task_id}", output)
        self.assertIn("\033[95m表格输出", output)
        self.assertNotIn("\t", output)

        def display_width(text: str) -> int:
            return sum(
                0 if unicodedata.combining(char) else
                2 if unicodedata.east_asian_width(char) in "WF" else 1
                for char in text
            )

        row = next(line for line in lines if task_id in line)
        for heading, value in (("规模", "small"), ("时间", "-"), ("任务 ID", task_id)):
            self.assertEqual(
                display_width(lines[0][: lines[0].index(heading)]),
                display_width(row[: row.index(value)]),
            )

    def test_list_mobile_formats_each_task_as_vertical_block(self) -> None:
        task_id = f"{datetime.now().strftime('%Y%m%d')}-list-mobile-task"
        self.run_command("new", "chore", "list-mobile", "手机竖屏输出")

        output = self.run_command("list", "--mobile", "backlog").stdout

        self.assertEqual(
            ["backlog  small  -", task_id, "手机竖屏输出"],
            output.splitlines(),
        )

    def test_list_accepts_empty_state(self) -> None:
        self.assertEqual("", self.run_command("list", "--mobile", "done").stdout)
        self.assertIn("状态", self.run_command("list", "done").stdout)

    def test_list_uses_document_mtime_for_legacy_done_task(self) -> None:
        task_id = f"{datetime.now().strftime('%Y%m%d')}-legacy-done-task"
        task = self.root / "done" / f"{task_id}.md"
        task.write_text("# 历史任务\n", encoding="utf-8")
        modified = datetime(2024, 1, 2, 3, 4).timestamp()
        os.utime(task, (modified, modified))

        output = self.run_command("list", "done").stdout

        self.assertIn("2024-01-02 03:04", output)

    def test_list_groups_states_and_sorts_each_group_by_time_descending(self) -> None:
        today = datetime.now().strftime("%Y%m%d")
        tasks = (
            ("backlog", "backlog-old", "", ""),
            ("backlog", "backlog-new", "", ""),
            ("working", "working-old", "2024-01-01 10:00", ""),
            ("working", "working-new", "2024-01-02 10:00", ""),
            ("done", "done-old", "", "2024-01-03 10:00"),
            ("done", "done-new", "", "2024-01-04 10:00"),
        )
        for state, slug, started, completed in tasks:
            (self.root / state / f"{today}-{slug}-task.md").write_text(
                f"# {slug}\n- 开始时间: {started}\n- 完成时间: {completed}\n",
                encoding="utf-8",
            )

        output = self.run_command("list").stdout

        self.assertEqual(
            [
                f"{today}-backlog-old-task",
                f"{today}-backlog-new-task",
                f"{today}-working-new-task",
                f"{today}-working-old-task",
                f"{today}-done-new-task",
                f"{today}-done-old-task",
            ],
            re.findall(rf"{today}-[a-z-]+-task", output),
        )

    def test_list_adapts_all_colors_to_background(self) -> None:
        today = datetime.now().strftime("%Y%m%d")
        for state in STATES:
            (self.root / state / f"{today}-list-{state}-task.md").write_text(
                f"# 状态 {state}\n", encoding="utf-8"
            )
        self.env.pop("NO_COLOR", None)
        self.env["CLICOLOR_FORCE"] = "1"

        self.env["COLORFGBG"] = "15;0"
        dark = self.run_command("list").stdout
        self.env["COLORFGBG"] = "0;15"
        light = self.run_command("list").stdout

        for state, code in zip(STATES, ("90", "93", "96", "95", "92", "94", "91")):
            self.assertIn(f"\033[{code}m{state}", dark)
        for state, code in zip(STATES, ("30", "33", "34", "35", "32", "35", "31")):
            self.assertIn(f"\033[{code}m{state}", light)
        self.assertIn("\033[90msmall", dark)
        self.assertIn(f"\033[96m{today}", dark)
        self.assertIn("\033[95m状态", dark)
        self.assertIn("\033[30msmall", light)
        self.assertIn(f"\033[34m{today}", light)
        self.assertIn("\033[35m状态", light)

    def make_large_todo(self, slug: str) -> str:
        task_id = f"{datetime.now().strftime('%Y%m%d')}-{slug}-task"
        self.run_command("new", "--large", "chore", slug, f"大任务 {slug}")
        self.make_ready(self.root / "backlog" / task_id / "spec.md")
        self.run_command("pick", task_id)
        return task_id

    def last_launch_command(self) -> str:
        return (self.root / "tmux.log.command").read_text(encoding="utf-8").rstrip("\n")

    @staticmethod
    def set_branch(path: Path, branch: str) -> None:
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("- 任务分支:\n", f"- 任务分支: {branch}\n", 1), encoding="utf-8")

    def make_herdr_review(self, slug: str, agent: str = "claude") -> tuple[str, Path]:
        self.install_fake_herdr()
        task_id, task = self.make_todo(slug)
        self.run_command("start", "--launcher", "herdr", "--agent", agent, task_id)
        working = self.root / "working" / task.name
        session = re.search(rf"(?m)^- 会话: {agent} (\S+)$", working.read_text(encoding="utf-8"))
        if session is not None:
            self.env["KANBAN_HERDR_SESSION"] = session.group(1)
        self.env["KANBAN_HERDR_AGENT"] = agent
        self.set_branch(working, slug)
        self.run_command("move", task_id, "review")
        return task_id, self.root / "review" / task.name

    def test_new_template_includes_session_and_window_fields(self) -> None:
        task_id, task = self.make_todo("template-session")
        text = task.read_text(encoding="utf-8")
        self.assertIn("- 负责人:\n- 会话:\n- 窗口:\n- 开始时间:\n", text)

    def test_start_selects_agent_by_task_scale(self) -> None:
        fake_bin = self.install_fake_launchers()
        self.write_onevoke_config("codex", "tmux", kanban_agents={"large": "codex", "small": "cursor"})
        small_id, small = self.make_todo("scale-small")
        large_id = self.make_large_todo("scale-large")

        small_result = self.run_command("start", small_id)
        small_command = self.last_launch_command()
        large_result = self.run_command("start", large_id)
        large_command = self.last_launch_command()

        self.assertIn("规模=小任务\tAgent=cursor", small_result.stdout)
        self.assertIn(str(fake_bin / "cursor-agent"), small_command)
        self.assertIn("--model cursor-grok-4.6-high", small_command)
        self.assertIn("--resume chat-fake-0001", small_command)
        small_text = (self.root / "working" / small.name).read_text(encoding="utf-8")
        self.assertIn("- 负责人: cursor\n- 会话: cursor chat-fake-0001\n- 窗口: tmux:$42:@9:%9\n", small_text)

        self.assertIn("规模=大任务\tAgent=codex", large_result.stdout)
        self.assertIn(str(fake_bin / "codex"), large_command)
        self.assertIn('model_reasoning_effort="high"', large_command)
        self.assertNotIn("resume", large_command)
        large_text = (self.root / "working" / large_id / "spec.md").read_text(encoding="utf-8")
        self.assertIn("- 负责人: codex\n- 会话: codex\n- 窗口: tmux:$42:@9:%9\n", large_text)

    def test_start_agent_option_overrides_scale_selection(self) -> None:
        self.install_fake_launchers()
        self.write_onevoke_config("codex", "tmux", kanban_agents={"large": "codex", "small": "cursor"})
        task_id, _ = self.make_todo("scale-override")

        result = self.run_command("start", "--agent", "grok", task_id)

        self.assertIn("Agent=grok", result.stdout)
        self.assertIn("--permission-mode bypassPermissions", self.last_launch_command())

    def test_start_records_claude_session_id_for_resume(self) -> None:
        self.install_fake_launchers()
        task_id, task = self.make_todo("claude-session")

        self.run_command("start", "--agent", "claude", task_id)

        command = self.last_launch_command()
        match = re.search(r"--session-id ([0-9a-f-]{36})", command)
        self.assertIsNotNone(match, command)
        text = (self.root / "working" / task.name).read_text(encoding="utf-8")
        self.assertIn(f"- 会话: claude {match.group(1)}\n", text)
        self.assertNotIn("--resume", command)

    def test_start_fills_session_into_legacy_cards_without_the_field(self) -> None:
        self.install_fake_launchers()
        task_id, task = self.make_todo("legacy-session")
        task.write_text(
            task.read_text(encoding="utf-8")
            .replace("- 会话:\n", "", 1)
            .replace("- 窗口:\n", "", 1),
            encoding="utf-8",
        )

        self.run_command("start", "--agent", "grok", task_id)

        text = (self.root / "working" / task.name).read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^- 负责人: grok\n- 会话: grok [0-9a-f-]{36}\n- 窗口: tmux:\$42:@9:%9$")

    def test_start_cursor_create_chat_failure_does_not_claim(self) -> None:
        self.install_fake_launchers()
        self.env["KANBAN_CURSOR_CHAT_FAIL"] = "1"
        task_id, task = self.make_todo("cursor-chat-fail")

        result = self.run_command("start", "--agent", "cursor", task_id, succeeds=False)

        self.assertIn("create-chat", result.stderr)
        self.assertTrue(task.exists())
        self.assertIn("- 负责人:\n", task.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "tmux.log").exists())

    def test_review_state_sits_between_working_and_done(self) -> None:
        task_id, task = self.make_todo("review-flow")
        self.run_command("move", task_id, "working")
        working = self.root / "working" / task.name

        rejected = self.run_command("move", task_id, "review", succeeds=False)
        self.assertIn("任务分支", rejected.stderr)
        self.set_branch(working, "review-flow")
        self.run_command("move", task_id, "review")
        review = self.root / "review" / task.name
        self.assertTrue(review.exists())
        self.assertIn("review", self.run_command("list", "review").stdout)

        # 修复轮次迁回 working, 再回到 review; 完成后从 review 直接进 done.
        self.run_command("move", task_id, "working")
        self.run_command("move", task_id, "review")
        self.run_command("move", task_id, "done", succeeds=False)
        self.complete(review)
        self.run_command("move", task_id, "done")
        self.assertTrue((self.root / "done" / task.name).exists())
        self.run_command("check")

    def test_review_cannot_be_entered_from_todo_or_backlog(self) -> None:
        task_id, _ = self.make_todo("review-skip")
        rejected = self.run_command("move", task_id, "review", succeeds=False)
        self.assertIn("todo -> review", rejected.stderr.replace("todo -> review", "todo -> review"))

    def test_resume_relaunches_claude_with_its_original_session(self) -> None:
        fake_bin = self.install_fake_launchers()
        task_id, task = self.make_todo("resume-claude")
        self.run_command("start", "--agent", "claude", task_id)
        session_id = re.search(r"--session-id ([0-9a-f-]{36})", self.last_launch_command()).group(1)
        working = self.root / "working" / task.name
        self.set_branch(working, "resume-claude")
        self.run_command("move", task_id, "review")

        result = self.run_command("resume", task_id, "--message", "QA finding: 补齐空输入校验")

        self.assertIn(f"已唤醒: {task_id}\t规模=小任务\tAgent=claude", result.stdout)
        self.assertTrue(working.exists())
        self.assertFalse((self.root / "review" / task.name).exists())
        command = self.last_launch_command()
        self.assertIn(str(fake_bin / "claude"), command)
        self.assertIn(f"--resume {session_id}", command)
        self.assertNotIn("--session-id", command)
        self.assertIn("--dangerously-skip-permissions", command)
        self.assertIn("--effort medium", command)
        self.assertIn(f"继续 Kanban 任务 {task_id}", command)
        self.assertIn("QA finding: 补齐空输入校验", command)
        self.assertIn(f"kanban show {task_id}", command)
        tmux_args = (self.root / "tmux.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual("kb-任务-resume-claude", tmux_args[tmux_args.index("-n") + 1])

    def test_resume_reads_the_message_from_a_file(self) -> None:
        self.install_fake_launchers()
        task_id, task = self.make_todo("resume-file")
        self.run_command("start", "--agent", "grok", task_id)
        working = self.root / "working" / task.name
        self.set_branch(working, "resume-file")
        self.run_command("move", task_id, "review")
        findings = self.root / "findings.md"
        findings.write_text("- [QA][high] 越界读取\n- [PM][medium] 缺少验收 3\n", encoding="utf-8")

        self.run_command("resume", task_id, "--message-file", str(findings))

        command = self.last_launch_command()
        self.assertIn("越界读取", command)
        self.assertIn("缺少验收 3", command)
        self.assertRegex(command, r"--resume [0-9a-f-]{36}")

    def test_resume_requires_exactly_one_message_source(self) -> None:
        self.install_fake_launchers()
        task_id, task = self.make_todo("resume-args")
        self.run_command("start", "--agent", "grok", task_id)
        self.set_branch(self.root / "working" / task.name, "resume-args")
        self.run_command("move", task_id, "review")

        neither = self.run_command("resume", task_id, succeeds=False)
        both = self.run_command("resume", task_id, "--message", "x", "--message-file", "y", succeeds=False)
        empty = self.run_command("resume", task_id, "--message", "  ", succeeds=False)
        bad_timeout = self.run_command("resume", task_id, "--message", "x", "--timeout", "nan", succeeds=False)
        short_timeout = self.run_command("resume", task_id, "--message", "x", "--timeout", "60", succeeds=False)

        for result in (neither, both):
            self.assertIn("--message", result.stderr)
        self.assertIn("不得为空", empty.stderr)
        self.assertIn("超时", bad_timeout.stderr)
        self.assertIn("大于 60", short_timeout.stderr)
        self.assertTrue((self.root / "review" / task.name).exists())

    def test_resume_finds_the_codex_session_from_rollouts(self) -> None:
        fake_bin = self.install_fake_launchers()
        task_id, task = self.make_todo("resume-codex")
        other_id, _ = self.make_todo("resume-codex-other")
        self.run_command("start", task_id)
        self.set_branch(self.root / "working" / task.name, "resume-codex")
        self.run_command("move", task_id, "review")
        codex_home = self.root / "codex-home"
        day = codex_home / "sessions" / "2026" / "09" / "01"
        day.mkdir(parents=True)

        def rollout(name: str, session_id: str, prompt: str) -> Path:
            path = day / name
            lines = [
                {"timestamp": "t", "type": "session_meta", "payload": {"id": session_id, "cwd": "/p"}},
                {"timestamp": "t", "type": "response_item", "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "perm"}]}},
                {"timestamp": "t", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": prompt}]}},
            ]
            path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
            return path

        decoy = rollout("rollout-2026-09-01T10-00-00-aaaa.jsonl", "aaaaaaaa-0000-0000-0000-000000000001", f"执行 Kanban 任务 {other_id}.")
        # 主控自己的会话只是提到任务 ID, 不是 start prompt, 即使更新也不能被选中.
        orchestrator = rollout("rollout-2026-09-01T11-00-00-cccc.jsonl", "cccccccc-0000-0000-0000-000000000003", f"请用 kanban start 启动 {task_id} 并跟踪")
        target = rollout("rollout-2026-09-01T09-00-00-bbbb.jsonl", "bbbbbbbb-0000-0000-0000-000000000002", f"执行 Kanban 任务 {task_id}. 先运行 kanban rules")
        os.utime(target, (1_700_000_000, 1_700_000_000))
        os.utime(decoy, (1_700_000_500, 1_700_000_500))
        os.utime(orchestrator, (1_700_000_900, 1_700_000_900))
        self.env["CODEX_HOME"] = str(codex_home)

        result = self.run_command("resume", task_id, "--message", "PM finding: 验收 2 未闭环")

        self.assertIn("Agent=codex", result.stdout)
        command = self.last_launch_command()
        self.assertIn(f"{fake_bin / 'codex'} resume", command)
        self.assertIn("bbbbbbbb-0000-0000-0000-000000000002", command)
        self.assertNotIn("aaaaaaaa-0000-0000-0000-000000000001", command)
        self.assertNotIn("cccccccc-0000-0000-0000-000000000003", command)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertIn('model_reasoning_effort="medium"', command)
        self.assertTrue((self.root / "working" / task.name).exists())

    def test_resume_without_a_codex_rollout_keeps_the_card_in_review(self) -> None:
        self.install_fake_launchers()
        task_id, task = self.make_todo("resume-codex-missing")
        self.run_command("start", task_id)
        self.set_branch(self.root / "working" / task.name, "resume-codex-missing")
        self.run_command("move", task_id, "review")
        self.env["CODEX_HOME"] = str(self.root / "empty-codex-home")
        (self.root / "empty-codex-home" / "sessions").mkdir(parents=True)

        result = self.run_command("resume", task_id, "--message", "x", succeeds=False)

        self.assertIn(task_id, result.stderr)
        self.assertIn("Codex", result.stderr)
        self.assertTrue((self.root / "review" / task.name).exists())

    def test_resume_rejects_wrong_states_and_cards_without_a_session(self) -> None:
        self.install_fake_launchers()
        todo_id, _ = self.make_todo("resume-todo")
        manual_id, manual = self.make_todo("resume-manual")
        self.run_command("move", manual_id, "working")

        wrong_state = self.run_command("resume", todo_id, "--message", "x", succeeds=False)
        no_session = self.run_command("resume", manual_id, "--message", "x", succeeds=False)

        self.assertIn("review", wrong_state.stderr)
        self.assertIn("todo", wrong_state.stderr)
        self.assertIn("会话", no_session.stderr)
        self.assertTrue((self.root / "working" / manual.name).exists())

    def test_resume_launch_failure_moves_the_card_back_to_review(self) -> None:
        self.install_fake_launchers()
        task_id, task = self.make_todo("resume-rollback")
        self.run_command("start", "--agent", "claude", task_id)
        working = self.root / "working" / task.name
        self.set_branch(working, "resume-rollback")
        self.run_command("move", task_id, "review")
        before = (self.root / "review" / task.name).read_text(encoding="utf-8")
        self.env["KANBAN_TMUX_FAIL"] = "1"

        result = self.run_command("resume", task_id, "--message", "x", succeeds=False)

        self.assertIn("tmux new-window 失败", result.stderr)
        self.assertFalse(working.exists())
        self.assertEqual(before, (self.root / "review" / task.name).read_text(encoding="utf-8"))

    def test_resume_herdr_immediate_exit_reports_output_and_restores_review(self) -> None:
        kanban = self.load_kanban_module()
        task_id, review = self.make_herdr_review("resume-herdr-exit")
        before = review.read_text(encoding="utf-8")
        self.env["KANBAN_HERDR_STATUS"] = "done"
        self.env["KANBAN_HERDR_OUTPUT"] = "thread already has an active writer (code -32600)"
        args = argparse.Namespace(
            task=task_id,
            message="x",
            message_file=None,
            launcher="herdr",
            timeout=61,
        )

        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch.object(kanban.time, "monotonic", side_effect=(0.0, 62.0)):
                with mock.patch.object(kanban.time, "sleep"):
                    with self.assertRaisesRegex(kanban.KanbanError, "active writer"):
                        kanban.command_resume(args, self.root)

        self.assertEqual(before, review.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "working" / review.name).exists())
        self.assertEqual(["tab", "close", "w1:t9"], self.herdr_arguments("close"))

    def test_resume_console_immediate_exit_keeps_working_card_in_place(self) -> None:
        kanban = self.load_kanban_module()
        self.install_fake_launchers()
        task_id, task = self.make_todo("resume-console-exit")
        self.run_command("start", "--agent", "grok", task_id)
        working = self.root / "working" / task.name
        before = working.read_text(encoding="utf-8")
        process = mock.Mock(returncode=17)
        process.poll.return_value = 17
        args = argparse.Namespace(
            task=task_id,
            message="x",
            message_file=None,
            launcher="console",
            timeout=61,
        )

        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch.object(
                kanban,
                "prepare_launch",
                return_value=kanban.LaunchPlan("console", self.root.parent),
            ):
                with mock.patch.object(
                    kanban,
                    "launch_agent",
                    return_value=kanban.LaunchOutcome(process=process),
                ):
                    with self.assertRaisesRegex(kanban.KanbanError, "exit 17"):
                        kanban.command_resume(args, self.root)

        self.assertEqual(before, working.read_text(encoding="utf-8"))

    def test_resume_does_not_move_back_when_document_restore_fails(self) -> None:
        kanban = self.load_kanban_module()
        self.install_fake_launchers()
        task_id, task = self.make_todo("resume-restore-fail")
        self.run_command("start", "--agent", "claude", task_id)
        working = self.root / "working" / task.name
        self.set_branch(working, "resume-restore-fail")
        self.run_command("move", task_id, "review")
        args = argparse.Namespace(
            task=task_id,
            message="x",
            message_file=None,
            launcher="tmux",
            timeout=61,
        )

        def mutate_then_fail(_plan, _outcome, _session, _timeout):
            moved = self.root / "working" / task.name
            moved.write_text(moved.read_text(encoding="utf-8") + "\nAgent mutation\n", encoding="utf-8")
            raise kanban.KanbanError("liveness failed")

        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch.object(kanban, "launch_agent", return_value=kanban.LaunchOutcome()):
                with mock.patch.object(kanban, "validate_resumed_agent", side_effect=mutate_then_fail):
                    with mock.patch.object(kanban, "cleanup_failed_resume"):
                        with mock.patch.object(kanban, "write_text_atomic", side_effect=OSError("restore denied")):
                            with self.assertRaisesRegex(kanban.KanbanError, "卡片保留在 working"):
                                kanban.command_resume(args, self.root)

        self.assertFalse((self.root / "review" / task.name).exists())
        self.assertIn("Agent mutation", (self.root / "working" / task.name).read_text(encoding="utf-8"))

    def test_resume_failure_output_ignores_terminal_capture_errors(self) -> None:
        kanban = self.load_kanban_module()
        failure = subprocess.CompletedProcess([], 1, "", "can't find pane")
        cases = (
            (
                kanban.LaunchPlan("herdr", self.root.parent, herdr_bin="herdr"),
                kanban.LaunchOutcome(pane="w1:p9"),
                mock.patch.object(kanban, "herdr_capture", return_value=failure),
            ),
            (
                kanban.LaunchPlan("tmux", self.root.parent, tmux="tmux", session="$42"),
                kanban.LaunchOutcome(pane="%9"),
                mock.patch.object(kanban, "tmux_capture", return_value=failure),
            ),
        )

        for plan, outcome, capture in cases:
            with self.subTest(launcher=plan.launcher):
                with capture:
                    self.assertEqual("", kanban.resumed_agent_failure_output(plan, outcome))

    def test_resume_of_a_working_card_does_not_move_it(self) -> None:
        self.install_fake_launchers()
        task_id, task = self.make_todo("resume-working")
        self.run_command("start", "--agent", "grok", task_id)

        result = self.run_command("resume", task_id, "--message", "进程已退出, 请继续")

        self.assertIn("已唤醒", result.stdout)
        self.assertTrue((self.root / "working" / task.name).exists())
        self.assertRegex(self.last_launch_command(), r"--resume [0-9a-f-]{36}")

    def test_start_prompt_for_group_cards_stops_at_review(self) -> None:
        self.install_fake_launchers()
        task_id, task = self.make_todo("group-prompt")
        self.set_task_group(task, "20260901-demo-group")
        single_id, _ = self.make_todo("single-prompt")

        self.run_command("start", task_id)
        group_command = self.last_launch_command()
        self.run_command("start", single_id)
        single_command = self.last_launch_command()

        self.assertIn(f"kanban move {task_id} review", group_command)
        self.assertIn("汇入组集成分支", group_command)
        self.assertIn("由任务组主控负责", group_command)
        self.assertNotIn("审核、集成和看板收尾.", group_command)
        self.assertIn("审核、集成和看板收尾", single_command)
        self.assertNotIn("review", single_command)

    def test_resume_relaunches_cursor_with_its_chat(self) -> None:
        fake_bin = self.install_fake_launchers()
        task_id, task = self.make_todo("resume-cursor")
        self.env["KANBAN_CURSOR_CHAT_ID"] = "chat-resume-77"
        self.run_command("start", "--agent", "cursor", task_id)
        working = self.root / "working" / task.name
        self.assertIn("- 会话: cursor chat-resume-77\n", working.read_text(encoding="utf-8"))
        self.set_branch(working, "resume-cursor")
        self.run_command("move", task_id, "review")
        # resume 不得再建新 chat: 即使 create-chat 现在会失败也必须复用记录的 chat id.
        self.env["KANBAN_CURSOR_CHAT_FAIL"] = "1"

        result = self.run_command("resume", task_id, "--message", "QA finding: 修复空指针")

        self.assertIn("Agent=cursor", result.stdout)
        command = self.last_launch_command()
        self.assertIn(str(fake_bin / "cursor-agent"), command)
        self.assertIn("--resume chat-resume-77", command)
        self.assertIn("--model cursor-grok-4.6-high", command)
        self.assertIn("--trust", command)
        self.assertIn("--force", command)
        self.assertIn("QA finding: 修复空指针", command)
        self.assertTrue(working.exists())

    def test_notify_delivers_private_payload_and_waits_for_herdr_ack(self) -> None:
        task_id, review = self.make_herdr_review("notify-success")
        before = review.read_text(encoding="utf-8")
        source = self.root / "finding.md"
        source.write_text("QA finding: 补齐空输入校验\n第二行含 `code`", encoding="utf-8")

        result = self.run_command("notify", task_id, "--message-file", str(source), "--timeout", "61")

        working = self.root / "working" / review.name
        self.assertTrue(working.exists())
        self.assertEqual(before, working.read_text(encoding="utf-8"))
        self.assertIn("通道=herdr-direct", result.stdout)
        self.assertIn("回执=已确认", result.stdout)
        self.assertEqual(["pane", "get", "w1:p9"], self.herdr_arguments("get"))
        sent = self.herdr_arguments("prompt")
        self.assertEqual(["agent", "prompt", "w1:p9"], sent[:3])
        self.assertEqual(4, len(sent))
        self.assertTrue(sent[3].startswith("# onevoke-notify:"), sent[3])
        self.assertNotIn("QA finding", sent[3])
        self.assertFalse((self.root / "herdr.log.send").exists())
        # pane run 只属于 start 拉起 Agent 的那一次; 直投再走它会把正文留在输入栏.
        order = self.herdr_arguments("order")
        self.assertEqual(1, order.count("pane run"))
        self.assertEqual(1, order.count("agent prompt"))
        self.assertLess(order.index("pane run"), order.index("agent prompt"))
        self.assertNotIn("# onevoke-notify:", "\n".join(self.herdr_arguments("run")))
        waited = self.herdr_arguments("wait")
        self.assertEqual("recent", waited[waited.index("--source") + 1])
        self.assertRegex(waited[waited.index("--match") + 1], r"^ONEVOKE-NOTIFY-ACK:")
        match = re.search(r"消息文件=(\S+)", result.stdout)
        self.assertIsNotNone(match, result.stdout)
        message_path = Path(match.group(1))
        self.assertEqual("QA finding: 补齐空输入校验\n第二行含 `code`\n", message_path.read_text(encoding="utf-8"))
        self.assertEqual(0o600, stat.S_IMODE(message_path.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(message_path.parent.stat().st_mode))
        self.assertEqual(1, self.herdr_arguments("order").count("tab create"))
        message_path.unlink()
        message_path.parent.rmdir()

    def test_notify_delivers_to_tmux_after_identity_probe_and_ack(self) -> None:
        self.install_fake_launchers()
        task_id, task = self.make_todo("notify-tmux")
        self.run_command("start", "--launcher", "tmux", "--agent", "claude", task_id)
        working = self.root / "working" / task.name
        session_id = re.search(
            r"(?m)^- 会话: claude (\S+)$", working.read_text(encoding="utf-8")
        ).group(1)
        self.assertEqual(
            ["set-option", "-p", "-t", "%9", "@onevoke_session", session_id],
            (self.root / "tmux.log.pane-setopt").read_text(encoding="utf-8").splitlines(),
        )
        self.set_branch(working, "notify-tmux")
        self.run_command("move", task_id, "review")
        self.env["KANBAN_TMUX_CURRENT_COMMAND"] = "claude"

        result = self.run_command("notify", task_id, "--message", "tmux finding", "--timeout", "61")

        self.assertIn("通道=tmux-direct", result.stdout)
        self.assertIn("回执=已确认", result.stdout)
        self.assertTrue((self.root / "working" / task.name).exists())
        instruction = (self.root / "tmux.log.instruction").read_text(encoding="utf-8").strip()
        self.assertTrue(instruction.startswith("# onevoke-notify:"), instruction)
        self.assertNotIn("tmux finding", instruction)
        send = (self.root / "tmux.log.send").read_text(encoding="utf-8")
        self.assertIn("-l\n", send)
        self.assertIn("Enter\n", send)
        match = re.search(r"消息文件=(\S+)", result.stdout)
        message_path = Path(match.group(1))
        self.assertEqual("tmux finding\n", message_path.read_text(encoding="utf-8"))
        message_path.unlink()
        message_path.parent.rmdir()

    def test_notify_tmux_identity_mismatch_and_missing_marker_use_fallback(self) -> None:
        for slug, variable, value, expected in (
            ("notify-tmux-mismatch", "KANBAN_TMUX_PANE_SESSION", "wrong-session", "tmux 会话不匹配"),
            ("notify-tmux-missing", "KANBAN_TMUX_PANE_SESSION_MISSING", "1", "tmux pane 缺少会话标记"),
        ):
            with self.subTest(expected=expected):
                self.install_fake_launchers()
                task_id, task = self.make_todo(slug)
                self.run_command("start", "--launcher", "tmux", "--agent", "claude", task_id)
                review = self.root / "working" / task.name
                self.set_branch(review, slug)
                self.run_command("move", task_id, "review")
                review = self.root / "review" / task.name
                before = review.read_text(encoding="utf-8")
                self.env[variable] = value
                self.env["KANBAN_TMUX_RESPAWN_FAIL"] = "1"

                result = self.run_command(
                    "notify", task_id, "--message", "x", "--timeout", "61", succeeds=False
                )

                self.assertIn(expected, result.stderr)
                self.assertIn("恢复=", result.stderr)
                self.assertEqual(before, review.read_text(encoding="utf-8"))
                self.env.pop(variable, None)
                self.env.pop("KANBAN_TMUX_RESPAWN_FAIL", None)

    def test_notify_tmux_ack_timeout_warns_without_resuming(self) -> None:
        self.install_fake_launchers()
        task_id, task = self.make_todo("notify-tmux-unconfirmed")
        self.run_command("start", "--launcher", "tmux", "--agent", "claude", task_id)
        working = self.root / "working" / task.name
        self.set_branch(working, "notify-tmux-unconfirmed")
        self.run_command("move", task_id, "review")
        self.env["KANBAN_TMUX_CURRENT_COMMAND"] = "claude"
        self.env["KANBAN_TMUX_CAPTURE_FAIL"] = "1"

        result = self.run_command("notify", task_id, "--message", "x", "--timeout", "61")

        self.assertIn("通道=tmux-direct", result.stdout)
        self.assertIn("回执=未确认", result.stdout)
        self.assertIn("已投递, 未在超时内确认", result.stderr)
        self.assertTrue((self.root / "working" / task.name).exists())
        match = re.search(r"消息文件=(\S+)", result.stdout)
        message_path = Path(match.group(1))
        message_path.unlink()
        message_path.parent.rmdir()

    def test_notify_ack_timeout_warns_without_resuming_the_session(self) -> None:
        task_id, review = self.make_herdr_review("notify-fallback")
        self.env["KANBAN_HERDR_ACK_FAIL"] = "1"

        result = self.run_command("notify", task_id, "--message", "继续修复", "--timeout", "61")

        self.assertIn("通道=herdr-direct", result.stdout)
        self.assertIn("回执=未确认", result.stdout)
        self.assertIn("已投递, 未在超时内确认", result.stderr)
        self.assertTrue((self.root / "working" / review.name).exists())
        prompted = self.herdr_arguments("prompt")
        self.assertEqual(["agent", "prompt", "w1:p9"], prompted[:3])
        self.assertEqual(4, len(prompted))
        self.assertTrue(prompted[3].startswith("# onevoke-notify:"), prompted[3])
        # 未确认不得再拉起第二个进程: resume 会把带 --resume 的 Agent 命令写进 run 日志.
        self.assertNotIn("--resume", "\n".join(self.herdr_arguments("run")))
        self.assertEqual(1, self.herdr_arguments("order").count("tab create"))
        match = re.search(r"消息文件=(\S+)", result.stdout)
        message_path = Path(match.group(1))
        message_path.unlink()
        message_path.parent.rmdir()

    def test_notify_falls_back_to_resume_when_herdr_agent_prompt_fails(self) -> None:
        task_id, review = self.make_herdr_review("notify-prompt-fail")
        self.env["KANBAN_HERDR_PROMPT_FAIL"] = "1"

        result = self.run_command("notify", task_id, "--message", "继续修复", "--timeout", "61")

        self.assertIn("通道=resume", result.stdout)
        self.assertIn("herdr agent prompt 失败", result.stdout)
        self.assertTrue((self.root / "working" / review.name).exists())
        # 直投失败不得退回 pane run 重投: 那正是正文停在输入栏的路径.
        self.assertNotIn("# onevoke-notify:", "\n".join(self.herdr_arguments("run")))

    def test_notify_herdr_identity_rejections_are_reported_when_fallback_fails(self) -> None:
        cases = (
            ("notify-no-pane", "KANBAN_HERDR_GET_FAIL", "1", "pane 不存在"),
            ("notify-agent", "KANBAN_HERDR_AGENT", "codex", "Agent 不匹配"),
            ("notify-busy", "KANBAN_HERDR_STATUS", "running", "状态不可投递"),
            ("notify-session", "KANBAN_HERDR_SESSION", "wrong-session", "会话不匹配"),
        )
        for slug, variable, value, expected in cases:
            with self.subTest(expected=expected):
                task_id, review = self.make_herdr_review(slug)
                before = review.read_text(encoding="utf-8")
                self.env[variable] = value
                self.env["KANBAN_HERDR_RUN_FAIL"] = "1"

                result = self.run_command("notify", task_id, "--message", "x", "--timeout", "61", succeeds=False)

                self.assertIn(expected, result.stderr)
                self.assertIn("恢复=", result.stderr)
                self.assertEqual(before, review.read_text(encoding="utf-8"))
                self.env.pop(variable, None)
                self.env.pop("KANBAN_HERDR_RUN_FAIL", None)

    def test_notify_tmux_rejects_dead_and_copy_mode_panes(self) -> None:
        self.install_fake_launchers()
        for slug, variable, expected in (
            ("notify-dead", "KANBAN_TMUX_DEAD", "tmux pane 已退出"),
            ("notify-copy", "KANBAN_TMUX_IN_MODE", "copy-mode"),
        ):
            with self.subTest(expected=expected):
                task_id, task = self.make_todo(slug)
                self.run_command("start", "--launcher", "tmux", "--agent", "claude", task_id)
                review = self.root / "working" / task.name
                self.set_branch(review, slug)
                self.run_command("move", task_id, "review")
                review = self.root / "review" / task.name
                before = review.read_text(encoding="utf-8")
                self.env["KANBAN_TMUX_CURRENT_COMMAND"] = "claude"
                self.env[variable] = "1"
                self.env["KANBAN_TMUX_FAIL"] = "1"

                result = self.run_command("notify", task_id, "--message", "x", "--timeout", "61", succeeds=False)

                self.assertIn(expected, result.stderr)
                self.assertEqual(before, review.read_text(encoding="utf-8"))
                self.env.pop(variable, None)
                self.env.pop("KANBAN_TMUX_FAIL", None)

    def test_notify_fallback_immediate_exit_keeps_review_card_unchanged(self) -> None:
        self.install_fake_launchers()
        task_id, task = self.make_todo("notify-exit")
        self.run_command("start", "--launcher", "tmux", "--agent", "claude", task_id)
        review = self.root / "working" / task.name
        self.set_branch(review, "notify-exit")
        self.run_command("move", task_id, "review")
        review = self.root / "review" / task.name
        before = review.read_text(encoding="utf-8")
        review.write_text(re.sub(r"(?m)^- 窗口:.*$", "- 窗口:", before, count=1), encoding="utf-8")
        before = review.read_text(encoding="utf-8")
        self.env["KANBAN_TMUX_RESPAWN_FAIL"] = "1"

        result = self.run_command("notify", task_id, "--message", "x", "--timeout", "61", succeeds=False)

        self.assertIn("tmux 启动 Agent 失败", result.stderr)
        self.assertEqual(before, review.read_text(encoding="utf-8"))
        self.assertTrue((self.root / "tmux.log.kill").exists())

    def test_notify_legacy_non_codex_card_discovers_and_persists_herdr_pane(self) -> None:
        task_id, review = self.make_herdr_review("notify-legacy")
        original = review.read_text(encoding="utf-8")
        session_id = re.search(r"(?m)^- 会话: claude (\S+)$", original).group(1)
        review.write_text(
            re.sub(r"(?m)^- 窗口:.*\n", "", original, count=1),
            encoding="utf-8",
        )
        self.env["CODEX_HOME"] = str(self.root / "must-not-be-read")
        self.env["KANBAN_HERDR_LIST_JSON"] = json.dumps({
            "id": "cli:pane:list",
            "result": {
                "type": "pane_list",
                "panes": [
                    {
                        "pane_id": "w1:p9",
                        "tab_id": "w1:t9",
                        "agent": "claude",
                        "agent_status": "idle",
                        "agent_session": {"source": "herdr:claude", "value": session_id},
                    },
                    {
                        "pane_id": "w2:p8",
                        "tab_id": "w2:t8",
                        "agent": "codex",
                        "agent_status": "idle",
                        "agent_session": {"source": "herdr:codex", "value": session_id},
                    },
                ],
            },
        })

        result = self.run_command("notify", task_id, "--message", "x", "--timeout", "61")

        self.assertIn("通道=herdr-direct", result.stdout)
        working = self.root / "working" / review.name
        self.assertIn("- 窗口: herdr:w1:t9:w1:p9\n", working.read_text(encoding="utf-8"))
        self.assertEqual(["pane", "list"], self.herdr_arguments("list"))
        match = re.search(r"消息文件=(\S+)", result.stdout)
        message_path = Path(match.group(1))
        message_path.unlink()
        message_path.parent.rmdir()

    def test_notify_herdr_discovery_rejects_zero_and_ambiguous_matches(self) -> None:
        for slug, panes, expected in (
            ("notify-discovery-zero", [], "反查无匹配"),
            (
                "notify-discovery-many",
                [
                    {"pane_id": "w1:p9", "tab_id": "w1:t9"},
                    {"pane_id": "w2:p8", "tab_id": "w2:t8"},
                ],
                "匹配不唯一",
            ),
        ):
            with self.subTest(expected=expected):
                task_id, review = self.make_herdr_review(slug)
                text = review.read_text(encoding="utf-8")
                session_id = re.search(r"(?m)^- 会话: claude (\S+)$", text).group(1)
                review.write_text(re.sub(r"(?m)^- 窗口:.*\n", "", text, count=1), encoding="utf-8")
                enriched = [
                    {
                        **pane,
                        "agent": "claude",
                        "agent_status": "idle",
                        "agent_session": {"value": session_id},
                    }
                    for pane in panes
                ]
                self.env["KANBAN_HERDR_LIST_JSON"] = json.dumps(
                    {"id": "cli:pane:list", "result": {"type": "pane_list", "panes": enriched}}
                )
                self.env["KANBAN_HERDR_RUN_FAIL"] = "1"

                result = self.run_command("notify", task_id, "--message", "x", "--timeout", "61", succeeds=False)

                self.assertIn(expected, result.stderr)
                self.assertFalse((self.root / "herdr.log.send").exists())
                self.assertNotIn("- 窗口:", review.read_text(encoding="utf-8"))
                self.env.pop("KANBAN_HERDR_LIST_JSON", None)
                self.env.pop("KANBAN_HERDR_RUN_FAIL", None)

    def test_notify_discovered_window_is_rolled_back_when_delivery_and_resume_fail(self) -> None:
        task_id, review = self.make_herdr_review("notify-discovery-rollback")
        review.write_text(
            re.sub(r"(?m)^- 窗口:.*\n", "", review.read_text(encoding="utf-8"), count=1),
            encoding="utf-8",
        )
        before = review.read_text(encoding="utf-8")
        self.env["KANBAN_HERDR_PROMPT_FAIL"] = "1"
        self.env["KANBAN_HERDR_RUN_FAIL"] = "1"

        result = self.run_command("notify", task_id, "--message", "x", "--timeout", "61", succeeds=False)

        self.assertIn("herdr agent prompt 失败", result.stderr)
        self.assertEqual(before, review.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "working" / review.name).exists())

    def test_notify_explicit_pane_override_still_validates_identity(self) -> None:
        task_id, review = self.make_herdr_review("notify-pane")
        review.write_text(
            re.sub(r"(?m)^- 窗口:.*$", "- 窗口: foreground", review.read_text(encoding="utf-8"), count=1),
            encoding="utf-8",
        )

        result = self.run_command("notify", task_id, "--message", "x", "--pane", "w1:p9", "--timeout", "61")

        self.assertIn("通道=herdr-direct", result.stdout)
        working = self.root / "working" / review.name
        self.assertIn("- 窗口: herdr:w1:t9:w1:p9\n", working.read_text(encoding="utf-8"))
        match = re.search(r"消息文件=(\S+)", result.stdout)
        message_path = Path(match.group(1))
        message_path.unlink()
        message_path.parent.rmdir()

        task_id, review = self.make_herdr_review("notify-pane-mismatch")
        self.env["KANBAN_HERDR_AGENT"] = "codex"
        self.env["KANBAN_HERDR_RUN_FAIL"] = "1"

        result = self.run_command("notify", task_id, "--message", "x", "--pane", "w1:p9", "--timeout", "61", succeeds=False)

        self.assertIn("Agent 不匹配", result.stderr)
        self.assertTrue(review.exists())

    def test_notify_tmux_legacy_card_without_window_uses_resume(self) -> None:
        self.install_fake_launchers()
        task_id, task = self.make_todo("notify-tmux-legacy")
        self.run_command("start", "--launcher", "tmux", "--agent", "claude", task_id)
        review = self.root / "working" / task.name
        self.set_branch(review, "notify-tmux-legacy")
        review.write_text(re.sub(r"(?m)^- 窗口:.*\n", "", review.read_text(encoding="utf-8"), count=1), encoding="utf-8")
        self.run_command("move", task_id, "review")
        self.env["KANBAN_TMUX_CURRENT_COMMAND"] = "claude"

        result = self.run_command("notify", task_id, "--message", "x", "--timeout", "61")

        self.assertIn("通道=resume", result.stdout)
        self.assertTrue((self.root / "working" / task.name).exists())

    def test_notify_legacy_codex_card_resolves_rollout_session(self) -> None:
        self.install_fake_herdr()
        task_id, task = self.make_todo("notify-codex-legacy")
        self.run_command("start", "--launcher", "herdr", "--agent", "codex", task_id)
        working = self.root / "working" / task.name
        self.set_branch(working, "notify-codex-legacy")
        self.run_command("move", task_id, "review")
        review = self.root / "review" / task.name
        review.write_text(re.sub(r"(?m)^- 窗口:.*\n", "", review.read_text(encoding="utf-8"), count=1), encoding="utf-8")
        codex_home = self.root / "codex-home"
        sessions = codex_home / "sessions" / "2026" / "09" / "01"
        sessions.mkdir(parents=True)
        session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        rollout = sessions / "rollout-test.jsonl"
        rollout.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": session_id}}) + "\n"
            + json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": f"执行 Kanban 任务 {task_id}."}}) + "\n",
            encoding="utf-8",
        )
        self.env["CODEX_HOME"] = str(codex_home)
        self.env["KANBAN_HERDR_AGENT"] = "codex"
        self.env["KANBAN_HERDR_SESSION"] = session_id

        result = self.run_command("notify", task_id, "--message", "x", "--timeout", "61")

        self.assertIn("通道=herdr-direct", result.stdout)
        working = self.root / "working" / task.name
        self.assertIn("- 窗口: herdr:w1:t9:w1:p9\n", working.read_text(encoding="utf-8"))
        match = re.search(r"消息文件=(\S+)", result.stdout)
        message_path = Path(match.group(1))
        message_path.unlink()
        message_path.parent.rmdir()

    def test_notify_foreground_and_console_addresses_go_straight_to_resume(self) -> None:
        kanban = self.load_kanban_module()
        for launcher in ("foreground", "console"):
            with self.subTest(launcher=launcher):
                task_id, review = self.make_herdr_review(f"notify-{launcher}")
                text = review.read_text(encoding="utf-8")
                review.write_text(
                    re.sub(r"(?m)^- 窗口:.*$", f"- 窗口: {launcher}", text, count=1),
                    encoding="utf-8",
                )
                args = argparse.Namespace(
                    task=task_id,
                    message="x",
                    message_file=None,
                    timeout=61,
                    pane=None,
                )
                with mock.patch.dict(os.environ, self.env, clear=True):
                    with mock.patch.object(kanban.sys.stdout, "write"):
                        with mock.patch.object(kanban.sys.stdout, "flush"):
                            with mock.patch.object(kanban, "notify_via_resume") as resume:
                                with mock.patch.object(kanban, "write_notify_message") as writer:
                                    kanban.command_notify(args, self.root)

                resume.assert_called_once()
                writer.assert_not_called()
                self.assertTrue((self.root / "working" / review.name).exists())

    def test_notify_resume_rejects_done_herdr_agent_and_keeps_review(self) -> None:
        kanban = self.load_kanban_module()
        task_id, review = self.make_herdr_review("notify-resume-done")
        review.write_text(
            re.sub(r"(?m)^- 窗口:.*$", "- 窗口: foreground", review.read_text(encoding="utf-8"), count=1),
            encoding="utf-8",
        )
        before = review.read_text(encoding="utf-8")
        self.env["KANBAN_HERDR_STATUS"] = "done"
        args = argparse.Namespace(task=task_id, message="x", message_file=None, timeout=61, pane=None)

        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch.object(kanban.time, "monotonic", side_effect=(0.0, 62.0)):
                with mock.patch.object(kanban.time, "sleep"):
                    with self.assertRaisesRegex(kanban.KanbanError, "存活校验超时"):
                        kanban.command_notify(args, self.root)

        self.assertEqual(before, review.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "working" / review.name).exists())

    def test_notify_existing_done_herdr_pane_accepts_direct_delivery(self) -> None:
        task_id, review = self.make_herdr_review("notify-existing-done")
        self.env["KANBAN_HERDR_STATUS"] = "done"

        result = self.run_command("notify", task_id, "--message", "x", "--timeout", "61")

        self.assertIn("通道=herdr-direct", result.stdout)
        self.assertTrue((self.root / "working" / review.name).exists())
        message_path = Path(re.search(r"消息文件=(\S+)", result.stdout).group(1))
        message_path.unlink()
        message_path.parent.rmdir()

    def test_process_resume_observes_the_full_liveness_budget(self) -> None:
        kanban = self.load_kanban_module()
        plan = kanban.LaunchPlan("console", self.root.parent)
        session = kanban.AgentSession("codex", "session")

        exits_late = mock.Mock(returncode=17)
        exits_late.poll.side_effect = (None, 17)
        with mock.patch.object(kanban.time, "monotonic", side_effect=(0.0, 0.25)):
            with mock.patch.object(kanban.time, "sleep"):
                with self.assertRaisesRegex(kanban.KanbanError, "观察期内退出"):
                    kanban.validate_resumed_agent(
                        plan, kanban.LaunchOutcome(process=exits_late), session, 61
                    )

        survives = mock.Mock(returncode=None)
        survives.poll.return_value = None
        with mock.patch.object(kanban.time, "monotonic", side_effect=(0.0, 61.0)):
            with mock.patch.object(kanban.time, "sleep") as sleeper:
                kanban.validate_resumed_agent(
                    plan, kanban.LaunchOutcome(process=survives), session, 61
                )
        sleeper.assert_not_called()

    def test_validate_resumed_agent_herdr_liveness_identity_policy(self) -> None:
        kanban = self.load_kanban_module()
        plan = kanban.LaunchPlan("herdr", self.root.parent, herdr_bin="herdr")
        outcome = kanban.LaunchOutcome(tab="w1:t9", pane="w1:p9")
        session = kanban.AgentSession("cursor", "expected-session")

        absent_identities = (
            {},
            {"agent_session": None},
            {"agent_session": "invalid"},
            {"agent_session": {}},
            {"agent_session": {"value": ""}},
            {"agent_session": {"value": 42}},
        )
        for status in ("idle", "working", "blocked"):
            for identity in absent_identities:
                with self.subTest(case="absent identity", status=status, identity=identity):
                    pane = {"agent": "cursor", "agent_status": status, **identity}
                    with mock.patch.object(kanban, "herdr_pane_info", return_value=pane):
                        with mock.patch.object(kanban.time, "monotonic", return_value=0.0):
                            kanban.validate_resumed_agent(plan, outcome, session, 61)

        for status in ("done", "unknown"):
            with self.subTest(case="absent identity invalid status", status=status):
                pane = {"agent": "cursor", "agent_status": status}
                with mock.patch.object(kanban, "herdr_pane_info", return_value=pane):
                    with mock.patch.object(kanban.time, "monotonic", side_effect=(0.0, 62.0)):
                        with self.assertRaisesRegex(kanban.KanbanError, "存活校验超时"):
                            kanban.validate_resumed_agent(plan, outcome, session, 61)

        matching = {
            "agent": "cursor",
            "agent_status": "idle",
            "agent_session": {"value": "expected-session"},
        }
        with mock.patch.object(kanban, "herdr_pane_info", return_value=matching):
            with mock.patch.object(kanban.time, "monotonic", return_value=0.0):
                kanban.validate_resumed_agent(plan, outcome, session, 61)

        rejected = (
            ("session mismatch", {**matching, "agent_session": {"value": "other-session"}}),
            ("agent mismatch", {**matching, "agent": "codex"}),
        )
        for case, pane in rejected:
            with self.subTest(case=case):
                with mock.patch.object(kanban, "herdr_pane_info", return_value=pane):
                    with mock.patch.object(kanban.time, "monotonic", side_effect=(0.0, 62.0)):
                        with self.assertRaisesRegex(kanban.KanbanError, "存活校验超时"):
                            kanban.validate_resumed_agent(plan, outcome, session, 61)

    def test_failed_resume_cleanup_reports_herdr_and_tmux_failures(self) -> None:
        kanban = self.load_kanban_module()
        cases = (
            (
                kanban.LaunchPlan("herdr", self.root.parent, herdr_bin="herdr"),
                kanban.LaunchOutcome(tab="w1:t9", pane="w1:p9"),
                mock.patch.object(kanban, "herdr_close_tab", return_value="injected close failure"),
            ),
            (
                kanban.LaunchPlan("tmux", self.root.parent, tmux="tmux", session="$42"),
                kanban.LaunchOutcome(window="@9", pane="%9"),
                mock.patch.object(kanban, "tmux_close_window", return_value="injected kill failure"),
            ),
        )
        for plan, outcome, failure in cases:
            with self.subTest(launcher=plan.launcher):
                with failure:
                    with self.assertRaisesRegex(kanban.KanbanError, "可能仍存活"):
                        kanban.cleanup_failed_resume(plan, outcome)

    def test_notify_via_resume_combines_liveness_and_cleanup_errors(self) -> None:
        kanban = self.load_kanban_module()
        self.install_fake_launchers()
        task_id, task = self.make_todo("notify-cleanup-combined")
        self.run_command("start", "--agent", "claude", task_id)
        working = self.root / "working" / task.name
        text = working.read_text(encoding="utf-8")
        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch.object(kanban, "validate_resumed_agent", side_effect=kanban.KanbanError("liveness failed")):
                with mock.patch.object(kanban, "cleanup_failed_resume", side_effect=kanban.KanbanError("cleanup failed")):
                    with self.assertRaisesRegex(kanban.KanbanError, "liveness failed; 清理=cleanup failed"):
                        kanban.notify_via_resume(kanban.locate(kanban.load_board(self.root), task_id), text, "x", self.root, 61)

    def test_tmux_notify_timeout_uses_injected_clock(self) -> None:
        kanban = self.load_kanban_module()
        ok = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(kanban, "tmux_capture", return_value=ok):
            with mock.patch.object(kanban.time, "monotonic", side_effect=(0.0, 62.0)):
                with mock.patch.object(kanban.time, "sleep") as sleeper:
                    confirmed, detail = kanban.tmux_direct_notify("tmux", "%9", "instruction", "marker", 61)
        self.assertFalse(confirmed)
        self.assertIn("回执超时", detail)
        sleeper.assert_not_called()

    def test_notify_message_cleanup_error_is_not_suppressed(self) -> None:
        kanban = self.load_kanban_module()
        path = mock.Mock()
        path.unlink.side_effect = OSError("injected cleanup failure")
        with self.assertRaisesRegex(OSError, "injected cleanup failure"):
            kanban.remove_notify_message(path)

    def test_notify_foreground_resume_rejects_an_immediate_exit(self) -> None:
        kanban = self.load_kanban_module()
        task_id, review = self.make_herdr_review("notify-foreground-exit")
        text = review.read_text(encoding="utf-8")
        review.write_text(
            re.sub(r"(?m)^- 窗口:.*$", "- 窗口: foreground", text, count=1),
            encoding="utf-8",
        )
        before = review.read_text(encoding="utf-8")
        self.write_onevoke_config("claude", "foreground")
        self.env["KANBAN_AGENT_EXIT"] = "1"
        args = argparse.Namespace(task=task_id, message="x", message_file=None, timeout=61, pane=None)

        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch.object(kanban.sys.stdin, "isatty", return_value=True):
                with mock.patch.object(kanban.sys.stdout, "isatty", return_value=True):
                    with mock.patch.object(kanban.sys.stderr, "isatty", return_value=True):
                        with self.assertRaisesRegex(kanban.KanbanError, "观察期内退出"):
                            kanban.command_notify(args, self.root)

        self.assertEqual(before, review.read_text(encoding="utf-8"))

    def test_notify_foreground_stays_attached_until_a_late_agent_exit_on_pty(self) -> None:
        fake_bin = self.install_fake_herdr()
        task_id, review = self.make_herdr_review("notify-foreground-pty")
        review.write_text(
            re.sub(r"(?m)^- 窗口:.*$", "- 窗口: foreground", review.read_text(encoding="utf-8"), count=1),
            encoding="utf-8",
        )
        self.write_onevoke_config("claude", "foreground")
        claude = fake_bin / "claude"
        claude.write_text("#!/bin/sh\n/bin/sleep 0.6\nexit 7\n", encoding="utf-8")
        claude.chmod(0o755)
        master, slave = pty.openpty()
        started = time.monotonic()
        process = subprocess.Popen(
            [sys.executable, str(COMMAND), "notify", task_id, "--message", "x", "--timeout", "61"],
            env=self.env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
        )
        os.close(slave)
        try:
            returncode = process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
            self.fail("foreground notify did not observe the resumed Agent exit")
        finally:
            os.close(master)
        self.assertNotEqual(0, returncode)
        self.assertGreaterEqual(time.monotonic() - started, 0.5)
        self.assertTrue(review.exists())
        self.assertFalse((self.root / "working" / review.name).exists())

    def test_notify_foreground_waits_after_migrating_the_card(self) -> None:
        kanban = self.load_kanban_module()
        task_id, review = self.make_herdr_review("notify-foreground-wait")
        review.write_text(
            re.sub(r"(?m)^- 窗口:.*$", "- 窗口: foreground", review.read_text(encoding="utf-8"), count=1),
            encoding="utf-8",
        )
        process = mock.Mock()

        def finish_after_migration():
            self.assertTrue((self.root / "working" / review.name).exists())
            return 0

        process.wait.side_effect = finish_after_migration
        launch = kanban.ResumeLaunch(
            kanban.LaunchPlan("foreground", self.root.parent),
            kanban.LaunchOutcome(process=process),
        )
        args = argparse.Namespace(task=task_id, message="x", message_file=None, timeout=61, pane=None)
        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch.object(kanban, "notify_via_resume", return_value=launch):
                kanban.command_notify(args, self.root)

        process.wait.assert_called_once_with()

    def test_notify_requires_message_and_supported_state(self) -> None:
        self.install_fake_herdr()
        wrong_id, _wrong = self.make_todo("notify-wrong-state")
        task_id, _task = self.make_todo("notify-args")
        self.run_command("start", "--launcher", "herdr", task_id)

        wrong_state = self.run_command("notify", wrong_id, "--message", "x", succeeds=False)
        neither = self.run_command("notify", task_id, succeeds=False)
        both = self.run_command("notify", task_id, "--message", "x", "--message-file", "y", succeeds=False)
        empty = self.run_command("notify", task_id, "--message", "  ", succeeds=False)
        bad_timeout = self.run_command("notify", task_id, "--message", "x", "--timeout", "nan", succeeds=False)
        short_timeout = self.run_command("notify", task_id, "--message", "x", "--timeout", "60", succeeds=False)

        self.assertIn("todo", wrong_state.stderr)
        self.assertIn("--message", neither.stderr)
        self.assertIn("--message", both.stderr)
        self.assertIn("不得为空", empty.stderr)
        self.assertIn("超时", bad_timeout.stderr)
        self.assertIn("大于 60", short_timeout.stderr)

    def test_tmux_persists_window_before_agent_can_mutate_card(self) -> None:
        self.install_fake_launchers()
        task_id, task = self.make_todo("tmux-order")
        working = self.root / "working" / task.name
        self.env["KANBAN_TMUX_MUTATE_CARD"] = str(working)

        self.run_command("start", "--launcher", "tmux", "--agent", "claude", task_id)

        text = working.read_text(encoding="utf-8")
        self.assertIn("- 窗口: tmux:$42:@9:%9\n", text)
        self.assertTrue(text.endswith("# agent mutation\n"))

    def test_tmux_metadata_failure_closes_window_and_rolls_back_card(self) -> None:
        kanban = self.load_kanban_module()
        self.install_fake_launchers()
        task_id, task = self.make_todo("tmux-metadata-fail")
        original = task.read_text(encoding="utf-8")
        original_writer = kanban.write_text_atomic
        calls = 0

        def fail_metadata(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected metadata failure")
            return original_writer(*args, **kwargs)

        args = argparse.Namespace(task=task_id, agent="claude", launcher="tmux")
        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch.object(kanban, "write_text_atomic", side_effect=fail_metadata):
                with self.assertRaisesRegex(OSError, "injected metadata failure"):
                    kanban.command_start(args, self.root)

        restored = self.root / "todo" / task.name
        self.assertEqual(original, restored.read_text(encoding="utf-8"))
        self.assertTrue((self.root / "tmux.log.kill").exists())
        self.assertFalse((self.root / "tmux.log.command").exists())

    def load_kanban_module(self):
        import importlib.util
        from importlib.machinery import SourceFileLoader

        sys.path.insert(0, str(COMMAND.parent))
        try:
            loader = SourceFileLoader("kanban_rollback_test", str(COMMAND))
            spec = importlib.util.spec_from_loader(loader.name, loader)
            if spec is None:
                self.fail(f"unable to load {COMMAND}")
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
        finally:
            sys.path.pop(0)
        return module

    def test_rollback_keeps_the_card_in_working_when_the_document_cannot_be_restored(self) -> None:
        kanban_mod = self.load_kanban_module()
        task_id, task = self.make_todo("rollback-restore")
        original = task.read_text(encoding="utf-8")
        self.run_command("move", task_id, "working")
        working = self.root / "working" / task.name
        working.write_text(original.replace("- 负责人:\n", "- 负责人: codex\n", 1), encoding="utf-8")
        moved = kanban_mod.Entry(task_id, "working", working, working, "small")
        failure = kanban_mod.LaunchFailure(kanban_mod.KanbanError("tmux new-window 失败"), None)

        def refuse_write(*_args, **_kwargs):
            raise OSError("disk full")

        with mock.patch.object(kanban_mod, "write_text_atomic", refuse_write):
            with self.assertRaises(kanban_mod.KanbanError) as raised:
                kanban_mod.rollback_launch(self.root, moved, "todo", failure, original)

        message = str(raised.exception)
        self.assertIn("卡片保留在 working", message)
        self.assertIn("恢复文档失败", message)
        self.assertTrue(working.exists(), "文档未恢复时不得迁回 todo")
        self.assertFalse(task.exists())
        self.assertIn("- 负责人: codex\n", working.read_text(encoding="utf-8"))

    def test_rollback_restores_the_document_and_moves_the_card_back(self) -> None:
        kanban_mod = self.load_kanban_module()
        task_id, task = self.make_todo("rollback-ok")
        original = task.read_text(encoding="utf-8")
        self.run_command("move", task_id, "working")
        working = self.root / "working" / task.name
        working.write_text(original.replace("- 负责人:\n", "- 负责人: codex\n", 1), encoding="utf-8")
        moved = kanban_mod.Entry(task_id, "working", working, working, "small")
        failure = kanban_mod.LaunchFailure(kanban_mod.KanbanError("tmux new-window 失败"), None)

        with self.assertRaises(kanban_mod.KanbanError) as raised:
            kanban_mod.rollback_launch(self.root, moved, "todo", failure, original)

        self.assertEqual("tmux new-window 失败", str(raised.exception))
        self.assertTrue(task.exists())
        self.assertFalse(working.exists())
        self.assertEqual(original, task.read_text(encoding="utf-8"))

    def test_start_moves_task_and_launches_agent_window(self) -> None:
        task_id, task = self.make_todo("start-direct")
        fake_bin = self.install_fake_launchers()

        result = self.run_command("start", task_id)

        self.assertIn(f"已启动: {task_id}", result.stdout)
        started = self.root / "working" / task.name
        text = started.read_text(encoding="utf-8")
        self.assertIn("- 负责人: codex\n", text)
        started_at = re.search(
            r"(?m)^- 开始时间: (\d{4}-\d{2}-\d{2} \d{2}:\d{2})$", text
        )
        self.assertIsNotNone(started_at)
        self.assertIn(started_at.group(1), self.run_command("list", "working").stdout)
        tmux_args = (self.root / "tmux.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual("new-window", tmux_args[0])
        self.assertIn("-d", tmux_args)
        self.assertEqual("$42:", tmux_args[tmux_args.index("-t") + 1])
        self.assertEqual(str(self.root.resolve().parent), tmux_args[tmux_args.index("-c") + 1])
        self.assertEqual("kb-任务-start-direct", tmux_args[tmux_args.index("-n") + 1])
        command = self.last_launch_command()
        self.assertIn(str(fake_bin / "codex"), command)
        self.assertIn("--model gpt-5.6-sol", command)
        self.assertIn('model_reasoning_effort="medium"', command)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertIn(task_id, command)
        self.assertIn("先运行 kanban rules", command)
        self.assertIn("遵守目标项目 AGENTS.md", command)
        self.assertNotIn(".onevoke/bin/kanban", command)

    def test_start_window_name_folds_title_and_truncates(self) -> None:
        task_id, task = self.make_todo("window-name")
        text = task.read_text(encoding="utf-8")
        task.write_text(
            text.replace("# 任务 window-name", f"# 修复 登录  重试 {'长' * 60}", 1),
            encoding="utf-8",
        )
        self.install_fake_launchers()

        self.run_command("start", task_id)

        tmux_args = (self.root / "tmux.log").read_text(encoding="utf-8").splitlines()
        name = tmux_args[tmux_args.index("-n") + 1]
        self.assertEqual(f"kb-修复-登录-重试-{'长' * 60}"[:50], name)
        self.assertEqual(50, len(name))

    def test_start_window_name_falls_back_to_slug_without_title(self) -> None:
        task_id, task = self.make_todo("no-title")
        text = task.read_text(encoding="utf-8")
        task.write_text(text.replace("# 任务 no-title\n", "", 1), encoding="utf-8")
        self.install_fake_launchers()

        self.run_command("start", task_id)

        tmux_args = (self.root / "tmux.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual("kb-no-title", tmux_args[tmux_args.index("-n") + 1])

    def test_start_without_task_prompts_for_todo_selection(self) -> None:
        first_id, first = self.make_todo("alpha")
        second_id, second = self.make_todo("beta")
        self.install_fake_launchers()

        result = self.run_command("start", "--agent", "claude", input_text="2\n")

        self.assertIn(f"1. {first_id}", result.stdout)
        self.assertIn(f"2. {second_id}", result.stdout)
        self.assertTrue(first.exists())
        self.assertTrue((self.root / "working" / second.name).exists())
        self.assertIn("Agent=claude", result.stdout)
        command = self.last_launch_command()
        self.assertIn("--model opus --effort medium", command)
        self.assertIn("--dangerously-skip-permissions", command)

    def test_start_with_grok_launches_bypass_permission_session(self) -> None:
        task_id, task = self.make_todo("start-grok")
        fake_bin = self.install_fake_launchers()

        result = self.run_command("start", "--agent", "grok", task_id)

        self.assertIn("Agent=grok", result.stdout)
        started = self.root / "working" / task.name
        self.assertIn("- 负责人: grok\n", started.read_text(encoding="utf-8"))
        command = self.last_launch_command()
        self.assertIn(str(fake_bin / "grok"), command)
        self.assertNotIn("--model", command)
        self.assertIn("--effort high", command)
        self.assertNotIn("--effort xhigh", command)
        self.assertIn("--permission-mode bypassPermissions", command)
        self.assertIn(task_id, command)

    def test_start_uses_configured_models_and_efforts(self) -> None:
        task_id, _ = self.make_todo("custom-model")
        self.install_fake_launchers()
        self.write_onevoke_config(
            "codex",
            "tmux",
            models={"kanban": {"codex": {"model": "gpt-7", "small_effort": "low"}}},
        )

        self.run_command("start", task_id)

        command = self.last_launch_command()
        self.assertIn("--model gpt-7", command)
        self.assertIn('model_reasoning_effort="low"', command)
        self.assertNotIn("gpt-5.6-sol", command)

    def test_start_omits_model_argument_when_config_model_is_empty(self) -> None:
        task_id, _ = self.make_todo("empty-model")
        self.install_fake_launchers()
        self.write_onevoke_config(
            "claude",
            "tmux",
            models={"kanban": {"claude": {"model": ""}}},
        )

        self.run_command("start", task_id)

        command = self.last_launch_command()
        self.assertNotIn("--model", command)
        self.assertIn("--effort medium", command)
        self.assertIn("--dangerously-skip-permissions", command)

    def test_start_uses_the_configured_default_agent(self) -> None:
        task_id, _ = self.make_todo("configured-agent")
        fake_bin = self.install_fake_launchers()
        self.write_onevoke_config("grok", "tmux")

        result = self.run_command("start", task_id)

        self.assertIn("Agent=grok", result.stdout)
        command = self.last_launch_command()
        self.assertIn(str(fake_bin / "grok"), command)
        self.assertIn("--permission-mode bypassPermissions", command)

    def test_start_ignores_unfinished_welcome_selections(self) -> None:
        task_id, _ = self.make_todo("unfinished-config")
        fake_bin = self.install_fake_launchers()
        self.write_onevoke_config("grok", "foreground", welcome_complete=False)

        result = self.run_command("start", task_id)

        self.assertIn("Agent=codex", result.stdout)
        command = self.last_launch_command()
        self.assertIn(str(fake_bin / "codex"), command)

    def test_foreground_launcher_rejects_a_noninteractive_terminal(self) -> None:
        task_id, task = self.make_todo("foreground-no-tty")
        self.install_fake_launchers()
        self.write_onevoke_config("codex", "foreground")

        result = self.run_command("start", task_id, succeeds=False)

        self.assertIn("前台启动模式需要交互终端", result.stderr)
        self.assertIn("stdin/stdout/stderr 均为 tty", result.stderr)
        self.assertIn("--launcher tmux", result.stderr)
        self.assertTrue(task.exists())
        self.assertFalse((self.root / "working" / task.name).exists())

    def test_foreground_launcher_runs_the_agent_in_the_project(self) -> None:
        task_id, task = self.make_todo("foreground")
        fake_bin = self.install_fake_launchers()
        foreground_log = self.root / "foreground.log"
        (fake_bin / "claude").write_text(
            "#!/bin/sh\npwd > \"$KANBAN_FOREGROUND_LOG\"\n",
            encoding="utf-8",
        )
        (fake_bin / "claude").chmod(0o755)
        self.env["KANBAN_FOREGROUND_LOG"] = str(foreground_log)
        self.write_onevoke_config("claude", "foreground")
        sys.path.insert(0, str(COMMAND.parent))
        try:
            kanban = runpy.run_path(str(COMMAND), run_name="kanban_foreground_test")
        finally:
            sys.path.pop(0)
        args = argparse.Namespace(task=task_id, agent=None, launcher=None)

        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch.object(kanban["sys"].stdin, "isatty", return_value=True):
                with mock.patch.object(kanban["sys"].stdout, "isatty", return_value=True):
                    with mock.patch.object(
                        kanban["sys"].stderr, "isatty", return_value=True
                    ):
                        kanban["command_start"](args, self.root)

        started = self.root / "working" / task.name
        self.assertTrue(started.exists())
        self.assertIn("- 负责人: claude", started.read_text(encoding="utf-8"))
        self.assertEqual(str(self.root.parent), foreground_log.read_text().strip())

    def test_start_launcher_option_overrides_machine_config(self) -> None:
        task_id, task = self.make_todo("launcher-override")
        fake_bin = self.install_fake_launchers()
        foreground_log = self.root / "override.log"
        (fake_bin / "codex").write_text(
            "#!/bin/sh\npwd > \"$KANBAN_FOREGROUND_LOG\"\n", encoding="utf-8"
        )
        (fake_bin / "codex").chmod(0o755)
        self.env["KANBAN_FOREGROUND_LOG"] = str(foreground_log)
        self.write_onevoke_config("codex", "tmux")
        sys.path.insert(0, str(COMMAND.parent))
        try:
            kanban = runpy.run_path(str(COMMAND), run_name="kanban_launcher_override_test")
        finally:
            sys.path.pop(0)
        args = argparse.Namespace(task=task_id, agent=None, launcher="foreground")

        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch.object(kanban["sys"].stdin, "isatty", return_value=True):
                with mock.patch.object(kanban["sys"].stdout, "isatty", return_value=True):
                    with mock.patch.object(
                        kanban["sys"].stderr, "isatty", return_value=True
                    ):
                        kanban["command_start"](args, self.root)

        self.assertTrue((self.root / "working" / task.name).exists())
        self.assertEqual(str(self.root.parent), foreground_log.read_text().strip())

    def test_foreground_spawn_failure_rolls_back_before_started_output(self) -> None:
        task_id, task = self.make_todo("spawn-failure")
        self.install_fake_launchers()
        self.write_onevoke_config("codex", "foreground")
        original = task.read_text(encoding="utf-8")
        sys.path.insert(0, str(COMMAND.parent))
        try:
            kanban = runpy.run_path(str(COMMAND), run_name="kanban_spawn_failure_test")
        finally:
            sys.path.pop(0)
        args = argparse.Namespace(task=task_id, agent=None, launcher=None)
        output = io.StringIO()

        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch.object(kanban["sys"].stdin, "isatty", return_value=True):
                with mock.patch.object(kanban["sys"], "stdout", output):
                    with mock.patch.object(output, "isatty", return_value=True):
                        with mock.patch.object(
                            kanban["sys"].stderr, "isatty", return_value=True
                        ):
                            with mock.patch.object(
                                kanban["subprocess"],
                                "Popen",
                                side_effect=OSError("Exec format error"),
                            ):
                                with self.assertRaisesRegex(
                                    kanban["KanbanError"], "启动 Agent 失败"
                                ):
                                    kanban["command_start"](args, self.root)

        self.assertEqual("", output.getvalue())
        self.assertEqual(original, task.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "working" / task.name).exists())

    def test_start_with_cursor_launches_force_session(self) -> None:
        task_id, task = self.make_todo("start-cursor")
        fake_bin = self.install_fake_launchers()

        result = self.run_command("start", "--agent", "cursor", task_id)

        self.assertIn("Agent=cursor", result.stdout)
        started = self.root / "working" / task.name
        self.assertIn("- 负责人: cursor\n", started.read_text(encoding="utf-8"))
        command = self.last_launch_command()
        self.assertIn(str(fake_bin / "cursor-agent"), command)
        self.assertIn("--model cursor-grok-4.6-high", command)
        self.assertIn("--trust", command)
        self.assertIn("--force", command)
        self.assertNotIn("--effort", command)
        self.assertIn(task_id, command)

    def test_start_uses_cursor_large_model_for_large_tasks(self) -> None:
        self.install_fake_launchers()
        slug = "large-cursor"
        task_id = f"{datetime.now().strftime('%Y%m%d')}-{slug}-task"
        self.run_command("new", "--large", "chore", slug, "大型任务 cursor")
        spec = self.root / "backlog" / task_id / "spec.md"
        self.make_ready(spec)
        self.run_command("pick", task_id)

        self.run_command("start", "--agent", "cursor", task_id)

        command = self.last_launch_command()
        self.assertIn("--model cursor-grok-4.6-xhigh", command)
        self.assertNotIn("cursor-grok-4.6-high", command)
        self.assertIn("--trust", command)
        self.assertIn("--force", command)

    def test_start_reports_cursor_agent_when_executable_missing(self) -> None:
        task_id, task = self.make_todo("missing-cursor")
        fake_bin = self.install_fake_launchers()
        (fake_bin / "cursor-agent").unlink()
        self.env["PATH"] = str(fake_bin)

        result = self.run_command("start", "--agent", "cursor", task_id, succeeds=False)

        self.assertIn("cursor-agent", result.stderr)
        self.assertTrue(task.exists())
        self.assertFalse((self.root / "working" / task.name).exists())

    def test_start_omits_cursor_model_when_config_model_is_empty(self) -> None:
        task_id, _ = self.make_todo("empty-cursor-model")
        self.install_fake_launchers()
        self.write_onevoke_config(
            "cursor",
            "tmux",
            models={"kanban": {"cursor": {"small_model": ""}}},
        )

        self.run_command("start", task_id)

        command = self.last_launch_command()
        self.assertNotIn("--model", command)
        self.assertIn("--trust", command)
        self.assertIn("--force", command)

    def test_start_uses_high_effort_for_large_tasks(self) -> None:
        self.install_fake_launchers()
        for agent, expected in (
            ("codex", '--model gpt-5.6-sol --config \'model_reasoning_effort="high"\''),
            ("claude", "--model opus --effort high"),
            ("grok", "--effort xhigh --permission-mode bypassPermissions"),
        ):
            slug = f"large-{agent}"
            task_id = f"{datetime.now().strftime('%Y%m%d')}-{slug}-task"
            self.run_command("new", "--large", "chore", slug, f"大型任务 {agent}")
            spec = self.root / "backlog" / task_id / "spec.md"
            self.make_ready(spec)
            self.run_command("pick", task_id)

            self.run_command("start", "--agent", agent, task_id)

            command = self.last_launch_command()
            self.assertIn(expected, command)

    def test_start_failure_restores_todo_and_metadata(self) -> None:
        task_id, task = self.make_todo("rollback")
        original = task.read_text(encoding="utf-8")
        self.install_fake_launchers()
        self.env["KANBAN_TMUX_FAIL"] = "1"

        result = self.run_command("start", task_id, succeeds=False)

        self.assertIn("tmux new-window 失败", result.stderr)
        self.assertEqual(original, task.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "working" / task.name).exists())

    def test_tmux_pane_session_write_failure_closes_window_and_restores_todo(self) -> None:
        task_id, task = self.make_todo("pane-session-rollback")
        original = task.read_text(encoding="utf-8")
        self.install_fake_launchers()
        self.env["KANBAN_TMUX_PANE_SETOPT_FAIL"] = "1"

        result = self.run_command(
            "start", "--launcher", "tmux", "--agent", "claude", task_id, succeeds=False
        )

        self.assertIn("tmux 写入 pane 会话失败", result.stderr)
        self.assertEqual(original, task.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "working" / task.name).exists())
        self.assertEqual(
            ["kill-window", "-t", "@9"],
            (self.root / "tmux.log.kill").read_text(encoding="utf-8").splitlines(),
        )

    def test_tmux_start_records_the_resolved_codex_session(self) -> None:
        task_id, _task = self.make_todo("pane-codex-session")
        self.install_fake_launchers()

        self.run_command("start", "--launcher", "tmux", "--agent", "codex", task_id)

        self.assertEqual(
            [
                "set-option",
                "-p",
                "-t",
                "%9",
                "@onevoke_session",
                "fake-codex-session",
            ],
            (self.root / "tmux.log.pane-setopt").read_text(encoding="utf-8").splitlines(),
        )

    def test_tmux_codex_start_rejects_all_historical_sessions(self) -> None:
        kanban = self.load_kanban_module()
        task_id, _task = self.make_todo("pane-codex-history")
        self.install_fake_launchers()
        args = argparse.Namespace(task=task_id, launcher="tmux", agent="codex")

        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch.object(
                kanban,
                "codex_sessions_for_task",
                side_effect=(
                    ("old-a", "old-b"),
                    ("old-b",),
                    ("old-a", "old-b", "new-session"),
                ),
            ):
                with mock.patch.object(kanban.time, "sleep"):
                    kanban.command_start(args, self.root)

        self.assertEqual(
            "new-session",
            (self.root / "tmux.log.pane-session").read_text(encoding="utf-8").strip(),
        )

    def test_tmux_codex_start_rejects_ambiguous_new_sessions(self) -> None:
        kanban = self.load_kanban_module()
        task_id, task = self.make_todo("pane-codex-ambiguous")
        original = task.read_text(encoding="utf-8")
        self.install_fake_launchers()
        args = argparse.Namespace(task=task_id, launcher="tmux", agent="codex")

        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch.object(
                kanban,
                "codex_sessions_for_task",
                side_effect=(("old-session",), ("old-session", "new-a", "new-b")),
            ):
                with self.assertRaisesRegex(kanban.KanbanError, "多个新 Codex 会话"):
                    kanban.command_start(args, self.root)

        self.assertEqual(original, task.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "working" / task.name).exists())
        self.assertTrue((self.root / "tmux.log.kill").exists())

    def test_start_outside_tmux_does_not_claim_task(self) -> None:
        task_id, task = self.make_todo("no-tmux")
        self.install_fake_launchers()
        self.env.pop("TMUX")
        self.env.pop("TMUX_PANE")

        result = self.run_command("start", "--launcher", "tmux", task_id, succeeds=False)

        self.assertIn("当前不在 tmux session", result.stderr)
        self.assertIn("tmux new -A -s onevoke", result.stderr)
        self.assertTrue(task.exists())

    def project_session(self, suffix: str = "") -> str:
        project = self.root.resolve().parent
        digest = hashlib.sha256(str(project).encode("utf-8")).hexdigest()[:8]
        label = project.name[:30]
        return f"kb-{label}-{digest}{suffix}" if label else f"kb-{digest}{suffix}"

    def tmux_arguments(self) -> list[str]:
        return (self.root / "tmux.log").read_text(encoding="utf-8").splitlines()

    def test_tmux_session_launcher_creates_the_project_session(self) -> None:
        task_id, task = self.make_todo("session-create")
        fake_bin = self.install_fake_launchers()
        # 项目专属 session 自己建, 因此不在 tmux 里也能启动.
        self.env.pop("TMUX")
        self.env.pop("TMUX_PANE")

        result = self.run_command("start", "--launcher", "tmux-session", task_id)

        session = self.project_session()
        project = str(self.root.resolve().parent)
        self.assertIn(f"已启动: {task_id}", result.stdout)
        self.assertIn(f"会话={session}", result.stdout)
        self.assertIn(f"tmux attach -t {session}", result.stdout)
        arguments = self.tmux_arguments()
        self.assertEqual("new-session", arguments[0])
        self.assertIn("-d", arguments)
        self.assertEqual(session, arguments[arguments.index("-s") + 1])
        self.assertEqual(project, arguments[arguments.index("-c") + 1])
        self.assertEqual("kb-任务-session-create", arguments[arguments.index("-n") + 1])
        command = self.last_launch_command()
        self.assertIn(str(fake_bin / "codex"), command)
        self.assertIn(task_id, command)
        self.assertEqual(
            ["set-option", "-t", session, "@onevoke_project", project],
            (self.root / "tmux.log.setopt").read_text(encoding="utf-8").splitlines(),
        )
        self.assertTrue((self.root / "working" / task.name).exists())

    def test_tmux_session_launcher_reuses_the_project_session(self) -> None:
        task_id, _ = self.make_todo("session-reuse")
        self.install_fake_launchers()
        session = self.project_session()
        self.env["KANBAN_TMUX_SESSIONS"] = session
        self.env["KANBAN_TMUX_PROJECT"] = str(self.root.resolve().parent)

        result = self.run_command("start", "--launcher", "tmux-session", task_id)

        arguments = self.tmux_arguments()
        self.assertEqual("new-window", arguments[0])
        self.assertIn("-d", arguments)
        self.assertEqual(f"{session}:", arguments[arguments.index("-t") + 1])
        self.assertEqual("kb-任务-session-reuse", arguments[arguments.index("-n") + 1])
        self.assertFalse((self.root / "tmux.log.setopt").exists())
        self.assertIn(f"会话={session}", result.stdout)
        # 已在 tmux 里时给切换命令而不是 attach.
        self.assertIn(f"tmux switch-client -t {session}", result.stdout)

    def test_tmux_session_launcher_reuses_an_unmarked_session(self) -> None:
        task_id, _ = self.make_todo("session-unmarked")
        self.install_fake_launchers()
        session = self.project_session()
        self.env["KANBAN_TMUX_SESSIONS"] = session

        self.run_command("start", "--launcher", "tmux-session", task_id)

        arguments = self.tmux_arguments()
        self.assertEqual("new-window", arguments[0])
        self.assertEqual(f"{session}:", arguments[arguments.index("-t") + 1])

    def test_tmux_session_launcher_avoids_another_projects_session(self) -> None:
        task_id, _ = self.make_todo("session-conflict")
        self.install_fake_launchers()
        self.env["KANBAN_TMUX_SESSIONS"] = self.project_session()
        self.env["KANBAN_TMUX_PROJECT"] = "/somewhere/else"

        result = self.run_command("start", "--launcher", "tmux-session", task_id)

        arguments = self.tmux_arguments()
        self.assertEqual("new-session", arguments[0])
        self.assertEqual(self.project_session("-2"), arguments[arguments.index("-s") + 1])
        self.assertIn(f"会话={self.project_session('-2')}", result.stdout)

    def test_tmux_session_launcher_reads_the_machine_config(self) -> None:
        task_id, _ = self.make_todo("session-config")
        self.install_fake_launchers()
        self.write_onevoke_config("codex", "tmux-session")
        self.env.pop("TMUX")
        self.env.pop("TMUX_PANE")

        self.run_command("start", task_id)

        self.assertEqual("new-session", self.tmux_arguments()[0])

    def test_tmux_session_launcher_failure_restores_todo(self) -> None:
        task_id, task = self.make_todo("session-rollback")
        original = task.read_text(encoding="utf-8")
        self.install_fake_launchers()
        self.env["KANBAN_TMUX_FAIL"] = "1"

        result = self.run_command(
            "start", "--launcher", "tmux-session", task_id, succeeds=False
        )

        self.assertIn("tmux new-session 失败", result.stderr)
        self.assertEqual(original, task.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "working" / task.name).exists())

    def test_start_help_lists_auto_launcher(self) -> None:
        self.assertIn("auto", self.run_command("start", "--help").stdout)

    def test_default_auto_launcher_uses_tmux_inside_tmux(self) -> None:
        task_id, task = self.make_todo("default-auto-tmux")
        self.install_fake_launchers()

        result = self.run_command("start", task_id)

        self.assertIn("启动方式=tmux", result.stdout)
        self.assertNotIn("启动方式=auto", result.stdout)
        self.assertEqual("new-window", self.tmux_arguments()[0])
        self.assertIn("-d", self.tmux_arguments())
        self.assertTrue((self.root / "working" / task.name).exists())

    def test_default_auto_launcher_prefers_herdr_inside_herdr(self) -> None:
        task_id, task = self.make_todo("default-auto-herdr")
        self.install_fake_herdr()

        result = self.run_command("start", task_id)

        self.assertIn("启动方式=herdr", result.stdout)
        self.assertNotIn("启动方式=auto", result.stdout)
        self.assertIn("tab=w1:t9", result.stdout)
        create = self.herdr_arguments("create")
        self.assertIn("--no-focus", create)
        self.assertNotIn("--focus", create)
        self.assertFalse((self.root / "tmux.log").exists())
        self.assertTrue((self.root / "working" / task.name).exists())

    def test_auto_launcher_prefers_herdr_even_inside_tmux(self) -> None:
        task_id, task = self.make_todo("auto-herdr-first")
        self.install_fake_herdr()

        result = self.run_command("start", "--launcher", "auto", task_id)

        self.assertIn("启动方式=herdr", result.stdout)
        self.assertIn("tab=w1:t9", result.stdout)
        self.assertTrue((self.root / "herdr.log.create").exists())
        self.assertFalse((self.root / "tmux.log").exists())
        self.assertTrue((self.root / "working" / task.name).exists())

    def test_auto_launcher_uses_tmux_when_not_in_herdr(self) -> None:
        task_id, task = self.make_todo("auto-tmux")
        self.install_fake_launchers()

        result = self.run_command("start", "--launcher", "auto", task_id)

        self.assertIn("启动方式=tmux", result.stdout)
        self.assertNotIn("启动方式=auto", result.stdout)
        self.assertEqual("new-window", self.tmux_arguments()[0])
        self.assertIn("-d", self.tmux_arguments())
        self.assertTrue((self.root / "working" / task.name).exists())

    def test_auto_launcher_without_herdr_or_tmux_does_not_claim(self) -> None:
        task_id, task = self.make_todo("auto-none")
        original = task.read_text(encoding="utf-8")
        self.install_fake_launchers()
        self.env.pop("TMUX")
        self.env.pop("TMUX_PANE")
        self.env.pop("HERDR_ENV", None)

        result = self.run_command("start", task_id, succeeds=False)

        self.assertIn("auto 无法解析", result.stderr)
        self.assertIn("--launcher tmux", result.stderr)
        self.assertIn("tmux-session", result.stderr)
        self.assertIn("herdr", result.stderr)
        self.assertIn("foreground", result.stderr)
        self.assertEqual(original, task.read_text(encoding="utf-8"))
        self.assertTrue(task.exists())
        self.assertFalse((self.root / "working" / task.name).exists())
        self.assertFalse((self.root / "tmux.log").exists())

    def test_existing_tmux_config_is_kept_and_not_rewritten_to_auto(self) -> None:
        task_id, _ = self.make_todo("keep-tmux-config")
        self.install_fake_launchers()
        self.write_onevoke_config("codex", "tmux")

        result = self.run_command("start", task_id)

        self.assertIn("启动方式=tmux", result.stdout)
        config = json.loads(
            (self.home / ".config" / "onevoke" / "config.json").read_text(encoding="utf-8")
        )
        self.assertEqual("tmux", config["launcher"])

    def test_posix_default_config_launcher_is_auto(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import onevoke_config
        finally:
            sys.path.pop(0)
        self.assertEqual("auto", onevoke_config.default_config()["launcher"])
        self.assertIn("auto", onevoke_config.LAUNCHERS)

    def test_herdr_launcher_creates_a_tab_and_runs_the_agent(self) -> None:
        task_id, task = self.make_todo("herdr-start")
        fake_bin = self.install_fake_herdr()

        result = self.run_command("start", "--launcher", "herdr", task_id)

        self.assertIn("herdr", self.run_command("start", "--help").stdout)
        self.assertIn(f"已启动: {task_id}", result.stdout)
        self.assertIn("Agent=codex", result.stdout)
        self.assertIn("启动方式=herdr", result.stdout)
        self.assertIn("tab=w1:t9", result.stdout)
        self.assertIn("pane=w1:p9", result.stdout)
        create = self.herdr_arguments("create")
        self.assertEqual("tab", create[0])
        self.assertEqual("create", create[1])
        self.assertEqual("w1", create[create.index("--workspace") + 1])
        self.assertEqual(str(self.root.resolve().parent), create[create.index("--cwd") + 1])
        self.assertEqual("kb-任务-herdr-start", create[create.index("--label") + 1])
        self.assertIn("--no-focus", create)
        self.assertNotIn("--focus", create)
        wait = self.herdr_arguments("wait")
        self.assertEqual(["pane", "wait-output", "w1:p9"], wait[:3])
        self.assertEqual(r"\S", wait[wait.index("--regex") + 1])
        self.assertEqual("visible", wait[wait.index("--source") + 1])
        self.assertTrue(int(wait[wait.index("--timeout") + 1]) > 0)
        # 先等 pane 就绪再送命令, 否则命令文本会被未就绪的终端丢弃.
        self.assertEqual(
            ["tab create", "pane wait-output", "pane run"],
            self.herdr_arguments("order"),
        )
        run = self.herdr_arguments("run")
        self.assertEqual(["pane", "run", "w1:p9"], run[:3])
        self.assertEqual(4, len(run))
        self.assertFalse(run[3].startswith("--"))
        self.assertIn(str(fake_bin / "codex"), run[3])
        self.assertIn(task_id, run[3])
        self.assertFalse((self.root / "herdr.log.close").exists())
        working = self.root / "working" / task.name
        self.assertTrue(working.exists())
        self.assertIn("- 会话: codex\n- 窗口: herdr:w1:t9:w1:p9\n", working.read_text(encoding="utf-8"))

    def test_herdr_launcher_reports_cursor_session_and_reads_it_back(self) -> None:
        task_id, task = self.make_todo("herdr-report-cursor")
        self.install_fake_herdr()
        self.env["KANBAN_HERDR_AGENT"] = "cursor"
        self.env["KANBAN_HERDR_SESSION"] = "chat-fake-0001"

        with self.fake_herdr_socket() as requests:
            result = self.run_command(
                "start", "--launcher", "herdr", "--agent", "cursor", task_id
            )

        self.assertNotIn("herdr 会话身份上报失败", result.stderr)
        self.assertEqual(1, len(requests))
        request = requests[0]
        self.assertEqual("pane.report_agent_session", request["method"])
        self.assertEqual({
            "pane_id": "w1:p9",
            "source": "herdr:cursor",
            "agent": "cursor",
            "agent_session_id": "chat-fake-0001",
        }, {
            key: request["params"][key]
            for key in ("pane_id", "source", "agent", "agent_session_id")
        })
        self.assertIsInstance(request["params"]["seq"], int)
        self.assertTrue((self.root / "working" / task.name).exists())

    def test_herdr_launcher_skips_session_report_without_reference(self) -> None:
        task_id, _task = self.make_todo("herdr-report-codex")
        self.install_fake_herdr()

        with self.fake_herdr_socket() as requests:
            result = self.run_command("start", "--launcher", "herdr", task_id)

        self.assertEqual([], requests)
        self.assertNotIn("herdr 会话身份上报失败", result.stderr)

    def test_herdr_session_report_socket_failure_only_warns(self) -> None:
        task_id, task = self.make_todo("herdr-report-socket-fail")
        self.install_fake_herdr()
        self.env["HERDR_SOCKET_PATH"] = str(self.root / "missing-herdr.sock")

        result = self.run_command(
            "start", "--launcher", "herdr", "--agent", "cursor", task_id
        )

        self.assertEqual(1, result.stderr.count("警告: herdr 会话身份上报失败"))
        self.assertIn(f"已启动: {task_id}", result.stdout)
        self.assertTrue((self.root / "working" / task.name).exists())
        self.assertFalse((self.root / "herdr.log.close").exists())

    def test_herdr_session_report_non_ok_response_only_warns(self) -> None:
        task_id, task = self.make_todo("herdr-report-response-fail")
        self.install_fake_herdr()

        with self.fake_herdr_socket(response_type="error") as requests:
            result = self.run_command(
                "start", "--launcher", "herdr", "--agent", "cursor", task_id
            )

        self.assertGreater(len(requests), 1)
        self.assertEqual(1, result.stderr.count("警告: herdr 会话身份上报失败"))
        self.assertIn("响应不是 ok", result.stderr)
        self.assertTrue((self.root / "working" / task.name).exists())

    def test_herdr_session_report_slow_fragments_respect_total_budget(self) -> None:
        task_id, task = self.make_todo("herdr-report-slow-response")
        self.install_fake_herdr()
        started = time.monotonic()

        with self.fake_herdr_socket(response_chunks=8, chunk_delay=0.20):
            result = self.run_command(
                "start", "--launcher", "herdr", "--agent", "cursor", task_id
            )

        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.5)
        self.assertEqual(1, result.stderr.count("警告: herdr 会话身份上报失败"))
        self.assertTrue((self.root / "working" / task.name).exists())
        self.assertFalse((self.root / "herdr.log.close").exists())

    def test_herdr_session_report_readback_mismatch_only_warns(self) -> None:
        task_id, task = self.make_todo("herdr-report-mismatch")
        self.install_fake_herdr()
        self.env["KANBAN_HERDR_SESSION"] = "wrong-session"

        with self.fake_herdr_socket() as requests:
            result = self.run_command(
                "start", "--launcher", "herdr", "--agent", "cursor", task_id
            )

        self.assertGreater(len(requests), 1)
        sequences = [request["params"]["seq"] for request in requests]
        self.assertEqual(sequences, sorted(set(sequences)))
        self.assertEqual(1, result.stderr.count("警告: herdr 会话身份上报失败"))
        self.assertIn("读回不一致", result.stderr)
        self.assertTrue((self.root / "working" / task.name).exists())

    def test_herdr_resume_uses_the_shared_session_report_path(self) -> None:
        self.install_fake_herdr()
        task_id, task = self.make_todo("herdr-report-resume")
        self.run_command("start", "--launcher", "tmux", "--agent", "cursor", task_id)
        working = self.root / "working" / task.name
        self.set_branch(working, "herdr-report-resume")
        self.run_command("move", task_id, "review")
        self.env["KANBAN_HERDR_AGENT"] = "cursor"
        self.env["KANBAN_HERDR_SESSION"] = "chat-fake-0001"

        with self.fake_herdr_socket() as requests:
            result = self.run_command(
                "resume",
                "--launcher",
                "herdr",
                "--timeout",
                "61",
                "--message",
                "继续验证",
                task_id,
            )

        self.assertIn(f"已唤醒: {task_id}", result.stdout)
        self.assertEqual(1, len(requests))
        self.assertEqual("chat-fake-0001", requests[0]["params"]["agent_session_id"])
        self.assertTrue((self.root / "working" / task.name).exists())

    def test_herdr_notify_fallback_uses_the_shared_session_report_path(self) -> None:
        self.install_fake_herdr()
        self.write_onevoke_config("cursor", "herdr")
        task_id, task = self.make_todo("herdr-report-notify")
        self.run_command("start", "--launcher", "tmux", "--agent", "cursor", task_id)
        working = self.root / "working" / task.name
        text = working.read_text(encoding="utf-8")
        working.write_text(
            re.sub(r"(?m)^- 窗口:.*$", "- 窗口: foreground", text),
            encoding="utf-8",
        )
        self.set_branch(working, "herdr-report-notify")
        self.run_command("move", task_id, "review")
        self.env["KANBAN_HERDR_AGENT"] = "cursor"
        self.env["KANBAN_HERDR_SESSION"] = "chat-fake-0001"

        with self.fake_herdr_socket() as requests:
            result = self.run_command(
                "notify",
                "--timeout",
                "61",
                "--message",
                "继续验证",
                task_id,
            )

        self.assertIn(f"已通知: {task_id}", result.stdout)
        self.assertIn("通道=resume", result.stdout)
        self.assertEqual(1, len(requests))
        self.assertEqual("chat-fake-0001", requests[0]["params"]["agent_session_id"])
        self.assertTrue((self.root / "working" / task.name).exists())

    def test_herdr_launcher_reads_the_machine_config(self) -> None:
        task_id, _ = self.make_todo("herdr-config")
        self.install_fake_herdr()
        self.write_onevoke_config("codex", "herdr")

        result = self.run_command("start", task_id)

        self.assertIn("启动方式=herdr", result.stdout)
        self.assertIn("tab=w1:t9", result.stdout)

    def test_herdr_launcher_rejects_outside_herdr_before_claiming(self) -> None:
        task_id, task = self.make_todo("herdr-no-env")
        original = task.read_text(encoding="utf-8")
        self.install_fake_herdr()
        self.env.pop("HERDR_ENV")

        result = self.run_command("start", "--launcher", "herdr", task_id, succeeds=False)

        self.assertIn("当前不在 herdr", result.stderr)
        self.assertEqual(original, task.read_text(encoding="utf-8"))
        self.assertTrue(task.exists())
        self.assertFalse((self.root / "working" / task.name).exists())
        self.assertFalse((self.root / "herdr.log.create").exists())

    def test_herdr_launcher_rejects_missing_binary_before_claiming(self) -> None:
        task_id, task = self.make_todo("herdr-no-bin")
        original = task.read_text(encoding="utf-8")
        fake_bin = self.install_fake_launchers()
        self.env["PATH"] = str(fake_bin)
        self.env["HERDR_ENV"] = "1"
        self.env["HERDR_WORKSPACE_ID"] = "w1"

        result = self.run_command("start", "--launcher", "herdr", task_id, succeeds=False)

        self.assertIn("herdr 不在 PATH", result.stderr)
        self.assertEqual(original, task.read_text(encoding="utf-8"))
        self.assertTrue(task.exists())
        self.assertFalse((self.root / "working" / task.name).exists())

    def test_herdr_launcher_rejects_missing_workspace_before_claiming(self) -> None:
        task_id, task = self.make_todo("herdr-no-ws")
        original = task.read_text(encoding="utf-8")
        self.install_fake_herdr()
        self.env.pop("HERDR_WORKSPACE_ID")

        result = self.run_command("start", "--launcher", "herdr", task_id, succeeds=False)

        self.assertIn("缺少 HERDR_WORKSPACE_ID", result.stderr)
        self.assertEqual(original, task.read_text(encoding="utf-8"))
        self.assertTrue(task.exists())
        self.assertFalse((self.root / "working" / task.name).exists())
        self.assertFalse((self.root / "herdr.log.create").exists())

    def test_herdr_tab_create_failure_restores_todo(self) -> None:
        task_id, task = self.make_todo("herdr-create-fail")
        original = task.read_text(encoding="utf-8")
        self.install_fake_herdr()
        self.env["KANBAN_HERDR_CREATE_FAIL"] = "1"

        result = self.run_command(
            "start", "--launcher", "herdr", task_id, succeeds=False
        )

        self.assertIn("herdr tab create 失败", result.stderr)
        self.assertEqual(original, task.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "working" / task.name).exists())
        self.assertFalse((self.root / "herdr.log.close").exists())

    def test_herdr_pane_run_failure_closes_tab_and_restores_todo(self) -> None:
        task_id, task = self.make_todo("herdr-run-fail")
        original = task.read_text(encoding="utf-8")
        self.install_fake_herdr()
        self.env["KANBAN_HERDR_RUN_FAIL"] = "1"

        result = self.run_command(
            "start", "--launcher", "herdr", task_id, succeeds=False
        )

        self.assertIn("herdr pane run 失败", result.stderr)
        self.assertEqual(["tab", "close", "w1:t9"], self.herdr_arguments("close"))
        self.assertEqual(original, task.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "working" / task.name).exists())

    def test_herdr_pane_ready_failure_closes_tab_and_restores_todo(self) -> None:
        task_id, task = self.make_todo("herdr-wait-fail")
        original = task.read_text(encoding="utf-8")
        self.install_fake_herdr()
        self.env["KANBAN_HERDR_WAIT_FAIL"] = "1"

        result = self.run_command(
            "start", "--launcher", "herdr", task_id, succeeds=False
        )

        self.assertIn("herdr pane 未就绪", result.stderr)
        self.assertFalse((self.root / "herdr.log.run").exists())
        self.assertEqual(["tab", "close", "w1:t9"], self.herdr_arguments("close"))
        self.assertEqual(original, task.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "working" / task.name).exists())

    def test_herdr_non_object_create_json_restores_todo(self) -> None:
        task_id, task = self.make_todo("herdr-bad-json")
        original = task.read_text(encoding="utf-8")
        self.install_fake_herdr()
        self.env["KANBAN_HERDR_CREATE_JSON"] = "[]"

        result = self.run_command(
            "start", "--launcher", "herdr", task_id, succeeds=False
        )

        self.assertIn("herdr tab create 失败", result.stderr)
        self.assertIn("JSON object", result.stderr)
        self.assertEqual(original, task.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "working" / task.name).exists())

    def test_herdr_nul_pane_id_closes_tab_and_restores_todo(self) -> None:
        task_id, task = self.make_todo("herdr-nul-id")
        original = task.read_text(encoding="utf-8")
        self.install_fake_herdr()
        self.env["KANBAN_HERDR_CREATE_JSON"] = json.dumps(
            {
                "id": "cli:tab:create",
                "result": {
                    "type": "tab_created",
                    "tab": {
                        "tab_id": "w1:t9",
                        "workspace_id": "w1",
                        "number": 1,
                        "label": "kb",
                        "focused": True,
                        "pane_count": 1,
                        "agent_status": "idle",
                    },
                    "root_pane": {
                        "pane_id": "w1:p9\u0000bad",
                        "terminal_id": "term1",
                        "workspace_id": "w1",
                        "tab_id": "w1:t9",
                        "focused": True,
                        "agent_status": "idle",
                        "revision": 1,
                    },
                },
            }
        )

        result = self.run_command(
            "start", "--launcher", "herdr", task_id, succeeds=False
        )

        self.assertIn("herdr tab create 失败", result.stderr)
        self.assertEqual(["tab", "close", "w1:t9"], self.herdr_arguments("close"))
        self.assertEqual(original, task.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "working" / task.name).exists())

    def test_herdr_close_oserror_still_restores_todo(self) -> None:
        task_id, task = self.make_todo("herdr-close-crash")
        original = task.read_text(encoding="utf-8")
        self.install_fake_herdr()
        self.env["KANBAN_HERDR_RUN_FAIL"] = "1"
        self.env["KANBAN_HERDR_CLOSE_CRASH"] = "1"

        result = self.run_command(
            "start", "--launcher", "herdr", task_id, succeeds=False
        )

        self.assertIn("herdr pane run 失败", result.stderr)
        self.assertIn("关闭 tab w1:t9 失败", result.stderr)
        self.assertEqual(original, task.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "working" / task.name).exists())

    def test_rejects_invalid_transition_and_duplicate_id(self) -> None:
        task_id = f"{datetime.now().strftime('%Y%m%d')}-duplicate-task"
        self.run_command("new", "chore", "duplicate", "重复检测")
        self.run_command("move", task_id, "working", succeeds=False)
        (self.root / "todo" / task_id).mkdir()
        (self.root / "todo" / task_id / "spec.md").write_text("# 重复\n", encoding="utf-8")
        result = self.run_command("check", succeeds=False)
        self.assertIn("重复任务 ID", result.stderr)

    def test_archive_and_trash_require_results(self) -> None:
        task_id = f"{datetime.now().strftime('%Y%m%d')}-retired-task"
        self.run_command("new", "research", "retired", "终止研究")
        task = self.root / "backlog" / f"{task_id}.md"
        self.run_command("move", task_id, "archived", succeeds=False)
        text = task.read_text(encoding="utf-8").replace(
            "- 结果:\n", "- 结果: cancelled\n", 1
        )
        task.write_text(text, encoding="utf-8")
        self.run_command("move", task_id, "archived")
        task = self.root / "archived" / f"{task_id}.md"
        self.run_command("move", task_id, "trash", succeeds=False)
        text = task.read_text(encoding="utf-8").replace(
            "- 结果: cancelled\n", "- 结果: trashed\n", 1
        )
        task.write_text(text, encoding="utf-8")
        self.run_command("move", task_id, "trash")

    def test_discovers_non_git_project_board_from_child_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            board = project / "kanban"
            nested = project / "src" / "nested"
            nested.mkdir(parents=True)
            for state in STATES:
                (board / state).mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env.pop("KANBAN_DIR", None)
            result = subprocess.run(
                [sys.executable, str(COMMAND), "check"],
                cwd=nested,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("通过: 0 个任务\n", result.stdout)

    def test_init_non_git_project_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            project.mkdir()
            env = os.environ.copy()
            env.pop("KANBAN_DIR", None)
            for _ in range(2):
                result = subprocess.run(
                    [sys.executable, str(COMMAND), "init", str(project)],
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("规则: ", result.stdout)
            for state in STATES:
                self.assertTrue((project / "kanban" / state).is_dir())
            self.assertFalse((project / ".git").exists())

    def test_init_git_project_adds_local_exclude_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            project.mkdir()
            subprocess.run(
                ["git", "init", "-q", str(project)],
                text=True,
                capture_output=True,
                check=True,
            )
            env = os.environ.copy()
            env.pop("KANBAN_DIR", None)
            for _ in range(2):
                result = subprocess.run(
                    [sys.executable, str(COMMAND), "init", str(project)],
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(0, result.returncode, result.stderr)
            exclude = (project / ".git" / "info" / "exclude").read_text(encoding="utf-8")
            self.assertEqual(1, exclude.splitlines().count("/kanban/"))

    def test_rules_are_global_and_do_not_require_a_board(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            env = self.env.copy()
            env.pop("KANBAN_DIR", None)
            result = subprocess.run(
                [sys.executable, str(COMMAND), "rules"],
                cwd=temp,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue(result.stdout.startswith("# 全局文件看板规则\n"))

    def test_stray_file_does_not_break_the_whole_board(self) -> None:
        task_id, _ = self.make_todo("healthy")
        (self.root / "backlog" / "notes.md").write_text("随手记", encoding="utf-8")

        listing = self.run_command("list")

        self.assertIn(task_id, listing.stdout)
        self.assertIn("无效入口", listing.stderr)
        self.assertIn("# 任务 healthy", self.run_command("show", task_id).stdout)
        self.run_command("move", task_id, "working")

        sys.path.insert(0, str(COMMAND.parent))
        try:
            kanban = runpy.run_path(str(COMMAND), run_name="kanban_web_scan_warning_test")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                payload = kanban["web_board_payload"](self.root)
                kanban["web_task_payload"](self.root, task_id)
        finally:
            sys.path.pop(0)
        self.assertEqual("", stderr.getvalue())
        self.assertEqual(task_id, payload["tasks"][0]["task_id"])

    def test_check_reports_invalid_entries_and_fails(self) -> None:
        (self.root / "backlog" / "notes.md").write_text("随手记", encoding="utf-8")

        result = self.run_command("check", succeeds=False)

        self.assertEqual(1, result.returncode)
        self.assertIn("notes.md", result.stderr)
        self.assertIn("已检查: 0 个有效, 1 个无效", result.stdout)
        self.assertNotIn("通过:", result.stdout)

    def test_check_passes_on_a_clean_board(self) -> None:
        self.make_todo("clean")

        result = self.run_command("check")

        self.assertEqual("通过: 1 个任务\n", result.stdout)

    def test_dependencies_parse_classify_and_expand_groups(self) -> None:
        source_id, source = self.make_todo("dependency-source")
        internal_id, internal = self.make_todo("dependency-internal")
        external_id, external = self.make_todo("dependency-external")
        group_first_id, group_first = self.make_todo("dependency-group-first")
        group_second_id, group_second = self.make_todo("dependency-group-second")
        source_group = "20260901-source-group"
        expanded_group = "20260901-expanded-group"
        for task in (source, internal):
            self.set_task_group(task, source_group)
        self.set_task_group(external, "20260901-external-group")
        for task in (group_first, group_second):
            self.set_task_group(task, expanded_group)
        self.set_prerequisites(
            source,
            f"{internal_id}, {external_id}, {expanded_group}",
        )
        self.set_prerequisites(internal, "N/A")

        sys.path.insert(0, str(COMMAND.parent))
        try:
            kanban = runpy.run_path(str(COMMAND), run_name="kanban_dependencies")
        finally:
            sys.path.pop(0)
        board = kanban["scan"](self.root)
        dependencies = kanban["task_dependencies"](
            board.entries[source_id], board
        )

        self.assertEqual(
            (internal_id, external_id, expanded_group),
            dependencies.prerequisite_ids,
        )
        self.assertEqual((internal_id,), dependencies.internal_tasks)
        self.assertEqual((external_id,), dependencies.external_tasks)
        self.assertEqual((expanded_group,), dependencies.task_groups)
        self.assertEqual(
            (internal_id, external_id, group_first_id, group_second_id),
            dependencies.expanded_task_ids,
        )
        # 前置均未完成是正常中间态; 缺行旧卡和 N/A 也都按无依赖处理.
        self.assertEqual("通过: 5 个任务\n", self.run_command("check").stdout)

    def test_check_rejects_missing_prerequisite_and_invalid_syntax(self) -> None:
        missing_id, missing = self.make_todo("dependency-missing")
        invalid_id, invalid = self.make_todo("dependency-invalid")
        task_group = "20260901-validation-group"
        self.set_task_group(missing, task_group)
        self.set_task_group(invalid, task_group)
        self.set_prerequisites(missing, "20260901-does-not-exist-task")
        self.set_prerequisites(invalid, "20260901-bad-task，N/A")

        missing_result = self.run_command("check", missing_id, succeeds=False)
        invalid_result = self.run_command("check", invalid_id, succeeds=False)

        self.assertIn(missing_id, missing_result.stderr)
        self.assertIn("20260901-does-not-exist-task", missing_result.stderr)
        self.assertIn(invalid_id, invalid_result.stderr)
        self.assertIn("前置任务语法非法", invalid_result.stderr)

    def test_check_rejects_unfenced_prerequisite(self) -> None:
        task_id, task = self.make_todo("dependency-unfenced")
        self.set_task_group(task, "20260901-unfenced-group")
        text = task.read_text(encoding="utf-8")
        task.write_text(
            text.replace(
                "## 讨论与决策\n\n",
                "## 讨论与决策\n\n前置任务: 20260901-other-task\n\n",
                1,
            ),
            encoding="utf-8",
        )

        result = self.run_command("check", task_id, succeeds=False)

        self.assertIn(task_id, result.stderr)
        self.assertIn("讨论与决策开头的代码块", result.stderr)

    def test_check_detects_cross_group_dependency_cycle(self) -> None:
        first_id, first = self.make_todo("dependency-cycle-first")
        second_id, second = self.make_todo("dependency-cycle-second")
        self.set_task_group(first, "20260901-cycle-first-group")
        self.set_task_group(second, "20260901-cycle-second-group")
        self.set_prerequisites(first, second_id)
        self.set_prerequisites(second, first_id)

        result = self.run_command("check", first_id, succeeds=False)

        self.assertIn("依赖成环", result.stderr)
        self.assertIn(f"{first_id} -> {second_id} -> {first_id}", result.stderr)

    def test_targeted_check_ignores_unrelated_invalid_entries(self) -> None:
        first_id, _ = self.make_todo("targeted-clean")
        second_id, _ = self.make_todo("targeted-second")
        (self.root / "backlog" / "notes.md").write_text("随手记", encoding="utf-8")

        result = self.run_command("check", first_id, second_id)

        self.assertEqual("通过: 2 个任务\n", result.stdout)
        self.assertEqual("", result.stderr)
        self.run_command("check", succeeds=False)

    def test_targeted_check_rejects_missing_duplicate_and_invalid_target(self) -> None:
        task_id, task = self.make_todo("targeted-broken")
        missing = f"{datetime.now().strftime('%Y%m%d')}-missing-target-task"
        self.assertIn(
            "任务不存在",
            self.run_command("check", missing, succeeds=False).stderr,
        )

        duplicate = self.root / "working" / task.name
        duplicate.write_bytes(task.read_bytes())
        self.assertIn(
            "重复任务 ID",
            self.run_command("check", task_id, succeeds=False).stderr,
        )
        duplicate.unlink()

        task.unlink()
        task.mkdir()
        self.assertIn(
            "任务入口类型错误",
            self.run_command("check", task_id, succeeds=False).stderr,
        )

    def test_targeted_check_rejects_symlink_and_missing_large_spec(self) -> None:
        task_id = f"{datetime.now().strftime('%Y%m%d')}-target-link-task"
        outside = self.root.parent / "outside-target.md"
        outside.write_text("outside\n", encoding="utf-8")
        (self.root / "todo" / f"{task_id}.md").symlink_to(outside)
        self.assertIn(
            "符号链接/重解析点",
            self.run_command("check", task_id, succeeds=False).stderr,
        )

        (self.root / "todo" / f"{task_id}.md").unlink()
        (self.root / "todo" / task_id).mkdir()
        self.assertIn(
            "大任务缺少 spec.md",
            self.run_command("check", task_id, succeeds=False).stderr,
        )
        self.assertEqual("outside\n", outside.read_text(encoding="utf-8"))

    def test_targeted_check_rejects_fifo_without_blocking(self) -> None:
        task_id = f"{datetime.now().strftime('%Y%m%d')}-target-fifo-task"
        small = self.root / "todo" / f"{task_id}.md"
        os.mkfifo(small)
        result = self.run_command("check", task_id, succeeds=False, timeout=5)
        self.assertIn("任务入口类型错误", result.stderr)
        small.unlink()

        large = self.root / "todo" / task_id
        large.mkdir()
        os.mkfifo(large / "spec.md")
        result = self.run_command("check", task_id, succeeds=False, timeout=5)
        self.assertIn("spec.md 不是普通文件", result.stderr)

    def test_check_skips_done_and_archived_unless_all(self) -> None:
        (self.root / "done" / "notes.md").write_text("旧笔记", encoding="utf-8")
        (self.root / "archived" / "notes.md").write_text("归档笔记", encoding="utf-8")

        result = self.run_command("check")
        self.assertEqual("通过: 0 个任务\n", result.stdout)
        self.assertEqual("", result.stderr)

        full = self.run_command("check", "--all", succeeds=False)
        self.assertEqual(1, full.returncode)
        self.assertIn("notes.md", full.stderr)
        self.assertIn("已检查: 0 个有效, 2 个无效", full.stdout)

    def test_check_still_reports_trash_invalid_entries(self) -> None:
        (self.root / "done" / "notes.md").write_text("旧笔记", encoding="utf-8")
        (self.root / "trash" / "notes.md").write_text("垃圾笔记", encoding="utf-8")

        result = self.run_command("check", succeeds=False)
        self.assertIn(f"{os.sep}trash{os.sep}notes.md", result.stderr)
        self.assertNotIn(f"{os.sep}done{os.sep}notes.md", result.stderr)

    def test_check_ignores_done_prerequisite_errors_unless_all(self) -> None:
        task_id, task = self.make_todo("old-done-prereq")
        active_id, active = self.make_todo("active-done-prereq")
        group = "20260901-old-done-group"
        self.set_task_group(task, group)
        self.set_task_group(active, group)
        self.run_command("move", task_id, "working")
        working = self.root / "working" / task.name
        self.complete(working)
        self.run_command("move", task_id, "done")
        self.set_prerequisites(self.root / "done" / task.name, "20260901-bad-task，N/A")
        self.set_prerequisites(active, task_id)

        result = self.run_command("check")
        self.assertEqual("通过: 1 个任务\n", result.stdout)
        self.assertEqual("通过: 1 个任务\n", self.run_command("check", active_id).stdout)

        targeted = self.run_command("check", task_id, succeeds=False)
        self.assertIn("前置任务语法非法", targeted.stderr)

        full = self.run_command("check", "--all", succeeds=False)
        self.assertIn("前置任务语法非法", full.stderr)

    def test_check_accepts_done_prerequisites_of_active_cards(self) -> None:
        done_id, done_task = self.make_todo("done-prereq-target")
        active_id, active = self.make_todo("active-with-done-prereq")
        group = "20260901-done-prereq-group"
        self.set_task_group(done_task, group)
        self.set_task_group(active, group)
        self.run_command("move", done_id, "working")
        working = self.root / "working" / done_task.name
        self.complete(working)
        self.run_command("move", done_id, "done")
        self.set_prerequisites(active, done_id)

        result = self.run_command("check")
        self.assertEqual("通过: 1 个任务\n", result.stdout)
        self.assertEqual("通过: 1 个任务\n", self.run_command("check", active_id).stdout)

    def test_check_reports_duplicate_id_across_done_and_active(self) -> None:
        task_id, task = self.make_todo("dup-done")
        self.run_command("move", task_id, "working")
        working = self.root / "working" / task.name
        self.complete(working)
        self.run_command("move", task_id, "done")
        duplicate = self.root / "working" / task.name
        duplicate.write_bytes((self.root / "done" / task.name).read_bytes())

        result = self.run_command("check", succeeds=False)
        self.assertIn("重复任务 ID", result.stderr)

    def test_targeted_scan_retries_a_concurrent_forward_move(self) -> None:
        task_id, task = self.make_todo("target-race")
        sys.path.insert(0, str(COMMAND.parent))
        try:
            kanban = runpy.run_path(str(COMMAND), run_name="kanban_target_race")
        finally:
            sys.path.pop(0)
        original_probe = kanban["regular_file_exists_nofollow"]
        moved = False

        def racing_probe(root: Path, path: Path) -> bool:
            nonlocal moved
            exists = original_probe(root, path)
            if path == task and exists and not moved:
                task.rename(self.root / "working" / task.name)
                moved = True
            return exists

        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch.dict(
                kanban["scan_targets_once"].__globals__,
                {"regular_file_exists_nofollow": racing_probe},
            ):
                board = kanban["scan_targets"](self.root, [task_id])

        self.assertTrue(moved)
        self.assertEqual("working", board.entries[task_id].state)
        self.assertEqual([], board.problems)

    def test_subscribe_rejects_non_finite_intervals(self) -> None:
        task_id = f"{datetime.now().strftime('%Y%m%d')}-interval-task"
        group_id = f"{datetime.now().strftime('%Y%m%d')}-interval-group"
        for option, value in (
            ("--refresh", "nan"),
            ("--refresh", "inf"),
            ("--heartbeat", "nan"),
            ("--heartbeat", "inf"),
        ):
            with self.subTest(option=option, value=value):
                result = self.run_command(
                    "subscribe", option, value, group_id, task_id, succeeds=False
                )
                self.assertIn("间隔必须大于 0", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_subscribe_emits_snapshot_changes_and_heartbeat_only_for_members(self) -> None:
        group_id = f"{datetime.now().strftime('%Y%m%d')}-events-group"
        first_id, first = self.make_todo("event-first")
        second_id, second = self.make_todo("event-second")
        unrelated_id, unrelated = self.make_todo("event-unrelated")
        self.set_task_group(first, group_id)
        self.set_task_group(second, group_id)
        first = first.rename(self.root / "working" / first.name)

        process = subprocess.Popen(
            [
                sys.executable,
                str(COMMAND),
                "subscribe",
                group_id,
                first_id,
                second_id,
                "--refresh",
                "0.5",
                "--heartbeat",
                "1.2",
            ],
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert process.stdout is not None
            snapshot = self.read_process_json(process)
            self.assertEqual("snapshot", snapshot["event"])
            self.assertEqual(group_id, snapshot["group_id"])
            self.assertEqual(
                {first_id: "working", second_id: "todo"}, snapshot["tasks"]
            )

            unrelated.write_text(
                unrelated.read_text(encoding="utf-8") + "\n正文变化\n",
                encoding="utf-8",
            )
            unrelated.rename(self.root / "working" / unrelated.name)
            first.write_text(
                first.read_text(encoding="utf-8") + "\n目标正文变化\n",
                encoding="utf-8",
            )
            ready, _, _ = select.select([process.stdout], [], [], 0.25)
            self.assertEqual([], ready)

            first.rename(self.root / "done" / first.name)
            second.rename(self.root / "working" / second.name)
            changed = self.read_process_json(process)
            self.assertEqual("state-change", changed["event"])
            self.assertEqual(
                [
                    {"from": "working", "task_id": first_id, "to": "done"},
                    {"from": "todo", "task_id": second_id, "to": "working"},
                ],
                changed["changed"],
            )
            self.assertEqual("working", snapshot["tasks"][first_id])
            self.assertEqual("done", changed["tasks"][first_id])

            heartbeat = self.read_process_json(process)
            self.assertEqual("heartbeat", heartbeat["event"])
            self.assertEqual(changed["tasks"], heartbeat["tasks"])
            self.assertNotIn(unrelated_id, heartbeat["tasks"])
            self.assertNotIn("watched", snapshot)
            self.assertNotIn("watched", changed)
            self.assertNotIn("watched", heartbeat)
        finally:
            process.terminate()
            process.communicate(timeout=5)

    def test_subscribe_watches_external_task_changes_and_heartbeat(self) -> None:
        group_id = f"{datetime.now().strftime('%Y%m%d')}-watch-events-group"
        member_id, member = self.make_todo("watch-member")
        external_id, external = self.make_todo("watch-external")
        self.set_task_group(member, group_id)

        process = subprocess.Popen(
            [
                sys.executable,
                str(COMMAND),
                "subscribe",
                group_id,
                member_id,
                "--watch",
                external_id,
                "--refresh",
                "0.2",
                "--heartbeat",
                "0.6",
            ],
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            snapshot = self.read_process_json(process)
            self.assertEqual(
                {member_id: "todo", external_id: "todo"}, snapshot["tasks"]
            )
            self.assertEqual([external_id], snapshot["watched"])

            external.rename(self.root / "working" / external.name)
            changed = self.read_process_json(process)
            self.assertEqual(
                [{"from": "todo", "task_id": external_id, "to": "working"}],
                changed["changed"],
            )
            self.assertEqual([external_id], changed["watched"])

            heartbeat = self.read_process_json(process)
            self.assertEqual("heartbeat", heartbeat["event"])
            self.assertEqual(changed["tasks"], heartbeat["tasks"])
            self.assertEqual([external_id], heartbeat["watched"])
        finally:
            process.terminate()
            process.communicate(timeout=5)

    def test_direct_watch_ignores_unrelated_invalid_document(self) -> None:
        group_id = f"{datetime.now().strftime('%Y%m%d')}-watch-isolation-group"
        member_id, member = self.make_todo("watch-isolation-member")
        external_id, _ = self.make_todo("watch-isolation-external")
        _, unrelated = self.make_todo("watch-isolation-invalid")
        self.set_task_group(member, group_id)
        unrelated.write_bytes(b"\xff")

        process = subprocess.Popen(
            [
                sys.executable,
                str(COMMAND),
                "subscribe",
                group_id,
                member_id,
                "--watch",
                external_id,
            ],
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            snapshot = self.read_process_json(process)
            self.assertEqual(
                {member_id: "todo", external_id: "todo"}, snapshot["tasks"]
            )
            self.assertEqual([external_id], snapshot["watched"])
        finally:
            process.terminate()
            process.communicate(timeout=5)

    def test_subscribe_expands_watched_task_group(self) -> None:
        group_id = f"{datetime.now().strftime('%Y%m%d')}-watch-source-group"
        watched_group = f"{datetime.now().strftime('%Y%m%d')}-watched-group"
        member_id, member = self.make_todo("watch-group-member")
        first_id, first = self.make_todo("watched-group-first")
        second_id, second = self.make_todo("watched-group-second")
        self.set_task_group(member, group_id)
        self.set_task_group(first, watched_group)
        self.set_task_group(second, watched_group)

        process = subprocess.Popen(
            [
                sys.executable,
                str(COMMAND),
                "subscribe",
                group_id,
                member_id,
                "--watch",
                watched_group,
            ],
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            snapshot = self.read_process_json(process)
            self.assertEqual([first_id, second_id], snapshot["watched"])
            self.assertEqual(
                {member_id: "todo", first_id: "todo", second_id: "todo"},
                snapshot["tasks"],
            )
        finally:
            process.terminate()
            process.communicate(timeout=5)

    def test_group_watch_ignores_unrelated_invalid_document(self) -> None:
        group_id = f"{datetime.now().strftime('%Y%m%d')}-group-isolation-group"
        watched_group = f"{datetime.now().strftime('%Y%m%d')}-watched-isolation-group"
        member_id, member = self.make_todo("group-isolation-member")
        external_id, external = self.make_todo("group-isolation-external")
        _, unrelated = self.make_todo("group-isolation-invalid")
        self.set_task_group(member, group_id)
        self.set_task_group(external, watched_group)
        unrelated.write_bytes(b"\xff")

        process = subprocess.Popen(
            [
                sys.executable,
                str(COMMAND),
                "subscribe",
                group_id,
                member_id,
                "--watch",
                watched_group,
            ],
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            snapshot = self.read_process_json(process)
            self.assertEqual(
                {member_id: "todo", external_id: "todo"}, snapshot["tasks"]
            )
            self.assertEqual([external_id], snapshot["watched"])
        finally:
            process.terminate()
            process.communicate(timeout=5)

    def test_subscribe_rejects_invalid_watched_targets(self) -> None:
        date = datetime.now().strftime("%Y%m%d")
        group_id = f"{date}-watch-reject-group"
        member_id, member = self.make_todo("watch-reject-member")
        self.set_task_group(member, group_id)

        missing = self.run_command(
            "subscribe",
            group_id,
            member_id,
            "--watch",
            f"{date}-missing-watch-task",
            succeeds=False,
        )
        duplicate = self.run_command(
            "subscribe",
            group_id,
            member_id,
            "--watch",
            member_id,
            succeeds=False,
        )
        empty_group = self.run_command(
            "subscribe",
            group_id,
            member_id,
            "--watch",
            f"{date}-empty-watch-group",
            succeeds=False,
        )

        self.assertIn("任务不存在", missing.stderr)
        self.assertIn("外部监控目标与成员任务重复", duplicate.stderr)
        self.assertIn("外部监控任务组没有成员", empty_group.stderr)

    def test_subscribe_rejects_invalid_group_membership_and_duplicate_members(self) -> None:
        group_id = f"{datetime.now().strftime('%Y%m%d')}-events-group"
        task_id, _ = self.make_todo("event-wrong-group")

        wrong_group = self.run_command(
            "subscribe", group_id, task_id, succeeds=False
        )
        self.assertIn("任务不属于指定任务组", wrong_group.stderr)
        duplicated = self.run_command(
            "subscribe", group_id, task_id, task_id, succeeds=False
        )
        self.assertIn("成员任务 ID 不得重复", duplicated.stderr)

    def test_duplicate_task_id_blocks_only_that_task(self) -> None:
        duplicated, todo_path = self.make_todo("dup")
        healthy, _ = self.make_todo("fine")
        (self.root / "working" / todo_path.name).write_bytes(todo_path.read_bytes())

        listing = self.run_command("list")
        blocked = self.run_command("show", duplicated, succeeds=False)

        self.assertNotIn(duplicated, listing.stdout)
        self.assertIn(healthy, listing.stdout)
        self.assertIn("重复任务 ID", blocked.stderr)
        self.run_command("move", healthy, "working")

    def test_large_task_without_spec_blocks_only_that_task(self) -> None:
        healthy, _ = self.make_todo("fine")
        broken = f"{datetime.now().strftime('%Y%m%d')}-broken-task"
        (self.root / "backlog" / broken).mkdir()

        listing = self.run_command("list")
        blocked = self.run_command("show", broken, succeeds=False)

        self.assertIn(healthy, listing.stdout)
        self.assertIn("大任务缺少 spec.md", blocked.stderr)

    def test_symlink_spec_is_rejected_and_outside_bytes_stay_intact(self) -> None:
        healthy, _ = self.make_todo("fine")
        outside = self.root.parent / "outside-secret.md"
        secret = "do-not-touch-external-target\n"
        outside.write_text(secret, encoding="utf-8")
        task_id = f"{datetime.now().strftime('%Y%m%d')}-symlink-spec-task"
        task_dir = self.root / "todo" / task_id
        task_dir.mkdir()
        (task_dir / "spec.md").symlink_to(outside)

        check = self.run_command("check", succeeds=False)
        show = self.run_command("show", task_id, succeeds=False)
        start = self.run_command("start", task_id, succeeds=False)

        self.assertIn("spec.md 不得是符号链接", check.stderr)
        self.assertIn("已检查:", check.stdout)
        self.assertIn("spec.md 不得是符号链接", show.stderr)
        self.assertIn("spec.md 不得是符号链接", start.stderr)
        self.assertIn(healthy, self.run_command("list").stdout)
        self.assertEqual(secret, outside.read_text(encoding="utf-8"))

    def test_document_replaced_with_symlink_is_rejected_on_read(self) -> None:
        # 大任务: 入口目录合法, 仅 spec.md 在 scan 后被换成看板外软链.
        task_id = f"{datetime.now().strftime('%Y%m%d')}-swap-link-task"
        task_dir = self.root / "todo" / task_id
        task_dir.mkdir()
        spec = task_dir / "spec.md"
        contract = """# 任务 swap-link

- 类型: Bug
- 创建时间: 2026-08-11 00:00
- 负责人:
- 开始时间:
- 完成时间:
- 任务分支:
- 结果:

## 任务目标

目标

## 用户决策

N/A

## 预期成果

成果

## 验收条件

- [ ] 条件

## 威胁模型

N/A

## 不在本轮范围

- 无

## 讨论与决策

N/A

## 实施与验证

N/A

## 完成总结

"""
        spec.write_text(contract, encoding="utf-8")
        outside = self.root.parent / "swap-secret.md"
        outside.write_text("external\n", encoding="utf-8")
        self.assertIn("# 任务 swap-link", self.run_command("show", task_id).stdout)
        spec.unlink()
        spec.symlink_to(outside)

        show = self.run_command("show", task_id, succeeds=False)
        self.assertTrue(
            "不得是符号链接" in show.stderr or "符号链接" in show.stderr,
            show.stderr,
        )
        self.assertEqual("external\n", outside.read_text(encoding="utf-8"))

    def test_document_read_rejects_board_root_symlink_swap(self) -> None:
        task_id, task = self.make_todo("root-swap")
        sys.path.insert(0, str(COMMAND.parent))
        try:
            kanban = runpy.run_path(str(COMMAND), run_name="kanban_root_swap")
        finally:
            sys.path.pop(0)
        entry = kanban["Entry"](task_id, "todo", task, task, "small")
        original_root = self.root.with_name(f"{self.root.name}-original")
        outside = self.root.with_name(f"{self.root.name}-outside")
        outside_task = outside / "todo" / task.name
        outside_task.parent.mkdir(parents=True)
        outside_task.write_text("outside sentinel\n", encoding="utf-8")
        real_open = os.open
        swapped = False

        def swap_before_root_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if Path(path) == self.root and not swapped:
                self.root.rename(original_root)
                self.root.symlink_to(outside, target_is_directory=True)
                swapped = True
            return real_open(path, flags, *args, **kwargs)

        try:
            with mock.patch.object(
                kanban["os"], "open", side_effect=swap_before_root_open
            ):
                with self.assertRaises(OSError):
                    kanban["read_document"](entry)
            self.assertEqual(
                "outside sentinel\n", outside_task.read_text(encoding="utf-8")
            )
        finally:
            if self.root.is_symlink():
                self.root.unlink()
            if original_root.exists():
                original_root.rename(self.root)
            shutil.rmtree(outside)

        self.assertTrue(swapped)

    def test_document_read_derives_boundary_without_path_resolve(self) -> None:
        task_id, task = self.make_todo("lexical-boundary")
        sys.path.insert(0, str(COMMAND.parent))
        try:
            kanban = runpy.run_path(str(COMMAND), run_name="kanban_lexical_boundary")
        finally:
            sys.path.pop(0)
        entry = kanban["Entry"](task_id, "todo", task, task, "small")

        with mock.patch.object(
            Path,
            "resolve",
            side_effect=AssertionError("安全路径派生不得调用 Path.resolve"),
        ):
            document = kanban["read_document"](entry)

        self.assertIn("# 任务 lexical-boundary", document)

    def test_write_text_atomic_rejects_document_symlink(self) -> None:
        task_id, task = self.make_todo("write-link")
        outside = self.root / "write-secret.md"
        outside.write_text("keep-me\n", encoding="utf-8")
        self.run_command("move", task_id, "working")
        working = self.root / "working" / task.name
        working.unlink()
        working.symlink_to(outside)

        import runpy
        import sys as _sys

        _sys.path.insert(0, str(COMMAND.parent))
        try:
            kanban = runpy.run_path(str(COMMAND), run_name="kanban_write_link_test")
        finally:
            _sys.path.pop(0)
        entry = kanban["Entry"](
            task_id, "working", working, working, "small"
        )
        with self.assertRaises(kanban["KanbanError"]) as raised:
            kanban["write_text_atomic"](working, "# rewritten\n", entry=entry)

        self.assertIn("符号链接", str(raised.exception))
        self.assertEqual("keep-me\n", outside.read_text(encoding="utf-8"))

    def test_missing_review_directory_is_created_on_first_use(self) -> None:
        task_id, task = self.make_todo("legacy-board")
        review = self.root / "review"
        shutil.rmtree(review)

        listed = self.run_command("list")

        self.assertTrue(review.is_dir(), "旧看板缺 review/ 时应自动补建")
        self.assertIn(task_id, listed.stdout)
        self.assertEqual("ok: 1 tasks\n", self.run_command("--lang", "en", "check").stdout)
        if os.name == "posix":
            self.assertTrue(os.access(review, os.W_OK))

    def test_other_missing_state_directories_still_fail(self) -> None:
        self.make_todo("broken-board")
        shutil.rmtree(self.root / "review")
        shutil.rmtree(self.root / "archived")

        result = self.run_command("list", succeeds=False)

        self.assertIn("状态目录不存在", result.stderr)
        self.assertFalse((self.root / "review").exists(), "其他目录缺失时不得补建 review")

    def test_review_path_occupied_by_a_file_or_symlink_is_rejected(self) -> None:
        self.make_todo("occupied-review")
        review = self.root / "review"
        shutil.rmtree(review)
        review.write_text("", encoding="utf-8")
        as_file = self.run_command("list", succeeds=False)
        review.unlink()
        outside = self.root / "outside-review"
        outside.mkdir()
        review.symlink_to(outside)
        as_link = self.run_command("list", succeeds=False)

        self.assertIn("状态路径不是目录", as_file.stderr)
        self.assertIn("状态目录不得是符号链接", as_link.stderr)
        self.assertEqual([], list(outside.iterdir()))

    def test_state_directory_symlink_is_rejected_by_check_and_start(self) -> None:
        task_id, task = self.make_todo("state-link")
        outside = self.root / "evil-working-outside"
        outside.mkdir()
        working = self.root / "working"
        shutil.rmtree(working)
        working.symlink_to(outside)
        self.install_fake_launchers()

        check = self.run_command("check", succeeds=False)
        start = self.run_command("start", task_id, succeeds=False)

        self.assertIn("状态目录不得是符号链接", check.stderr)
        self.assertIn("状态目录不得是符号链接", start.stderr)
        self.assertTrue(task.exists())
        self.assertEqual([], list(outside.iterdir()))

    def test_task_directory_swapped_for_symlink_is_rejected(self) -> None:
        task_id = f"{datetime.now().strftime('%Y%m%d')}-dir-swap-task"
        task_dir = self.root / "todo" / task_id
        task_dir.mkdir()
        (task_dir / "spec.md").write_text(
            "# 任务 dir-swap\n\n"
            "- 类型: Bug\n- 创建时间: 2026-08-11 00:00\n- 负责人:\n"
            "- 开始时间:\n- 完成时间:\n- 任务分支:\n- 结果:\n\n"
            "## 任务目标\n\n目标\n\n## 用户决策\n\nN/A\n\n"
            "## 预期成果\n\n成果\n\n## 验收条件\n\n- [ ] 条件\n\n"
            "## 威胁模型\n\nN/A\n\n## 不在本轮范围\n\n- 无\n\n"
            "## 讨论与决策\n\nN/A\n\n## 实施与验证\n\nN/A\n\n"
            "## 完成总结\n\n",
            encoding="utf-8",
        )
        outside_dir = self.root / "evil-task-outside"
        outside_dir.mkdir()
        (outside_dir / "spec.md").write_text("# evil\n", encoding="utf-8")
        self.assertIn("# 任务 dir-swap", self.run_command("show", task_id).stdout)
        # 模拟 scan 后把任务目录换成指向外部的软链.
        shutil.rmtree(task_dir)
        task_dir.symlink_to(outside_dir)

        show = self.run_command("show", task_id, succeeds=False)
        self.assertTrue(
            "符号链接" in show.stderr or "无效" in show.stderr or "不存在" in show.stderr,
            show.stderr,
        )
        self.assertEqual("# evil\n", (outside_dir / "spec.md").read_text(encoding="utf-8"))

    def test_symlink_entry_is_rejected_without_blocking_others(self) -> None:
        healthy, todo_path = self.make_todo("fine")
        link = self.root / "backlog" / f"{datetime.now().strftime('%Y%m%d')}-link-task.md"
        link.symlink_to(todo_path)

        result = self.run_command("check", succeeds=False)

        self.assertIn("符号链接", result.stderr)
        self.assertIn(healthy, self.run_command("list").stdout)

    def test_installer_copies_command_and_rules(self) -> None:
        install_home = self.root / "install-home"
        legacy_bin = install_home / ".local" / "bin"
        legacy_bin.mkdir(parents=True)
        for name in ("codex-review.sh", "claude-review.sh", "grok-review.sh"):
            (legacy_bin / name).write_text("legacy\n", encoding="utf-8")
        env = install_env(install_home)
        result = subprocess.run(
            ["sh", str(INSTALLER)],
            stdin=subprocess.DEVNULL,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(INSTALLED_ZH, result.stdout)
        for name in ("codex-review.sh", "claude-review.sh", "grok-review.sh"):
            self.assertEqual(
                "legacy\n",
                (legacy_bin / name).read_text(encoding="utf-8"),
                name,
            )
        self.assertIn("检测到已退役的 Reviewer 脚本", result.stderr)
        self.assertIn("已保留旧 Reviewer 脚本", result.stderr)

        command = install_home / ".local" / "bin" / "kanban"
        for source in sorted((PROJECT_ROOT / "bin").iterdir()):
            if source.is_file():
                installed = install_home / ".local" / "bin" / source.name
                self.assertEqual(source.read_bytes(), installed.read_bytes(), source.name)
                self.assertTrue(os.access(installed, os.X_OK), source.name)
        # rules/ 下每份规则都必须被安装; 新增规则文件时无需改测试.
        for source in sorted(RULES_DIR.glob("*.md")):
            self.assertEqual(
                source.read_bytes(),
                (install_home / ".agents" / source.name).read_bytes(),
                source.name,
            )
        share_dir = install_home / ".local" / "share" / "onevoke" / "kanban-web"
        for source in sorted((PROJECT_ROOT / "share" / "kanban-web").iterdir()):
            if source.is_file():
                self.assertEqual(
                    source.read_bytes(),
                    (share_dir / source.name).read_bytes(),
                    source.name,
                )
        own_rules = install_home / ".agents" / "AGENTS.md"
        self.assertTrue(own_rules.is_symlink())
        self.assertEqual(Path("ONEVOKE-AGENTS.md"), own_rules.readlink())
        self.assertEqual(AGENT_RULES.read_bytes(), own_rules.read_bytes())

        self.assertIn("请在终端运行 onevoke welcome", result.stderr)

        output = subprocess.run(
            [str(command), "rules"],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, output.returncode, output.stderr)
        self.assertEqual(RULES.read_text(encoding="utf-8"), output.stdout)

    def test_installer_deletes_legacy_review_scripts_after_confirmation(self) -> None:
        install_home = self.root / "legacy-confirm-home"
        legacy_bin = install_home / ".local" / "bin"
        legacy_bin.mkdir(parents=True)
        names = ("codex-review.sh", "claude-review.sh", "grok-review.sh")
        for name in names:
            (legacy_bin / name).write_text("legacy\n", encoding="utf-8")

        result = subprocess.run(
            ["sh", str(INSTALLER)],
            input="y\n",
            env=install_env(install_home),
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(INSTALLED_ZH, result.stdout)
        self.assertIn("是否删除这些旧脚本", result.stderr)
        self.assertIn("已删除旧 Reviewer 脚本", result.stderr)
        for name in names:
            self.assertFalse((legacy_bin / name).exists(), name)

    def test_installer_keeps_legacy_review_scripts_when_install_fails(self) -> None:
        install_home = self.root / "legacy-install-failure-home"
        legacy_bin = install_home / ".local" / "bin"
        legacy_bin.mkdir(parents=True)
        names = ("codex-review.sh", "claude-review.sh", "grok-review.sh")
        for name in names:
            (legacy_bin / name).write_text("legacy\n", encoding="utf-8")
        fake_bin = self.root / "failing-install-bin"
        fake_bin.mkdir()
        fake_install = fake_bin / "install"
        fake_install.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        fake_install.chmod(0o755)

        result = subprocess.run(
            ["sh", str(INSTALLER)],
            input="y\n",
            env={
                **os.environ,
                "HOME": str(install_home),
                "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
            },
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("已删除旧 Reviewer 脚本", result.stderr)
        for name in names:
            self.assertEqual(
                "legacy\n",
                (legacy_bin / name).read_text(encoding="utf-8"),
                name,
            )

    def test_installer_skips_non_file_rule_matches(self) -> None:
        project = self.root / "installer-project"
        (project / "bin").mkdir(parents=True)
        (project / "rules" / "ignored.md").mkdir(parents=True)
        (project / "install.sh").write_bytes(INSTALLER.read_bytes())
        (project / "bin" / "onevoke").write_text(
            "#!/bin/sh\nexit 0\n", encoding="utf-8"
        )
        (project / "bin" / "onevoke").chmod(0o755)
        (project / "rules" / "REAL.md").write_text("# real\n", encoding="utf-8")
        install_home = self.root / "non-file-rule-home"

        result = subprocess.run(
            ["sh", str(project / "install.sh")],
            stdin=subprocess.DEVNULL,
            env={**os.environ, "HOME": str(install_home)},
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue((install_home / ".agents" / "REAL.md").is_file())
        self.assertFalse((install_home / ".agents" / "ignored.md").exists())

    def test_installer_preserves_existing_agent_rules(self) -> None:
        install_home = self.root / "user-home"
        env = os.environ.copy()
        env["HOME"] = str(install_home)
        own_rules = install_home / ".agents" / "AGENTS.md"
        own_rules.parent.mkdir(parents=True)
        own_rules.write_text("本机自定规则\n", encoding="utf-8")

        for _ in range(2):
            result = subprocess.run(
                ["sh", str(INSTALLER)],
                stdin=subprocess.DEVNULL,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("本机自定规则\n", own_rules.read_text(encoding="utf-8"))
            self.assertNotIn(str(own_rules), result.stderr)

        self.assertEqual(
            AGENT_RULES.read_bytes(),
            (install_home / ".agents" / "ONEVOKE-AGENTS.md").read_bytes(),
        )

    def test_installer_preserves_agent_rules_directory(self) -> None:
        install_home = self.root / "rules-directory-home"
        env = os.environ.copy()
        env["HOME"] = str(install_home)
        own_rules = install_home / ".agents" / "AGENTS.md"
        own_rules.mkdir(parents=True)
        marker = own_rules / "keep"
        marker.write_text("preserved\n", encoding="utf-8")

        result = subprocess.run(
            ["sh", str(INSTALLER)],
            stdin=subprocess.DEVNULL,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(own_rules.is_dir())
        self.assertEqual("preserved\n", marker.read_text(encoding="utf-8"))

    def test_installer_preserves_dangling_agent_rules_symlink(self) -> None:
        install_home = self.root / "dangling-rules-home"
        env = os.environ.copy()
        env["HOME"] = str(install_home)
        own_rules = install_home / ".agents" / "AGENTS.md"
        own_rules.parent.mkdir(parents=True)
        own_rules.symlink_to("missing-user-rules.md")

        result = subprocess.run(
            ["sh", str(INSTALLER)],
            stdin=subprocess.DEVNULL,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(own_rules.is_symlink())
        self.assertEqual(Path("missing-user-rules.md"), own_rules.readlink())

    def test_installer_reports_welcome_failure_without_undoing_install(self) -> None:
        install_home = self.root / "welcome-failure-home"
        config = install_home / ".config" / "onevoke" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text("not json\n", encoding="utf-8")

        result = subprocess.run(
            ["sh", str(INSTALLER)],
            stdin=subprocess.DEVNULL,
            env=install_env(install_home),
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(INSTALLED_ZH, result.stdout)
        self.assertIn("welcome 未完成", result.stderr)
        self.assertTrue((install_home / ".local" / "bin" / "onevoke").exists())

    def test_installer_always_overwrites_the_entry_rule(self) -> None:
        install_home = self.root / "overwrite-home"
        entry = install_home / ".agents" / "ONEVOKE-AGENTS.md"
        entry.parent.mkdir(parents=True)
        entry.write_text("用户旧配置\n", encoding="utf-8")

        result = subprocess.run(
            ["sh", str(INSTALLER)],
            stdin=subprocess.DEVNULL,
            env={**os.environ, "HOME": str(install_home)},
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(AGENT_RULES.read_bytes(), entry.read_bytes())

    def test_installer_rejects_a_directory_at_a_file_target(self) -> None:
        install_home = self.root / "bad-target-home"
        entry = install_home / ".agents" / "ONEVOKE-AGENTS.md"
        entry.mkdir(parents=True)

        result = subprocess.run(
            ["sh", str(INSTALLER)],
            stdin=subprocess.DEVNULL,
            env={**os.environ, "HOME": str(install_home)},
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(1, result.returncode)
        self.assertIn("安装目标是目录", result.stderr)
        self.assertFalse((install_home / ".local" / "bin").exists())
        self.assertTrue(entry.is_dir())

    def test_installer_rejects_a_directory_at_a_legacy_review_target(self) -> None:
        install_home = self.root / "legacy-directory-home"
        legacy_target = install_home / ".local" / "bin" / "codex-review.sh"
        legacy_target.mkdir(parents=True)

        result = subprocess.run(
            ["sh", str(INSTALLER)],
            stdin=subprocess.DEVNULL,
            env={**os.environ, "HOME": str(install_home)},
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(1, result.returncode)
        self.assertIn("旧版安装目标是目录", result.stderr)
        self.assertFalse((install_home / ".local" / "bin" / "onevoke").exists())
        self.assertTrue(legacy_target.is_dir())

    def test_installer_rejects_arguments(self) -> None:
        chinese_help = subprocess.run(
            ["sh", str(INSTALLER), "--lang", "cn", "--help"],
            env={
                **os.environ,
                "HOME": str(self.root / "help-home-cn"),
                "ONEVOKE_LANG": "en",
            },
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, chinese_help.returncode, chinese_help.stderr)
        self.assertIn("用法: install.sh", chinese_help.stdout)

        help_result = subprocess.run(
            ["sh", str(INSTALLER), "--lang", "en", "--help"],
            env={**os.environ, "HOME": str(self.root / "help-home")},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, help_result.returncode, help_result.stderr)
        self.assertIn("--lang {cn,en}", help_result.stdout)

        result = subprocess.run(
            ["sh", str(INSTALLER), "--force"],
            stdin=subprocess.DEVNULL,
            env={**os.environ, "HOME": str(self.root / "arg-home")},
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("用法: install.sh", result.stderr)

        english = subprocess.run(
            ["sh", str(INSTALLER), "--force"],
            stdin=subprocess.DEVNULL,
            env={
                **os.environ,
                "HOME": str(self.root / "arg-home-en"),
                "ONEVOKE_LANG": "en",
                "LC_ALL": "zh_CN.UTF-8",
            },
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(2, english.returncode)
        self.assertIn("usage: install.sh", english.stderr)
        self.assertNotIn("用法", english.stderr)

        fallbacks = (
            {"ONEVOKE_LANG": "zh", "LC_ALL": "en"},
            {"ONEVOKE_LANG": "", "LC_ALL": "zh", "LC_MESSAGES": "en"},
            {"ONEVOKE_LANG": "", "LC_ALL": "", "LC_MESSAGES": "zh", "LANG": "en"},
            {"ONEVOKE_LANG": "", "LC_ALL": "", "LC_MESSAGES": "", "LANG": "zh"},
            {"ONEVOKE_LANG": "", "LC_ALL": "", "LC_MESSAGES": "", "LANG": ""},
        )
        for index, locale_env in enumerate(fallbacks):
            localized = subprocess.run(
                ["sh", str(INSTALLER), "--force"],
                stdin=subprocess.DEVNULL,
                env={
                    **os.environ,
                    "HOME": str(self.root / f"arg-home-locale-{index}"),
                    **locale_env,
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(2, localized.returncode)
            self.assertIn("用法: install.sh", localized.stderr)

        for mixed_case in ("En_US.UTF-8", "eN_US.UTF-8"):
            english_mixed = subprocess.run(
                ["sh", str(INSTALLER), "--force"],
                stdin=subprocess.DEVNULL,
                env={
                    **os.environ,
                    "HOME": str(self.root / f"arg-home-mixed-{mixed_case}"),
                    "ONEVOKE_LANG": "",
                    "LC_ALL": "",
                    "LC_MESSAGES": "",
                    "LANG": mixed_case,
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(2, english_mixed.returncode)
            self.assertIn("usage: install.sh", english_mixed.stderr)
            self.assertNotIn("用法", english_mixed.stderr)

        missing = subprocess.run(
            ["sh", str(INSTALLER), "--lang"],
            env={**os.environ, "HOME": str(self.root / "arg-home-missing")},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(2, missing.returncode)
        self.assertIn("用法: install.sh", missing.stderr)

        invalid = subprocess.run(
            ["sh", str(INSTALLER), "--lang", "fr"],
            env={
                **os.environ,
                "HOME": str(self.root / "arg-home-invalid"),
                "ONEVOKE_LANG": "en",
            },
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(2, invalid.returncode)
        self.assertIn("--lang must be cn or en", invalid.stderr)

    def test_installer_passes_explicit_language_to_welcome(self) -> None:
        project = self.root / "lang-installer-project"
        (project / "bin").mkdir(parents=True)
        (project / "install.sh").write_bytes(INSTALLER.read_bytes())
        (project / "bin" / "onevoke").write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$WELCOME_ARGS\"\n",
            encoding="utf-8",
        )
        (project / "bin" / "onevoke").chmod(0o755)
        welcome_args = self.root / "welcome-args"

        result = subprocess.run(
            ["sh", str(project / "install.sh"), "--lang", "cn"],
            env={
                **os.environ,
                "HOME": str(self.root / "lang-install-home"),
                "WELCOME_ARGS": str(welcome_args),
            },
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("--lang\ncn\nwelcome\n", welcome_args.read_text(encoding="utf-8"))

    def test_installer_success_message_follows_locale(self) -> None:
        english = subprocess.run(
            ["sh", str(INSTALLER)],
            stdin=subprocess.DEVNULL,
            env=install_env(self.root / "install-en-home", ONEVOKE_LANG="en"),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, english.returncode, english.stderr)
        self.assertEqual(INSTALLED_EN, english.stdout)
        self.assertNotIn("已安装", english.stdout)

    def test_installer_success_message_uses_config_language(self) -> None:
        install_home = self.root / "install-config-lang-home"
        config_dir = install_home / ".config" / "onevoke"
        config_dir.mkdir(parents=True)
        config_dir.joinpath("config.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "welcome_complete": True,
                    "kanban_agent": "codex",
                    "launcher": "tmux",
                    "language": "en",
                    "reviewers": {role: "codex" for role in ("PM", "CSA", "Hacker", "QA")},
                    "memsearch": {"enabled": False},
                }
            ),
            encoding="utf-8",
        )

        result = subprocess.run(
            ["sh", str(INSTALLER)],
            stdin=subprocess.DEVNULL,
            env=install_env(install_home, ONEVOKE_LANG="zh"),
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(INSTALLED_EN, result.stdout)

    def test_installer_ignores_invalid_config_language(self) -> None:
        install_home = self.root / "install-invalid-config-home"
        config_dir = install_home / ".config" / "onevoke"
        config_dir.mkdir(parents=True)
        config_dir.joinpath("config.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "welcome_complete": True,
                    "kanban_agent": "invalid",
                    "launcher": "tmux",
                    "language": "cn",
                    "reviewers": {role: "codex" for role in ("PM", "CSA", "Hacker", "QA")},
                    "memsearch": {"enabled": False},
                }
            ),
            encoding="utf-8",
        )

        result = subprocess.run(
            ["sh", str(INSTALLER)],
            stdin=subprocess.DEVNULL,
            env=install_env(install_home, ONEVOKE_LANG="en"),
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(INSTALLED_EN, result.stdout)

    def test_web_help_and_invalid_refresh(self) -> None:
        help_text = self.run_command("web", "--help").stdout
        self.assertIn("--host", help_text)
        self.assertIn("--port", help_text)
        self.assertIn("--refresh", help_text)
        self.assertIn("默认 60", help_text)
        bad = self.run_command("web", "--refresh", "0", succeeds=False)
        self.assertIn("扫描间隔", bad.stderr)

    def test_tui_help_and_rejects_invalid_or_noninteractive_use(self) -> None:
        help_text = self.run_command("tui", "--help").stdout
        self.assertIn("--single", help_text)
        self.assertIn("--refresh", help_text)
        self.assertIn("默认 30", help_text)
        self.assertIn("--theme", help_text)

        bad_refresh = self.run_command("tui", "--refresh", "0", succeeds=False)
        self.assertIn("刷新间隔", bad_refresh.stderr)
        bad_theme = self.run_command("tui", "--theme", "sepia", succeeds=False)
        self.assertIn("--theme", bad_theme.stderr)
        self.assertIn("sepia", bad_theme.stderr)
        noninteractive = self.run_command("tui", succeeds=False)
        self.assertIn("TUI 需要交互终端", noninteractive.stderr)
        self.assertIn("stdin/stdout 均为 tty", noninteractive.stderr)

    def test_tui_model_filters_web_fields_and_navigates_columns(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)

        model = kanban_tui.BoardModel(single=True)
        model.set_board({
            "generated_at": "2026-08-20 22:30:00",
            "tasks": [
                {
                    "task_id": "20260820-first-task",
                    "title": "第一项",
                    "state": "todo",
                    "task_group": "20260820-terminal-group",
                    "type": "Feature",
                    "assignee": "Codex",
                },
                {
                    "task_id": "20260820-second-task",
                    "title": "Second item",
                    "state": "todo",
                    "task_group": "",
                    "type": "Bug",
                    "assignee": "",
                },
                {
                    "task_id": "20260820-old-task",
                    "title": "Old item",
                    "state": "archived",
                    "task_group": "",
                    "type": "Chore",
                    "assignee": "QA",
                },
            ],
        })

        self.assertTrue(model.single)
        self.assertEqual(kanban_tui.ACTIVE_STATES, model.states)
        model.column_index = model.states.index("todo")
        self.assertEqual("20260820-first-task", model.selected_task()["task_id"])
        model.move_task(1)
        self.assertEqual("20260820-second-task", model.selected_task()["task_id"])

        model.query = "terminal-group"
        model.normalize()
        self.assertEqual(["20260820-first-task"], [
            task["task_id"] for task in model.tasks_for("todo")
        ])
        model.query = "qa"
        model.normalize()
        self.assertEqual([], model.tasks_for("todo"))
        self.assertEqual(["20260820-old-task"], [
            task["task_id"] for task in model.tasks_for("archived")
        ])

        model.query = ""
        model.toggle_archived()
        self.assertEqual(kanban_tui.ALL_STATES, model.states)
        model.column_index = 0
        model.move_column(-1)
        self.assertEqual("trash", model.current_state)
        model.toggle_archived()
        self.assertEqual("done", model.current_state)

    def test_tui_mouse_selects_columns_tasks_and_scrolls_detail(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)
        import curses
        from unittest import mock

        class FakeScreen:
            def getmaxyx(self):
                return 24, 120

            def bkgd(self, _char, attr=0):
                pass

        board = {
            "generated_at": "2026-08-21 18:00:00",
            "tasks": [
                {
                    "task_id": "20260821-one-task",
                    "title": "一号",
                    "state": "backlog",
                    "type": "Feature",
                    "kind": "small",
                    "assignee": "",
                    "time": "-",
                },
                {
                    "task_id": "20260821-two-task",
                    "title": "二号",
                    "state": "backlog",
                    "type": "Feature",
                    "kind": "small",
                    "assignee": "",
                    "time": "-",
                },
                {
                    "task_id": "20260821-todo-task",
                    "title": "待办",
                    "state": "todo",
                    "type": "Feature",
                    "kind": "small",
                    "assignee": "",
                    "time": "-",
                },
            ],
        }
        detail_docs = {
            "20260821-one-task": {
                "task_id": "20260821-one-task",
                "title": "一号",
                "state": "backlog",
                "document": "\n".join(f"line{i}" for i in range(1, 40)),
            },
            "20260821-two-task": {
                "task_id": "20260821-two-task",
                "title": "二号",
                "state": "backlog",
                "document": "\n".join(f"line{i}" for i in range(1, 40)),
            },
        }
        tui = kanban_tui.KanbanTui(
            FakeScreen(),
            single=False,
            refresh_interval=60,
            context={},
            get_board=lambda: board,
            get_task=lambda task_id: detail_docs[task_id],
            theme="dark",
        )
        tui.mouse_enabled = True
        tui.model.set_board(board)
        self.assertEqual("backlog", tui.model.current_state)

        self.assertEqual(-1, kanban_tui.mouse_wheel_delta(curses.BUTTON4_PRESSED))
        self.assertEqual(1, kanban_tui.mouse_wheel_delta(curses.BUTTON5_PRESSED))
        self.assertTrue(kanban_tui.mouse_left_clicked(curses.BUTTON1_CLICKED))
        self.assertTrue(kanban_tui.mouse_left_double_clicked(curses.BUTTON1_DOUBLE_CLICKED))

        # 点 todo 栏标题切换栏目.
        layout = tui._visible_column_layout()
        self.assertGreaterEqual(len(layout), 2)
        todo_state, todo_x, _todo_w = layout[1]
        self.assertEqual("todo", todo_state)
        with mock.patch.object(
            kanban_tui.curses,
            "getmouse",
            return_value=(0, todo_x + 1, 2, 0, curses.BUTTON1_CLICKED),
        ):
            tui._handle_mouse()
        self.assertEqual("todo", tui.model.current_state)

        # 仅 PRESSED/RELEASED 时栏目标题仍可点击.
        tui.model.column_index = 0
        with mock.patch.object(
            kanban_tui.curses,
            "getmouse",
            side_effect=[
                (0, todo_x + 1, 2, 0, curses.BUTTON1_PRESSED),
                (0, todo_x + 1, 2, 0, curses.BUTTON1_RELEASED),
            ],
        ):
            tui._handle_mouse()
            tui._handle_mouse()
        self.assertEqual("todo", tui.model.current_state)

        # 点 backlog 第二张卡选中.
        backlog_state, backlog_x, _bw = layout[0]
        card_y = kanban_tui.BODY_TOP + kanban_tui.CARD_HEIGHT
        with mock.patch.object(
            kanban_tui.curses,
            "getmouse",
            return_value=(0, backlog_x + 2, card_y, 0, curses.BUTTON1_CLICKED),
        ):
            tui._handle_mouse()
        self.assertEqual("backlog", tui.model.current_state)
        self.assertEqual("20260821-two-task", tui.model.selected_ids["backlog"])

        # 双击打开详情.
        with mock.patch.object(
            kanban_tui.curses,
            "getmouse",
            return_value=(
                0,
                backlog_x + 2,
                card_y,
                0,
                curses.BUTTON1_DOUBLE_CLICKED,
            ),
        ):
            tui._handle_mouse()
        self.assertIsNotNone(tui.detail)
        self.assertEqual("20260821-two-task", tui.detail["task_id"])

        # 详情页滚轮下滚.
        before = tui.detail_scroll
        with mock.patch.object(
            kanban_tui.curses,
            "getmouse",
            return_value=(0, 1, 10, 0, curses.BUTTON5_PRESSED),
        ):
            tui._handle_mouse()
        self.assertEqual(before + kanban_tui.MOUSE_SCROLL_STEP, tui.detail_scroll)

        # 单击搜索行进入搜索.
        tui._close_detail()
        with mock.patch.object(
            kanban_tui.curses,
            "getmouse",
            return_value=(0, 2, 1, 0, curses.BUTTON1_CLICKED),
        ):
            tui._handle_mouse()
        self.assertTrue(tui.searching)

        # 单栏标题左右箭头切栏.
        tui.searching = False
        tui.model.single = True
        tui.model.column_index = 0
        with mock.patch.object(
            kanban_tui.curses,
            "getmouse",
            return_value=(0, 0, 2, 0, curses.BUTTON1_CLICKED),
        ):
            tui._handle_mouse()
        self.assertEqual("done", tui.model.current_state)

        # 鼠标不可用时忽略事件.
        tui.mouse_enabled = False
        tui.model.column_index = 0
        with mock.patch.object(
            kanban_tui.curses,
            "getmouse",
            return_value=(0, todo_x + 1, 2, 0, curses.BUTTON1_CLICKED),
        ):
            tui._handle_mouse()
        self.assertEqual("backlog", tui.model.current_state)

        model_focus = kanban_tui.BoardModel()
        model_focus.set_board(board)
        self.assertTrue(model_focus.focus_state("todo"))
        self.assertTrue(model_focus.select_task_index("backlog", 1))
        self.assertEqual("20260821-two-task", model_focus.selected_ids["backlog"])
        self.assertEqual("backlog", model_focus.current_state)

    def test_tui_fits_visible_columns_and_keeps_focus_on_screen(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)

        self.assertEqual(1, kanban_tui.visible_column_count(20, 4))
        self.assertEqual(1, kanban_tui.visible_column_count(39, 4))
        self.assertEqual(1, kanban_tui.visible_column_count(40, 4))
        self.assertEqual(1, kanban_tui.visible_column_count(80, 4))
        self.assertEqual(2, kanban_tui.visible_column_count(81, 4))
        self.assertEqual(3, kanban_tui.visible_column_count(122, 4))
        self.assertEqual(4, kanban_tui.visible_column_count(163, 4))
        self.assertEqual(1, kanban_tui.visible_column_count(160, 4, single=True))
        for width, count in ((81, 2), (122, 3), (163, 4)):
            layout = kanban_tui.column_geometry(width, count)
            self.assertEqual(count, len(layout))
            self.assertTrue(all(column_width >= 40 for _x, column_width, _sep in layout))
            last_x, last_width, last_sep = layout[-1]
            self.assertFalse(last_sep)
            self.assertEqual(width, last_x + last_width)

        model = kanban_tui.BoardModel()
        self.assertEqual(("backlog", "todo", "working"), model.visible_states(3))
        model.move_column(1)
        self.assertEqual("todo", model.current_state)
        self.assertEqual(("backlog", "todo", "working"), model.visible_states(3))
        model.move_column(2)
        self.assertEqual("review", model.current_state)
        self.assertEqual(("todo", "working", "review"), model.visible_states(3))
        self.assertIn(model.current_state, model.visible_states(3))
        model.move_column(1)
        self.assertEqual("done", model.current_state)
        self.assertEqual(("working", "review", "done"), model.visible_states(3))
        model.move_column(1)
        self.assertEqual("backlog", model.current_state)
        self.assertEqual(("backlog", "todo", "working"), model.visible_states(3))
        model.move_column(-1)
        self.assertEqual("done", model.current_state)
        self.assertEqual(("working", "review", "done"), model.visible_states(3))

        model.toggle_archived()
        model.column_index = model.states.index("trash")
        self.assertEqual(("done", "archived", "trash"), model.visible_states(3))
        model.toggle_archived()
        self.assertEqual("done", model.current_state)
        self.assertEqual(("working", "review", "done"), model.visible_states(3))

        class FakeScreen:
            def __init__(self, width: int) -> None:
                self.width = width
                self.writes = []

            def getmaxyx(self):
                return 24, self.width

            def addstr(self, y, x, text, attr=0):
                self.writes.append((y, x, text))

            def move(self, y, x):
                pass

        context = {
            "state_labels": {state: state for state in STATES},
            "size_labels": {"small": "small", "large": "large"},
            "empty": "No tasks",
            "too_small": "Terminal is too small.",
        }
        board = {
            "generated_at": "2026-08-21 00:00:00",
            "tasks": [
                {
                    "task_id": f"20260821-{state}-task",
                    "title": state,
                    "state": state,
                }
                for state in ("backlog", "todo", "working", "done")
            ],
        }

        narrow = FakeScreen(122)
        tui = kanban_tui.KanbanTui(
            narrow,
            single=False,
            refresh_interval=30,
            context=context,
            get_board=lambda: board,
            get_task=lambda _task_id: {},
        )
        tui.model.set_board(board)
        tui._render_board()
        headings = " ".join(text for y, _x, text in narrow.writes if y == 2)
        self.assertIn("backlog", headings)
        self.assertIn("todo", headings)
        self.assertIn("working", headings)
        self.assertNotIn("done", headings)
        self.assertEqual(("backlog", "todo", "working"), tui.model.visible_states(3))
        self.assertNotIn("too narrow", headings.lower())
        self.assertNotIn("use --single", " ".join(text for _y, _x, text in narrow.writes))

        narrow.writes.clear()
        tui.model.move_column(3)
        tui._render_board()
        focused_headings = " ".join(text for y, _x, text in narrow.writes if y == 2)
        self.assertIn("review", focused_headings)
        self.assertNotIn("backlog", focused_headings)
        self.assertIn(tui.model.current_state, tui.model.visible_states(3))
        self.assertEqual(("todo", "working", "review"), tui.model.visible_states(3))

        one_column = FakeScreen(20)
        tui = kanban_tui.KanbanTui(
            one_column,
            single=False,
            refresh_interval=30,
            context=context,
            get_board=lambda: board,
            get_task=lambda _task_id: {},
        )
        tui.model.set_board(board)
        tui._render_board()
        self.assertFalse(
            any("too small" in text.lower() for _y, _x, text in one_column.writes)
        )
        single_headings = [text for y, _x, text in one_column.writes if y == 2]
        self.assertEqual(1, len(single_headings))
        self.assertIn("backlog", single_headings[0])
        self.assertEqual([(0, 20, False)], kanban_tui.column_geometry(20, 1))

        wide = FakeScreen(80)
        tui = kanban_tui.KanbanTui(
            wide,
            single=True,
            refresh_interval=30,
            context=context,
            get_board=lambda: board,
            get_task=lambda _task_id: {},
        )
        tui.model.set_board(board)
        tui.model.move_column(2)
        tui._render_board()
        forced = [text for y, _x, text in wide.writes if y == 2]
        self.assertEqual(1, len(forced))
        self.assertIn("working", forced[0])
        self.assertNotIn("todo", forced[0])

    def test_tui_column_width_keys_adjust_and_persist(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)

        config_dir = self.home / ".config" / "onevoke"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "config.json"
        prefs = config_dir / "tui.json"
        self.env["ONEVOKE_CONFIG"] = str(config_path)
        self.assertFalse(prefs.exists())
        with mock.patch.dict(os.environ, {"ONEVOKE_CONFIG": str(config_path)}):
            self.assertEqual(40, kanban_tui.load_column_width())
            self.assertEqual(2, kanban_tui.visible_column_count(100, 4, column_width=40))
            self.assertEqual(3, kanban_tui.visible_column_count(100, 4, column_width=30))
            self.assertEqual(4, kanban_tui.visible_column_count(100, 4, column_width=20))
            self.assertEqual(1, kanban_tui.visible_column_count(80, 4, column_width=40))

            kanban_tui.save_column_width(35)
            self.assertEqual(35, kanban_tui.load_column_width())
            self.assertTrue(prefs.is_file())
            self.assertEqual(0o600, prefs.stat().st_mode & 0o777)
            prefs.write_text('{"column_width": 999}\n', encoding="utf-8")
            self.assertEqual(40, kanban_tui.load_column_width())
            prefs.write_text('{"column_width": 0}\n', encoding="utf-8")
            self.assertEqual(40, kanban_tui.load_column_width())
            prefs.write_text('{"column_width": "wide"}\n', encoding="utf-8")
            self.assertEqual(40, kanban_tui.load_column_width())
            prefs.write_text(
                '{"column_width": ' + ("9" * 5000) + "}\n",
                encoding="utf-8",
            )
            self.assertEqual(40, kanban_tui.load_column_width())

        class FakeScreen:
            def getmaxyx(self):
                return 24, 120

        saved = []
        tui = kanban_tui.KanbanTui(
            FakeScreen(),
            single=False,
            refresh_interval=30,
            context={},
            get_board=lambda: {"tasks": []},
            get_task=lambda _task_id: {},
            column_width=40,
            persist_column_width=saved.append,
        )
        tui._handle_board_key("-")
        self.assertEqual(35, tui.column_width)
        self.assertEqual([35], saved)
        tui._handle_board_key("=")
        self.assertEqual(40, tui.column_width)
        self.assertEqual([35, 40], saved)
        tui._handle_board_key("+")
        self.assertEqual(45, tui.column_width)
        tui._handle_board_key("_")
        self.assertEqual(40, tui.column_width)

        tui.column_width = kanban_tui.MIN_COLUMN_WIDTH
        tui._handle_board_key("-")
        self.assertEqual(kanban_tui.MIN_COLUMN_WIDTH, tui.column_width)
        tui.column_width = kanban_tui.MAX_COLUMN_WIDTH
        before = len(saved)
        tui._handle_board_key("=")
        self.assertEqual(kanban_tui.MAX_COLUMN_WIDTH, tui.column_width)
        self.assertEqual(before, len(saved))

        def fail_persist(_width: int) -> None:
            raise OSError("disk full")

        failing = kanban_tui.KanbanTui(
            FakeScreen(),
            single=False,
            refresh_interval=30,
            context={},
            get_board=lambda: {"tasks": []},
            get_task=lambda _task_id: {},
            column_width=40,
            persist_column_width=fail_persist,
        )
        failing._handle_board_key("-")
        self.assertEqual(35, failing.column_width)
        self.assertIn("disk full", failing.prefs_error)
        self.assertIn("disk full", failing._status_error())
        failing.model.refresh_error = "board unavailable"
        status = failing._status_error()
        self.assertIn("disk full", status)
        self.assertIn("board unavailable", status)
        self.assertLess(status.index("board unavailable"), status.index("disk full"))

        class FooterScreen:
            def __init__(self, width: int) -> None:
                self.width = width
                self.writes = []

            def getmaxyx(self):
                return 24, self.width

            def addstr(self, y, x, text, attr=0):
                self.writes.append((y, x, text, attr))

            def move(self, y, x):
                pass

        narrow = FooterScreen(20)
        visible = kanban_tui.KanbanTui(
            narrow,
            single=False,
            refresh_interval=30,
            context={
                "title": "Task Board",
                "search": "Search",
                "theme": "Theme",
                "theme_labels": {"auto": "auto"},
                "width": "Width",
                "active": "active",
                "updated": "Updated",
                "state_labels": {state: state for state in STATES},
                "size_labels": {"small": "small", "large": "large"},
                "empty": "No tasks",
                "error": "Error",
            },
            get_board=lambda: {"tasks": []},
            get_task=lambda _task_id: {},
            column_width=40,
        )
        visible.model.set_board({"generated_at": "-", "tasks": []})
        visible._render_board()
        toolbar = " ".join(text for y, _x, text, _attr in narrow.writes if y == 1)
        self.assertIn("40", toolbar)

        wide_value = FooterScreen(20)
        visible.screen = wide_value
        visible.column_width = 120
        wide_value.writes.clear()
        visible._render_board()
        wide_toolbar = " ".join(text for y, _x, text, _attr in wide_value.writes if y == 1)
        self.assertIn("120", wide_toolbar)

        chinese = FooterScreen(20)
        visible.screen = chinese
        visible.column_width = 40
        visible.context = {
            **visible.context,
            "search": "搜索",
            "width": "栏宽",
            "theme": "主题",
            "theme_labels": {"auto": "自动"},
            "active": "活跃栏目",
            "updated": "更新于",
        }
        chinese.writes.clear()
        visible._render_board()
        chinese_toolbar = " ".join(text for y, _x, text, _attr in chinese.writes if y == 1)
        self.assertIn("40", chinese_toolbar)

        footer_screen = FooterScreen(80)
        both = kanban_tui.KanbanTui(
            footer_screen,
            single=False,
            refresh_interval=30,
            context={
                "error": "Error",
                "state_labels": {state: state for state in STATES},
                "size_labels": {"small": "small", "large": "large"},
            },
            get_board=lambda: {"tasks": []},
            get_task=lambda _task_id: {},
            column_width=40,
        )
        both.prefs_error = (
            f"[Errno 13] Permission denied: '{self.home / '.config' / 'onevoke' / 'tui.json'}'"
        )
        both.model.refresh_error = "board unavailable"
        both._render_footer(24, 80)
        footer = next(text for y, _x, text, _attr in footer_screen.writes if y == 23)
        self.assertIn("board unavailable", footer)
        self.assertTrue(footer.index("board unavailable") < footer.index("Permission"))

    def test_tui_page_keys_move_selection_by_page_and_clamp(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)
        import curses

        class FakeScreen:
            def getmaxyx(self):
                return 24, 100

        tui = kanban_tui.KanbanTui(
            FakeScreen(),
            single=True,
            refresh_interval=60,
            context={},
            get_board=lambda: {"tasks": []},
            get_task=lambda _task_id: {},
        )
        tui.model.set_board({
            "tasks": [
                {"task_id": f"20260820-page-{index:02d}-task", "state": "todo"}
                for index in range(10)
            ],
        })
        tui.model.column_index = tui.model.states.index("todo")
        page = tui._page_size()
        self.assertEqual((24 - 4) // kanban_tui.CARD_HEIGHT, page)

        tui._handle_board_key(curses.KEY_NPAGE)
        self.assertEqual(
            f"20260820-page-{page:02d}-task", tui.model.selected_task()["task_id"]
        )
        self.assertEqual(page, tui.model.scrolls["todo"])
        for _ in range(5):
            tui._handle_board_key(curses.KEY_NPAGE)
        self.assertEqual(
            "20260820-page-09-task", tui.model.selected_task()["task_id"]
        )
        self.assertEqual(10 - page, tui.model.scrolls["todo"])
        tui._handle_board_key(curses.KEY_PPAGE)
        self.assertEqual(
            f"20260820-page-{9 - page:02d}-task",
            tui.model.selected_task()["task_id"],
        )
        self.assertEqual(10 - 2 * page, tui.model.scrolls["todo"])
        for _ in range(5):
            tui._handle_board_key(curses.KEY_PPAGE)
        self.assertEqual(
            "20260820-page-00-task", tui.model.selected_task()["task_id"]
        )
        self.assertEqual(0, tui.model.scrolls["todo"])

    def test_tui_detail_vim_paging_and_document_search(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)

        class FakeScreen:
            def __init__(self, height: int, width: int) -> None:
                self.height = height
                self.width = width
                self.writes = []

            def getmaxyx(self):
                return self.height, self.width

            def addstr(self, y, x, text, attr=0):
                self.writes.append((y, x, text, attr))

            def move(self, y, x):
                self.cursor = (y, x)

        document = "\n".join(
            f"line-{index:02d} needle" if index in {3, 12, 20} else f"line-{index:02d}"
            for index in range(30)
        )
        screen = FakeScreen(14, 40)
        tui = kanban_tui.KanbanTui(
            screen,
            single=True,
            refresh_interval=60,
            context={
                "search": "Search",
                "search_help": "Enter apply | Esc clear",
                "detail_help": "detail keys",
                "no_match": "no match",
                "state_labels": {"todo": "todo"},
                "size_labels": {"small": "small"},
            },
            get_board=lambda: {"tasks": []},
            get_task=lambda _task_id: {},
        )
        tui.detail = {
            "task_id": "20260821-detail-search-task",
            "title": "Detail search",
            "state": "todo",
            "kind": "small",
            "document": document,
        }
        body = tui._detail_body_height()
        self.assertEqual(10, body)
        half = max(1, body // 2)

        tui._handle_detail_key("\x04")  # Ctrl-d
        self.assertEqual(half, tui.detail_scroll)
        tui._handle_detail_key("\x06")  # Ctrl-f
        self.assertEqual(half + body, tui.detail_scroll)
        tui._handle_detail_key("\x15")  # Ctrl-u
        self.assertEqual(half + body - half, tui.detail_scroll)
        tui._handle_detail_key("\x02")  # Ctrl-b
        self.assertEqual(max(0, half + body - half - body), tui.detail_scroll)
        tui._handle_detail_key("G")
        tui._render_detail()
        self.assertEqual(max(0, 30 - body), tui.detail_scroll)
        tui._handle_detail_key("g")
        tui._handle_detail_key("g")
        self.assertEqual(0, tui.detail_scroll)

        tui._handle_detail_key("/")
        self.assertTrue(tui.detail_searching)
        for char in "needle":
            tui._handle_detail_key(char)
        self.assertEqual("needle", tui.detail_query)
        tui._handle_detail_key("\n")
        self.assertFalse(tui.detail_searching)
        self.assertEqual([3, 12, 20], tui._detail_matches())
        self.assertEqual(0, tui.detail_match_index)
        self.assertEqual(max(0, 3 - body // 3), tui.detail_scroll)

        tui._handle_detail_key("n")
        self.assertEqual(1, tui.detail_match_index)
        self.assertEqual(max(0, 12 - body // 3), tui.detail_scroll)
        tui._handle_detail_key("N")
        self.assertEqual(0, tui.detail_match_index)

        tui._handle_detail_key("/")
        tui._handle_detail_key("\x1b")
        self.assertEqual("", tui.detail_query)
        self.assertFalse(tui.detail_searching)

        tui.detail_query = "missing"
        tui._apply_detail_search()
        screen.writes = []
        tui._render_detail()
        footer = next(write for write in screen.writes if write[0] == 13)
        self.assertIn("no match", footer[2])

        self.assertEqual([0, 2], kanban_tui.line_match_indexes(["Alpha", "beta", "ALPHA"], "alpha"))
        self.assertEqual([(2, 5)], kanban_tui.match_spans("xxABCyy", "abc"))

    def test_copy_to_clipboard_helpers(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)

        self.assertEqual((False, ""), kanban_tui.copy_to_clipboard(""))

        with mock.patch("kanban_tui.shutil.which", return_value=None), mock.patch(
            "kanban_tui.copy_via_osc52",
            return_value=(False, "no tty"),
        ):
            self.assertEqual(
                (False, "no tty"),
                kanban_tui.copy_to_clipboard("hello"),
            )

        with mock.patch("kanban_tui.shutil.which", return_value="/usr/bin/xclip"), mock.patch(
            "kanban_tui.subprocess.run",
            return_value=mock.Mock(returncode=1, stderr=b"cannot open display"),
        ), mock.patch(
            "kanban_tui.copy_via_osc52",
            return_value=(True, ""),
        ) as osc52:
            self.assertEqual((True, ""), kanban_tui.copy_to_clipboard("hello"))
            osc52.assert_called_once_with("hello")

        captured: list[str] = []

        class FakeTty:
            def write(self, data: str) -> None:
                captured.append(data)

            def flush(self) -> None:
                return None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with mock.patch("builtins.open", return_value=FakeTty()):
            success, error = kanban_tui.copy_via_osc52("复制")
        self.assertTrue(success)
        self.assertEqual("", error)
        self.assertEqual(1, len(captured))
        self.assertIn("52;c;", captured[0])
        self.assertIn(
            base64.b64encode("复制".encode("utf-8")).decode("ascii"),
            captured[0],
        )

    def test_tui_copy_and_selection_helpers(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)
        import curses

        self.assertEqual("lpha\nbe", kanban_tui.extract_char_selection(
            ["alpha", "beta"],
            (0, 1),
            (1, 2),
        ))
        self.assertEqual("beta", kanban_tui.extract_line_selection(
            ["alpha", "beta", "gamma"],
            (1, 0),
            (1, 3),
        ))
        self.assertEqual(1, kanban_tui.display_column_to_char_index("a中b", 2))
        self.assertEqual(3, kanban_tui.char_index_to_display_column("a中b", 2))
        self.assertEqual(4, kanban_tui.display_column_to_caret_index("复制测试", 7))
        self.assertEqual(
            "hello",
            kanban_tui.extract_mouse_char_selection(["hello"], (0, 4), (0, 0)),
        )
        self.assertEqual(
            "",
            kanban_tui.extract_mouse_char_selection(["hello"], (0, 2), (0, 2)),
        )
        self.assertTrue(kanban_tui.mouse_left_pressed(curses.BUTTON1_PRESSED))
        self.assertFalse(kanban_tui.mouse_left_clicked(curses.BUTTON1_PRESSED))
        self.assertTrue(kanban_tui.mouse_left_clicked(curses.BUTTON1_CLICKED))

        class FakeScreen:
            def getmaxyx(self):
                return 24, 100

        copied: list[str] = []

        def fake_copy(text: str) -> tuple[bool, str]:
            copied.append(text)
            return True, ""

        board = {
            "generated_at": "2026-08-22 12:00:00",
            "tasks": [
                {
                    "task_id": "20260822-copy-task",
                    "title": "复制测试",
                    "state": "todo",
                    "type": "Feature",
                    "kind": "small",
                    "assignee": "",
                    "time": "-",
                },
            ],
        }
        tui = kanban_tui.KanbanTui(
            FakeScreen(),
            single=True,
            refresh_interval=60,
            context={"copied": "Copied"},
            get_board=lambda: board,
            get_task=lambda task_id: {
                "task_id": task_id,
                "title": "复制测试",
                "state": "todo",
                "document": "line one\nline two\nline three\n",
            },
            copy_to_clipboard_fn=fake_copy,
        )
        tui.model.set_board(board)
        tui.model.column_index = tui.model.states.index("todo")
        tui._copy_selected_task_id()
        self.assertEqual(["20260822-copy-task"], copied)

        tui.detail = tui.get_task("20260822-copy-task")
        tui.detail_cursor = (0, 0)
        tui._handle_detail_key("j")
        self.assertEqual((1, 0), tui.detail_cursor)
        tui._detail_toggle_select("char")
        for _ in range(len("line two")):
            tui._handle_detail_key("l")
        tui._detail_yank()
        self.assertEqual(["20260822-copy-task", "line two"], copied)
        self.assertIsNone(tui.detail_select_mode)

        tui.detail_cursor = (0, 0)
        tui._detail_toggle_select("char")
        tui._detail_move_cursor(0, 4)
        tui._detail_yank()
        self.assertEqual(["20260822-copy-task", "line two", "line"], copied)
        self.assertIsNone(tui.detail_select_mode)

        tui.detail_cursor = (1, 0)
        tui._detail_toggle_select("line")
        self.assertEqual("line", tui.detail_select_mode)
        tui._detail_yank()
        self.assertEqual(["20260822-copy-task", "line two", "line", "line two"], copied)

        tui.mouse_select_anchor = ("board", "20260822-copy-task", 0, 0, 20)
        tui.mouse_select_cursor = ("board", "20260822-copy-task", 0, 3, 20)
        text = tui._extract_board_mouse_selection()
        self.assertEqual("复制测试", text)

        layout = tui._visible_column_layout()
        todo_x = next(x for state, x, _w in layout if state == "todo")
        card_y = kanban_tui.BODY_TOP
        tui._handle_board_mouse(todo_x + 1, card_y, curses.BUTTON1_PRESSED)
        tui._handle_board_mouse(todo_x + 8, card_y, curses.BUTTON1_PRESSED)
        tui._handle_board_mouse(todo_x + 8, card_y, curses.BUTTON1_RELEASED)
        self.assertIn("复制测试", copied)
        copied_len = len(copied)
        tui._handle_board_mouse(todo_x + 5, card_y, curses.BUTTON1_PRESSED)
        tui._handle_board_mouse(todo_x + 5, card_y, curses.BUTTON1_PRESSED)
        tui._handle_board_mouse(todo_x + 5, card_y, curses.BUTTON1_RELEASED)
        self.assertEqual(copied_len, len(copied))

        tui.detail = {
            "task_id": "20260822-copy-task",
            "title": "复制测试",
            "state": "todo",
            "document": "\n".join(f"line-{index}" for index in range(6)),
        }
        tui.detail_scroll = 0
        tui.detail_cursor = (4, 0)
        tui._detail_toggle_select("line")
        for _ in range(3):
            tui._handle_detail_key("k")
        tui._detail_yank()
        self.assertEqual("line-1\nline-2\nline-3\nline-4", copied[-1])

        tui.detail = tui.get_task("20260822-copy-task")
        tui._handle_detail_key("/")
        for char in "two":
            tui._handle_detail_key(char)
        tui._handle_detail_key("\n")
        self.assertEqual((1, 0), tui.detail_cursor)
        before_scroll = tui.detail_scroll
        tui._handle_detail_key("j")
        self.assertEqual((2, 0), tui.detail_cursor)
        self.assertEqual(before_scroll, tui.detail_scroll)

        fail_tui = kanban_tui.KanbanTui(
            FakeScreen(),
            single=True,
            refresh_interval=60,
            context={
                "copy_failed": "复制失败",
                "clipboard_unavailable": "无可用剪贴板工具",
            },
            get_board=lambda: board,
            get_task=lambda task_id: {"task_id": task_id, "document": ""},
            copy_to_clipboard_fn=lambda _text: (False, ""),
        )
        fail_tui._copy_text("x")
        self.assertIn("无可用剪贴板工具", fail_tui.copy_notice)

        tui.suppress_click = True
        tui._reset_mouse_selection()
        self.assertFalse(tui.suppress_click)

        tui.detail = {
            "task_id": "20260822-copy-task",
            "title": "复制测试",
            "state": "todo",
            "document": "hello world",
        }
        tui.mouse_select_anchor = ("detail", 0, 0)
        tui.mouse_select_cursor = ("detail", 0, 4)
        self.assertEqual("hello", tui._extract_detail_mouse_selection())
        tui.mouse_select_anchor = ("detail", 0, 4)
        tui.mouse_select_cursor = ("detail", 0, 0)
        self.assertEqual("hello", tui._extract_detail_mouse_selection())

        tui.searching = True
        tui._handle_search_mouse(5, 3, curses.BUTTON1_PRESSED)
        self.assertTrue(tui.searching)
        tui._handle_search_mouse(5, 3, curses.BUTTON1_RELEASED)
        self.assertFalse(tui.searching)

        tui.detail = {"task_id": "x", "document": "only"}
        tui.detail_cursor = (5, 0)
        tui.detail_scroll = 0
        tui._scroll_detail_by(3)
        self.assertEqual((0, 0), tui.detail_cursor)

    def test_tui_theme_key_cycles_and_run_rejects_unknown_theme(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)

        class FakeScreen:
            def getmaxyx(self):
                return 24, 100

        tui = kanban_tui.KanbanTui(
            FakeScreen(),
            single=True,
            refresh_interval=60,
            context={},
            get_board=lambda: {"tasks": []},
            get_task=lambda _task_id: {},
        )
        self.assertEqual("auto", tui.theme)
        seen = []
        for _ in kanban_tui.THEMES:
            tui._handle_board_key("t")
            seen.append(tui.theme)
        self.assertEqual(["light", "dark", "auto"], seen)

        with self.assertRaises(kanban_tui.KanbanTuiError) as raised:
            kanban_tui.run(
                single=False,
                refresh_interval=60,
                context={},
                get_board=lambda: {"tasks": []},
                get_task=lambda _task_id: {},
                theme="sepia",
            )
        self.assertIn("sepia", str(raised.exception))

    def test_tui_apply_theme_sets_palette_background_and_auto_fallback(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)
        import curses
        from unittest import mock

        class FakeScreen:
            def __init__(self):
                self.background = None

            def getmaxyx(self):
                return 24, 100

            def bkgd(self, _char, attr=0):
                self.background = attr

        pairs = {}
        screen = FakeScreen()
        tui = kanban_tui.KanbanTui(
            screen,
            single=True,
            refresh_interval=60,
            context={},
            get_board=lambda: {"tasks": []},
            get_task=lambda _task_id: {},
            theme="light",
        )
        with mock.patch.object(kanban_tui.curses, "has_colors", return_value=True), \
                mock.patch.object(kanban_tui.curses, "use_default_colors"), \
                mock.patch.object(
                    kanban_tui.curses,
                    "init_pair",
                    side_effect=lambda index, fg, bg: pairs.__setitem__(
                        index, (fg, bg)
                    ),
                ), \
                mock.patch.object(
                    kanban_tui.curses,
                    "color_pair",
                    side_effect=lambda index: index << 8,
                ), \
                mock.patch.dict(kanban_tui.os.environ, {"COLORFGBG": ""}):
            tui._init_style()
            light = kanban_tui.THEME_PALETTES["light"]
            self.assertTrue(
                all(bg == curses.COLOR_WHITE for _fg, bg in pairs.values())
            )
            self.assertEqual(light["backlog"], pairs[2][0])
            self.assertEqual(light["working"], pairs[4][0])
            self.assertEqual(tui.colors["text"], screen.background)
            self.assertNotEqual(0, tui.colors["error"])
            footer = tui._footer_attr()
            self.assertEqual(
                tui.colors["accent"] | curses.A_REVERSE, footer
            )
            self.assertEqual({}, tui.highlight_colors)

            pairs.clear()
            tui._handle_board_key("t")
            self.assertEqual("dark", tui.theme)
            self.assertTrue(
                all(bg == curses.COLOR_BLACK for _fg, bg in pairs.values())
            )
            self.assertEqual(
                kanban_tui.THEME_PALETTES["dark"]["working"], pairs[4][0]
            )
            self.assertEqual(tui.colors["text"], screen.background)

            pairs.clear()
            tui._handle_board_key("t")
            self.assertEqual("auto", tui.theme)
            base_pairs = {
                index: value
                for index, value in pairs.items()
                if index <= len(kanban_tui.COLOR_NAMES)
            }
            highlight_pairs = {
                index: value
                for index, value in pairs.items()
                if len(kanban_tui.COLOR_NAMES)
                < index
                <= 2 * len(kanban_tui.COLOR_NAMES)
            }
            self.assertTrue(all(bg == -1 for _fg, bg in base_pairs.values()))
            self.assertTrue(
                all(bg == curses.COLOR_BLACK for _fg, bg in highlight_pairs.values())
            )
            # 8 色 auto 深底: 分隔线弱化色用亮黑, 背景沿用终端默认.
            muted_index = 1 + 2 * len(kanban_tui.COLOR_NAMES)
            self.assertEqual((curses.COLOR_BLACK, -1), pairs[muted_index])
            self.assertEqual(
                (muted_index << 8) | curses.A_BOLD, tui.colors["muted"]
            )
            # 背景未知时仅正文回退到终端默认前景色; backlog 保持色相.
            self.assertEqual(-1, pairs[1][0])
            self.assertEqual(
                kanban_tui.THEME_PALETTES["dark"]["backlog"], pairs[2][0]
            )
            self.assertEqual(
                kanban_tui.THEME_PALETTES["dark"]["backlog"],
                pairs[2 + len(kanban_tui.COLOR_NAMES)][0],
            )
            self.assertEqual(
                curses.COLOR_WHITE, pairs[1 + len(kanban_tui.COLOR_NAMES)][0]
            )
            self.assertEqual(0, screen.background)
            self.assertEqual(
                tui.highlight_colors["backlog"] | curses.A_REVERSE | curses.A_BOLD,
                tui._highlight_attr("backlog", curses.A_BOLD),
            )

    def test_tui_auto_theme_light_terminal_uses_readable_selection(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)
        import curses
        from unittest import mock

        class FakeScreen:
            def getmaxyx(self):
                return 24, 100

            def bkgd(self, _char, attr=0):
                pass

        pairs = {}
        tui = kanban_tui.KanbanTui(
            FakeScreen(),
            single=True,
            refresh_interval=60,
            context={},
            get_board=lambda: {"tasks": []},
            get_task=lambda _task_id: {},
            theme="auto",
        )
        with mock.patch.object(kanban_tui.curses, "has_colors", return_value=True), \
                mock.patch.object(kanban_tui.curses, "use_default_colors"), \
                mock.patch.object(
                    kanban_tui.curses,
                    "init_pair",
                    side_effect=lambda index, fg, bg: pairs.__setitem__(
                        index, (fg, bg)
                    ),
                ), \
                mock.patch.object(
                    kanban_tui.curses,
                    "color_pair",
                    side_effect=lambda index: index << 8,
                ), \
                mock.patch.dict(kanban_tui.os.environ, {"COLORFGBG": "0;15"}):
            tui._init_style()
            light = kanban_tui.THEME_PALETTES["light"]
            self.assertEqual(light["backlog"], pairs[2][0])
            self.assertEqual(-1, pairs[2][1])
            highlight_backlog = pairs[2 + len(kanban_tui.COLOR_NAMES)]
            self.assertEqual(light["backlog"], highlight_backlog[0])
            self.assertEqual(curses.COLOR_WHITE, highlight_backlog[1])
            self.assertEqual(
                tui.highlight_colors["backlog"] | curses.A_REVERSE,
                tui._highlight_attr("backlog"),
            )

    def test_tui_theme_survives_terminal_with_few_color_pairs(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)
        import curses
        from unittest import mock

        class FakeScreen:
            def getmaxyx(self):
                return 24, 100

            def bkgd(self, _char, attr=0):
                pass

        def limited_init_pair(index, _fg, _bg):
            # 模拟 COLOR_PAIRS=8 的终端: 合法颜色对只有 1..7.
            if index > 7:
                raise ValueError(
                    "Color pair is greater than COLOR_PAIRS-1 (7)."
                )

        tui = kanban_tui.KanbanTui(
            FakeScreen(),
            single=True,
            refresh_interval=60,
            context={},
            get_board=lambda: {"tasks": []},
            get_task=lambda _task_id: {},
            theme="dark",
        )
        with mock.patch.object(kanban_tui.curses, "has_colors", return_value=True), \
                mock.patch.object(kanban_tui.curses, "use_default_colors"), \
                mock.patch.object(
                    kanban_tui.curses, "init_pair", side_effect=limited_init_pair
                ), \
                mock.patch.object(
                    kanban_tui.curses,
                    "color_pair",
                    side_effect=lambda index: index << 8,
                ):
            tui._init_style()
        # 第 8 对 (accent) 创建失败, 已建的状态色保留, accent 回退到属性 0.
        self.assertNotIn("accent", tui.colors)
        self.assertNotEqual(0, tui.colors["trash"])
        self.assertEqual(curses.A_REVERSE, tui._footer_attr())

    def test_tui_explicit_theme_works_without_default_colors(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)
        import curses
        from unittest import mock

        class FakeScreen:
            def __init__(self):
                self.background = None

            def getmaxyx(self):
                return 24, 100

            def bkgd(self, _char, attr=0):
                self.background = attr

        pairs = {}
        screen = FakeScreen()
        tui = kanban_tui.KanbanTui(
            screen,
            single=True,
            refresh_interval=60,
            context={},
            get_board=lambda: {"tasks": []},
            get_task=lambda _task_id: {},
            theme="dark",
        )
        with mock.patch.object(kanban_tui.curses, "has_colors", return_value=True), \
                mock.patch.object(
                    kanban_tui.curses,
                    "use_default_colors",
                    side_effect=curses.error("default colors unsupported"),
                ), \
                mock.patch.object(
                    kanban_tui.curses,
                    "init_pair",
                    side_effect=lambda index, fg, bg: pairs.__setitem__(
                        index, (fg, bg)
                    ),
                ), \
                mock.patch.object(
                    kanban_tui.curses,
                    "color_pair",
                    side_effect=lambda index: index << 8,
                ):
            tui._init_style()
            # 显式主题不依赖默认色扩展, 固定背景色照常初始化.
            self.assertFalse(tui.has_default_colors)
            self.assertNotEqual(0, tui.colors["working"])
            self.assertTrue(
                all(bg == curses.COLOR_BLACK for _fg, bg in pairs.values())
            )
            self.assertEqual(tui.colors["text"], screen.background)

            # auto 需要默认色扩展, 不可用时回退纯属性渲染并清除窗口背景.
            pairs.clear()
            tui.theme = "auto"
            tui._apply_theme()
            self.assertEqual({}, tui.colors)
            self.assertEqual({}, pairs)
            self.assertEqual(0, screen.background)

    def test_tui_board_height_boundary_keeps_footer_off_cards(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)

        class FakeScreen:
            def __init__(self, height, width):
                self.height = height
                self.width = width
                self.writes = []

            def getmaxyx(self):
                return self.height, self.width

            def addstr(self, y, x, text, attr=0):
                self.writes.append((y, x, text))

            def move(self, y, x):
                pass

        context = {
            "too_small": "Terminal is too small.",
            "state_labels": {state: state for state in STATES},
            "size_labels": {"small": "small", "large": "large"},
        }
        board = {
            "tasks": [
                {
                    "task_id": "20260820-boundary-task",
                    "title": "Boundary",
                    "state": "backlog",
                    "type": "chore",
                    "kind": "small",
                    "assignee": "codex",
                    "time": "08-20 23:00",
                }
            ],
        }

        short = FakeScreen(7, 40)
        tui = kanban_tui.KanbanTui(
            short,
            single=True,
            refresh_interval=60,
            context=context,
            get_board=lambda: board,
            get_task=lambda _task_id: {},
        )
        tui.model.set_board(board)
        tui._render_board()
        self.assertTrue(
            any("Terminal is too small." in text for _y, _x, text in short.writes)
        )

        tall = FakeScreen(8, 40)
        tui = kanban_tui.KanbanTui(
            tall,
            single=True,
            refresh_interval=60,
            context=context,
            get_board=lambda: board,
            get_task=lambda _task_id: {},
        )
        tui.model.set_board(board)
        tui._render_board()
        card_rows = [y for y, _x, text in tall.writes if "codex" in text]
        footer_rows = [y for y, _x, text in tall.writes if y == 7]
        # 8 行时卡片末行 (元信息) 在第 6 行, 页脚独占第 7 行.
        self.assertEqual([6], card_rows)
        self.assertTrue(footer_rows)
        self.assertFalse(any(y == 7 for y in card_rows))

    def test_tui_text_helpers_handle_wide_characters(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)

        self.assertEqual(4, kanban_tui.display_width("任务"))
        self.assertEqual("任...", kanban_tui.clip_text("任务标题", 5))
        self.assertEqual("08-21 18:00", kanban_tui.compact_time("2026-08-21 18:00"))
        self.assertEqual("-", kanban_tui.compact_time("-"))
        self.assertEqual("08-21 18:00", kanban_tui.compact_time("08-21 18:00"))
        self.assertEqual(
            "terminal-group", kanban_tui.compact_group("20260820-terminal-group")
        )
        self.assertEqual("custom-group", kanban_tui.compact_group("custom-group"))
        self.assertEqual("A B C", kanban_tui.clip_text("A\rB\x1bC", 10))
        self.assertEqual(["任务", "标题"], kanban_tui.wrap_text("任务标题", 4))
        task = {
            "title": "Alpha",
            "task_id": "id",
            "task_group": "group-one",
            "type": "Feature",
            "assignee": "Codex",
            "state": "todo",
        }
        for keyword in ("alpha", "ID", "group-one", "feature", "codex", "todo"):
            self.assertTrue(kanban_tui.task_matches(task, keyword))
        self.assertFalse(kanban_tui.task_matches(task, "missing"))

    def test_tui_narrow_toolbar_and_detail_keep_errors_visible(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)

        class FakeScreen:
            def __init__(self, height: int, width: int) -> None:
                self.height = height
                self.width = width
                self.writes = []

            def getmaxyx(self):
                return self.height, self.width

            def addstr(self, y, x, text, attr=0):
                self.writes.append((y, x, text, attr))

            def move(self, y, x):
                self.cursor = (y, x)

        context = {
            "title": "Task Board",
            "search": "Search",
            "active": "active columns",
            "updated": "Updated",
            "error": "Load failed",
            "state_labels": {state: state for state in STATES},
            "size_labels": {"small": "small", "large": "large"},
        }
        for width in (40, 50):
            screen = FakeScreen(24, width)
            tui = kanban_tui.KanbanTui(
                screen,
                single=True,
                refresh_interval=60,
                context=context,
                get_board=lambda: {"tasks": []},
                get_task=lambda _task_id: {},
            )
            tui.model.set_board({
                "generated_at": "2026-08-20 22:30:00",
                "tasks": [],
            })
            tui.model.query = "needle"
            tui.searching = True
            tui._render_board()
            toolbar_writes = [write for write in screen.writes if write[0] == 1]
            search_write, status_write = toolbar_writes
            self.assertIn("n", search_write[2])
            self.assertLessEqual(
                search_write[1] + kanban_tui.display_width(search_write[2]),
                status_write[1],
            )

        detail_screen = FakeScreen(12, 40)
        tui = kanban_tui.KanbanTui(
            detail_screen,
            single=True,
            refresh_interval=60,
            context=context,
            get_board=lambda: {"tasks": []},
            get_task=lambda _task_id: {},
        )
        tui.detail = {
            "task_id": "20260820-detail-task",
            "title": "Detail",
            "state": "todo",
            "kind": "small",
            "document": "# Detail\n\nBody",
        }
        tui.model.refresh_error = "board unavailable"
        tui._render_detail()
        footer = next(write for write in detail_screen.writes if write[0] == 11)
        self.assertIn("Load failed: board unavailable", footer[2])

        controller = kanban_tui.KanbanTui(
            detail_screen,
            single=True,
            refresh_interval=60,
            context=context,
            get_board=lambda: (_ for _ in ()).throw(
                UnicodeError("other card is invalid UTF-8")
            ),
            get_task=lambda task_id: {
                "task_id": task_id,
                "title": "Readable task",
                "document": "# Readable task",
            },
        )
        controller.model.set_board({
            "tasks": [
                {
                    "task_id": "20260820-readable-task",
                    "title": "Readable task",
                    "state": "backlog",
                }
            ],
        })
        controller._refresh()
        controller._open_detail()
        self.assertEqual(
            "other card is invalid UTF-8", controller.model.refresh_error
        )
        self.assertEqual(
            "other card is invalid UTF-8", controller.model.error
        )
        controller.get_board = lambda: {"tasks": []}
        controller._refresh()
        self.assertEqual("", controller.model.refresh_error)
        self.assertEqual("", controller.model.error)

    def test_tui_refresh_keeps_selection_scroll_and_updates_detail_inplace(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)

        def make_tasks(task_ids: list[str], *, title_prefix: str = "") -> list[dict]:
            return [
                {
                    "task_id": task_id,
                    "title": f"{title_prefix}{task_id}",
                    "state": "todo",
                }
                for task_id in task_ids
            ]

        task_ids = [f"20260820-keep-{index:02d}-task" for index in range(8)]
        model = kanban_tui.BoardModel()
        self.assertTrue(
            model.set_board({"generated_at": "t1", "tasks": make_tasks(task_ids)})
        )
        model.column_index = model.states.index("todo")
        model.selected_ids["todo"] = task_ids[4]
        model.selected_indexes["todo"] = 4
        model.scrolls["todo"] = 2

        self.assertTrue(
            model.set_board({
                "generated_at": "t2",
                "tasks": make_tasks(task_ids, title_prefix="updated-"),
            })
        )
        self.assertEqual(task_ids[4], model.selected_ids["todo"])
        self.assertEqual(4, model.selected_indexes["todo"])
        self.assertEqual(2, model.scrolls["todo"])

        remaining = task_ids[:4] + task_ids[5:]
        self.assertTrue(model.set_board({
            "generated_at": "t3",
            "tasks": make_tasks(remaining),
        }))
        self.assertEqual(task_ids[5], model.selected_ids["todo"])
        self.assertEqual(4, model.selected_indexes["todo"])
        self.assertEqual(2, model.scrolls["todo"])

        self.assertFalse(model.set_board({
            "generated_at": "t4",
            "tasks": make_tasks(remaining),
        }))
        self.assertEqual("t4", model.generated_at)
        self.assertFalse(model.set_board({
            "generated_at": "t4",
            "tasks": make_tasks(remaining),
        }))
        self.assertEqual(task_ids[5], model.selected_ids["todo"])
        self.assertEqual(2, model.scrolls["todo"])

        class FakeScreen:
            def getmaxyx(self):
                return 24, 80

        details = [
            {
                "task_id": "20260820-keep-task",
                "title": "Keep",
                "document": "one\n" * 8,
                "state": "todo",
            },
            {
                "task_id": "20260820-keep-task",
                "title": "Keep",
                "document": "two\n" * 8,
                "state": "todo",
            },
        ]
        boards = [
            {
                "generated_at": "d1",
                "tasks": [{
                    "task_id": "20260820-keep-task",
                    "title": "Keep",
                    "state": "todo",
                }],
            },
            {
                "generated_at": "d2",
                "tasks": [{
                    "task_id": "20260820-keep-task",
                    "title": "Keep changed",
                    "state": "todo",
                }],
            },
        ]
        tui = kanban_tui.KanbanTui(
            FakeScreen(),
            single=True,
            refresh_interval=30,
            context={},
            get_board=lambda: boards[1],
            get_task=lambda _task_id: details[1],
        )
        tui.model.set_board(boards[0])
        tui.detail = details[0]
        tui.detail_scroll = 3
        self.assertTrue(tui._refresh())
        self.assertEqual("two\n" * 8, tui.detail["document"])
        self.assertEqual(3, tui.detail_scroll)
        self.assertEqual("20260820-keep-task", tui.model.selected_ids["todo"])

    def test_tui_render_updates_inplace_without_erase(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)

        class FakeScreen:
            def __init__(self) -> None:
                self.writes = []
                self.erased = 0

            def getmaxyx(self):
                return 24, 80

            def erase(self):
                self.erased += 1

            def addstr(self, y, x, text, attr=0):
                self.writes.append((y, x, text, attr))

            def refresh(self):
                pass

            def move(self, y, x):
                pass

        context = {
            "title": "Task Board",
            "search": "Search",
            "active": "active",
            "updated": "Updated",
            "state_labels": {state: state for state in STATES},
            "size_labels": {"small": "small", "large": "large"},
            "help": "help",
        }
        first_board = {
            "generated_at": "2026-08-21 00:00:00",
            "tasks": [{
                "task_id": "20260821-inplace-task",
                "title": "Old title",
                "state": "backlog",
                "type": "chore",
                "kind": "small",
                "assignee": "codex",
                "time": "08-21 00:00",
            }],
        }
        screen = FakeScreen()
        tui = kanban_tui.KanbanTui(
            screen,
            single=True,
            refresh_interval=30,
            context=context,
            get_board=lambda: first_board,
            get_task=lambda _task_id: {},
        )
        tui.model.set_board(first_board)
        tui._render(force=True)
        self.assertEqual(0, screen.erased)
        first_writes = list(screen.writes)
        self.assertTrue(any("Old title" in text for _y, _x, text, _attr in first_writes))

        screen.writes.clear()
        tui._render()
        self.assertEqual([], screen.writes)
        self.assertEqual(0, screen.erased)

        next_board = {
            "generated_at": "2026-08-21 00:00:30",
            "tasks": [{
                **first_board["tasks"][0],
                "title": "New title",
            }],
        }
        self.assertTrue(tui.model.set_board(next_board))
        tui._render()
        self.assertEqual(0, screen.erased)
        self.assertTrue(any("New" in text for _y, _x, text, _attr in screen.writes))
        self.assertFalse(any("Old" in text for _y, _x, text, _attr in screen.writes))
        self.assertLess(len(screen.writes), len(first_writes))

    def test_tui_auto_refresh_runs_after_interval(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_tui
        finally:
            sys.path.pop(0)
        import curses

        class FakeScreen:
            def __init__(self) -> None:
                self.keys = [curses.error("timeout"), "q"]

            def getmaxyx(self):
                return 24, 80

            def keypad(self, _enabled):
                pass

            def timeout(self, _milliseconds):
                pass

            def get_wch(self):
                key = self.keys.pop(0)
                if isinstance(key, Exception):
                    raise key
                return key

            def addstr(self, y, x, text, attr=0):
                pass

            def refresh(self):
                pass

            def move(self, y, x):
                pass

            def bkgd(self, *_args):
                pass

        calls = {"board": 0}

        def get_board() -> dict:
            calls["board"] += 1
            return {"generated_at": str(calls["board"]), "tasks": []}

        tui = kanban_tui.KanbanTui(
            FakeScreen(),
            single=True,
            refresh_interval=30,
            context={"title": "Task Board"},
            get_board=get_board,
            get_task=lambda _task_id: {},
        )
        tui.last_refresh = 0
        renders = {"count": 0}
        original_render = tui._render

        def counted_render(*, force: bool = False) -> None:
            renders["count"] += 1
            original_render(force=force)

        with mock.patch.object(kanban_tui.KanbanTui, "_init_style"), mock.patch.object(
            tui, "_render", side_effect=counted_render
        ), mock.patch.object(
            kanban_tui.time, "monotonic", side_effect=[30, 30, 31, 31, 31]
        ):
            tui.run({"generated_at": "0", "tasks": []})
        self.assertEqual(1, calls["board"])
        self.assertEqual(1, renders["count"])
        self.assertFalse(tui.running)

    def test_tui_single_mode_searches_opens_detail_and_quits_on_a_pty(self) -> None:
        self.make_todo("tui-pty")
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
        process = subprocess.Popen(
            [
                sys.executable,
                str(COMMAND),
                "tui",
                "--single",
                "--refresh",
                "1",
            ],
            env={**self.env, "TERM": "xterm-256color"},
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
        )
        os.close(slave)
        try:
            os.write(master, b"l/tui-pty\n\njqarq")
            returncode = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
            self.fail("TUI did not exit after q")
        finally:
            os.close(master)
        self.assertEqual(0, returncode)

    def test_web_task_group_supports_new_legacy_and_missing_metadata(self) -> None:
        task_id, task = self.make_todo("web-task-group")
        sys.path.insert(0, str(COMMAND.parent))
        try:
            kanban = runpy.run_path(str(COMMAND), run_name="kanban_web_group_test")
        finally:
            sys.path.pop(0)

        current = task.read_text(encoding="utf-8")
        missing = current.replace("- 任务组:\n", "", 1)
        task.write_text(missing, encoding="utf-8")
        payload = kanban["web_board_payload"](self.root)
        self.assertEqual("", payload["tasks"][0]["task_group"])

        legacy_group = "20260820-legacy-web-group"
        legacy = missing.replace(
            "## 讨论与决策\n\n",
            f"## 讨论与决策\n\n任务组: {legacy_group}\n前置任务: N/A\n\n",
            1,
        )
        task.write_text(legacy, encoding="utf-8")
        payload = kanban["web_task_payload"](self.root, task_id)
        self.assertEqual(legacy_group, payload["task_group"])

        current_group = "20260820-current-web-group"
        current_with_legacy = current.replace(
            "- 任务组:\n", f"- 任务组: {current_group}\n", 1
        ).replace(
            "## 讨论与决策\n\n",
            f"## 讨论与决策\n\n任务组: {legacy_group}\n前置任务: N/A\n\n",
            1,
        )
        task.write_text(current_with_legacy, encoding="utf-8")
        payload = kanban["web_task_payload"](self.root, task_id)
        self.assertEqual(current_group, payload["task_group"])

    def test_web_task_group_card_renders_as_badge_after_task_id(self) -> None:
        script = (PROJECT_ROOT / "share" / "kanban-web" / "board.js").read_text(
            encoding="utf-8"
        )
        css = (PROJECT_ROOT / "share" / "kanban-web" / "board.css").read_text(
            encoding="utf-8"
        )
        title_at = script.index('makeElement("p", "task-title")')
        task_id_at = script.index('makeElement("p", "task-id")')
        group_at = script.index('makeElement("span", "badge task-group")')
        self.assertLess(title_at, task_id_at)
        self.assertLess(task_id_at, group_at)
        self.assertIn("taskGroup.hidden = !task.task_group", script)
        self.assertIn("task.task_group,", script)
        self.assertIn("console.error(detail)", script)
        self.assertIn("config.errorLabel", script)
        self.assertNotIn("error.message", script)

        group_style = re.search(r"\.badge\.task-group\s*\{([^}]+)\}", css)
        self.assertIsNotNone(group_style)
        group_css = group_style.group(1)
        self.assertIn("overflow-wrap: anywhere", group_css)
        self.assertIn("max-width: 100%", group_css)
        self.assertIn("width: fit-content", group_css)
        self.assertIn("[hidden]", css)
        self.assertIn("display: none !important", css)
        # MD2 chips 用 stadium 形状; 任务组徽章保留小圆角以容纳换行.
        self.assertIn("border-radius: 999px", css)
        self.assertIn("border-radius: var(--radius)", group_css)

    def test_web_sse_only_publishes_content_changes(self) -> None:
        import queue

        sys.path.insert(0, str(COMMAND.parent))
        try:
            import kanban_web
        finally:
            sys.path.pop(0)

        state = {"generated": 0, "title": "first"}

        def get_board():
            state["generated"] += 1
            return {
                "generated_at": str(state["generated"]),
                "tasks": [{"task_id": "task", "title": state["title"]}],
            }

        server = kanban_web.KanbanWebServer(
            ("127.0.0.1", 0),
            PROJECT_ROOT / "share" / "kanban-web",
            {},
            get_board,
            lambda _task_id: {},
        )
        subscriber = None
        try:
            server._refresh_board(force=True)
            subscriber = server.subscribe()
            event_name, revision, initial = subscriber.get_nowait()
            self.assertEqual(("board", 1, "first"), (
                event_name,
                revision,
                initial["tasks"][0]["title"],
            ))

            server._refresh_board()
            self.assertEqual("2", server.current_board()["generated_at"])
            with self.assertRaises(queue.Empty):
                subscriber.get_nowait()

            state["title"] = "second"
            server._refresh_board()
            event_name, revision, changed = subscriber.get_nowait()
            self.assertEqual(("board", 2, "second"), (
                event_name,
                revision,
                changed["tasks"][0]["title"],
            ))
        finally:
            if subscriber is not None:
                server.unsubscribe(subscriber)
            server.server_close()

    def test_web_serves_board_and_refreshes_task_state(self) -> None:
        import json
        import signal
        import socket
        import time
        import urllib.error
        import urllib.request

        def read_sse_event(response):
            event_name = ""
            data_lines = []
            while True:
                raw_line = response.readline()
                self.assertTrue(raw_line, "SSE stream closed before an event")
                line = raw_line.decode("utf-8").rstrip("\r\n")
                if not line:
                    if data_lines:
                        return event_name, json.loads("\n".join(data_lines))
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    event_name = line.partition(":")[2].strip()
                elif line.startswith("data:"):
                    data_lines.append(line.partition(":")[2].lstrip())

        task_id, _path = self.make_todo("web-board")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]

        process = subprocess.Popen(
            [
                sys.executable,
                str(COMMAND),
                "web",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--refresh",
                "1",
                "--assets",
                str(PROJECT_ROOT / "share" / "kanban-web"),
            ],
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.time() + 5
            started = False
            while time.time() < deadline:
                if process.poll() is not None:
                    err = process.stderr.read() if process.stderr else ""
                    self.fail(err or "web exited early")
                line = process.stdout.readline() if process.stdout else ""
                if not line:
                    time.sleep(0.05)
                    continue
                if f"http://127.0.0.1:{port}/" in line:
                    started = True
                    break
            self.assertTrue(started, "web server did not print listen URL")

            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                html = response.read().decode("utf-8")
            self.assertIn("任务看板", html)
            self.assertIn("SSE", html)

            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/static/board.js", timeout=2
            ) as response:
                script = response.read().decode("utf-8")
            self.assertIn("/api/board", script)
            self.assertIn('new EventSource("/api/events")', script)
            self.assertIn("insertBefore", script)
            self.assertNotIn("boardEl.innerHTML", script)
            self.assertNotIn("setInterval", script)
            self.assertIn("KanbanMarkdown.renderMarkdown", script)
            self.assertIn('makeElement("span", "badge task-group")', script)
            self.assertIn("taskGroup.hidden = !task.task_group", script)

            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/static/markdown.js", timeout=2
            ) as response:
                markdown_js = response.read().decode("utf-8")
            self.assertIn("function renderMarkdown", markdown_js)

            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                page = response.read().decode("utf-8")
            self.assertIn("/static/markdown.js", page)

            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/board", timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(1, len(payload["tasks"]))
            self.assertEqual(task_id, payload["tasks"][0]["task_id"])
            self.assertEqual("todo", payload["tasks"][0]["state"])

            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/events", timeout=4
            ) as events:
                event_name, initial = read_sse_event(events)
                self.assertEqual("board", event_name)
                self.assertEqual("todo", initial["tasks"][0]["state"])

                self.run_command("move", task_id, "working")
                event_name, refreshed = read_sse_event(events)
                self.assertEqual("board", event_name)
                self.assertEqual("working", refreshed["tasks"][0]["state"])

                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/tasks/{task_id}", timeout=2
                ) as response:
                    detail = json.loads(response.read().decode("utf-8"))
                self.assertEqual(task_id, detail["task_id"])
                self.assertIn("# ", detail["document"])

                task_path = self.root / "working" / f"{task_id}.md"
                task_path.write_bytes(b"\xff")
                event_name, error_event = read_sse_event(events)
                self.assertEqual("board-error", event_name)
                self.assertIn("UTF-8", error_event["error"])

                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/board", timeout=2
                    )
                error_response = caught.exception
                try:
                    self.assertEqual(400, error_response.code)
                    error_payload = json.loads(error_response.read().decode("utf-8"))
                    self.assertIn("UTF-8", error_payload["error"])
                finally:
                    error_response.close()
        finally:
            process.send_signal(signal.SIGINT)
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=3)

    def test_web_rejects_invalid_port(self) -> None:
        result = self.run_command("web", "--port", "70000", succeeds=False)
        self.assertIn("无效端口", result.stderr)
        self.assertNotIn("invalid port", result.stderr)

    def test_web_rejects_non_integer_port_with_chinese_message(self) -> None:
        result = self.run_command("web", "--port", "nope", succeeds=False)
        self.assertIn("无效 int 值", result.stderr)
        self.assertNotIn("invalid int value", result.stderr)

    def test_tui_rejects_non_integer_refresh_with_chinese_message(self) -> None:
        result = self.run_command("tui", "--refresh", "nope", succeeds=False)
        self.assertIn("无效 int 值", result.stderr)
        self.assertNotIn("invalid int value", result.stderr)

    def test_web_missing_assets_reports_localized_error(self) -> None:
        result = self.run_command(
            "web",
            "--assets",
            "/definitely/missing-kanban-web-assets",
            succeeds=False,
        )
        self.assertIn("Web 资源目录不存在", result.stderr)
        self.assertNotIn("web assets directory not found", result.stderr)

    def test_web_invalid_port_is_english_when_locale_is_en(self) -> None:
        self.env["ONEVOKE_LANG"] = "en"
        result = self.run_command("web", "--port", "70000", succeeds=False)
        self.assertIn("invalid port", result.stderr)
        self.assertNotIn("无效端口", result.stderr)

    def test_tui_terminal_init_failure_is_localized(self) -> None:
        sys.path.insert(0, str(COMMAND.parent))
        try:
            import curses
            import kanban_tui
        finally:
            sys.path.pop(0)

        board = {
            "generated_at": "2026-08-22 22:30:00",
            "tasks": [],
        }
        context = {"terminal_init_failed": "终端初始化失败"}
        with mock.patch(
            "kanban_tui.curses.wrapper",
            side_effect=curses.error("setup failed"),
        ):
            with self.assertRaises(kanban_tui.KanbanTuiError) as caught:
                kanban_tui.run(
                    single=True,
                    refresh_interval=30,
                    context=context,
                    get_board=lambda: board,
                    get_task=lambda task_id: {"task_id": task_id, "document": ""},
                    theme="auto",
                )
        self.assertIn("终端初始化失败", str(caught.exception))
        self.assertNotIn("failed to initialize terminal", str(caught.exception))


def _same_real_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(
        os.path.realpath(right)
    )


@unittest.skipUnless(os.name == "posix", "POSIX project installer tests require a shell")
class PosixProjectInstallerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()

    def tearDown(self) -> None:
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

    def run_installer(
        self,
        *args: str,
        home: Optional[Path] = None,
        extra_env: Optional[dict[str, str]] = None,
    ) -> subprocess.CompletedProcess:
        env = install_env(home or self.home)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["sh", str(INSTALLER), *args],
            stdin=subprocess.DEVNULL,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def assert_same_real_path(self, left: Path, right: Path) -> None:
        self.assertTrue(_same_real_path(left, right), f"{left} != {right}")

    def assert_home_has_no_onevoke(self, home: Path) -> None:
        for relative in (
            ".agents",
            ".local/bin",
            ".local/share/onevoke",
            ".config/onevoke",
        ):
            self.assertFalse(
                (home / relative).exists(),
                f"global path was created: {relative}",
            )

    def assert_project_payload(self, project: Path) -> Path:
        install_root = project / ".onevoke"
        dest_bin = install_root / "bin"
        dest_rules = install_root / "rules"
        dest_share = install_root / "share" / "kanban-web"
        for source in sorted((PROJECT_ROOT / "bin").iterdir()):
            if source.is_file():
                installed = dest_bin / source.name
                self.assertEqual(source.read_bytes(), installed.read_bytes(), source.name)
                self.assertTrue(os.access(installed, os.X_OK), source.name)
        for source in sorted(RULES_DIR.glob("*.md")):
            self.assertEqual(
                source.read_bytes(),
                (dest_rules / source.name).read_bytes(),
                source.name,
            )
        for source in sorted((PROJECT_ROOT / "share" / "kanban-web").iterdir()):
            if source.is_file():
                self.assertEqual(
                    source.read_bytes(),
                    (dest_share / source.name).read_bytes(),
                    source.name,
                )
        agent_rules = dest_rules / "AGENTS.md"
        self.assertTrue(agent_rules.is_symlink())
        self.assertEqual(Path("ONEVOKE-AGENTS.md"), agent_rules.readlink())
        exclude = (project / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        self.assertEqual(1, exclude.splitlines().count("/.onevoke/"))
        return dest_bin

    def assert_command_paths(self, stdout: str, dest_bin: Path) -> None:
        lines = stdout.splitlines()
        self.assertGreaterEqual(len(lines), 3, stdout)
        self.assertEqual(INSTALLED_ZH.strip(), lines[0])
        printed = [Path(line) for line in lines[1:3]]
        self.assertTrue(
            any(_same_real_path(path, dest_bin / "onevoke") for path in printed),
            stdout,
        )
        self.assertTrue(
            any(_same_real_path(path, dest_bin / "kanban") for path in printed),
            stdout,
        )

    def test_project_install_copies_payload_and_skips_global_and_welcome(self) -> None:
        project = self.init_git_repo(self.root / "app")
        canary = self.home / "keep"
        canary.write_text("home-canary\n", encoding="utf-8")
        result = self.run_installer("--project", str(project))

        self.assertEqual(0, result.returncode, result.stderr)
        dest_bin = self.assert_project_payload(project)
        self.assert_command_paths(result.stdout, dest_bin)
        self.assertIn("未修改 PATH", result.stderr)
        self.assertNotIn("welcome 未完成", result.stderr)
        self.assertNotIn("请在终端运行 onevoke welcome", result.stderr)
        self.assertNotIn("检测到已退役的 Reviewer 脚本", result.stderr)
        self.assert_home_has_no_onevoke(self.home)
        self.assertEqual("home-canary\n", canary.read_text(encoding="utf-8"))
        self.assertFalse((project / ".onevoke" / "config.json").exists())

    def test_project_install_from_linked_worktree_uses_main(self) -> None:
        main = self.init_git_repo(self.root / "app")
        linked = self.root / "app-linked"
        subprocess.run(
            ["git", "-C", str(main), "worktree", "add", "-q", str(linked), "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )

        result = self.run_installer("--lang", "cn", "--project", str(linked))

        self.assertEqual(0, result.returncode, result.stderr)
        dest_bin = self.assert_project_payload(main)
        self.assert_command_paths(result.stdout, dest_bin)
        self.assertFalse((linked / ".onevoke").exists())
        self.assert_home_has_no_onevoke(self.home)

    def test_project_install_is_idempotent_and_keeps_foreign_files(self) -> None:
        project = self.init_git_repo(self.root / "app")
        first = self.run_installer("--project", str(project))
        self.assertEqual(0, first.returncode, first.stderr)
        notes = project / ".onevoke" / "user-notes.txt"
        notes.write_text("keep-me\n", encoding="utf-8")
        exclude = project / ".git" / "info" / "exclude"
        original_mode = exclude.stat().st_mode & 0o777

        second = self.run_installer("--project", str(project))

        self.assertEqual(0, second.returncode, second.stderr)
        dest_bin = self.assert_project_payload(project)
        self.assert_command_paths(second.stdout, dest_bin)
        self.assertEqual("keep-me\n", notes.read_text(encoding="utf-8"))
        self.assertEqual(original_mode, exclude.stat().st_mode & 0o777)

    def test_project_install_does_not_read_global_config_language(self) -> None:
        project = self.init_git_repo(self.root / "app")
        config_dir = self.home / ".config" / "onevoke"
        config_dir.mkdir(parents=True)
        config_dir.joinpath("config.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "welcome_complete": True,
                    "kanban_agent": "codex",
                    "launcher": "tmux",
                    "language": "en",
                    "reviewers": {
                        role: "codex" for role in ("PM", "CSA", "Hacker", "QA")
                    },
                    "memsearch": {"enabled": False},
                }
            ),
            encoding="utf-8",
        )
        marker = self.home / ".local" / "bin" / "onevoke"
        marker.parent.mkdir(parents=True)
        marker.write_text("GLOBAL-ONEVOKE\n", encoding="utf-8")
        legacy = self.home / ".local" / "bin" / "codex-review.sh"
        legacy.write_text("legacy\n", encoding="utf-8")

        result = self.run_installer(
            "--project",
            str(project),
            extra_env={"ONEVOKE_LANG": "zh"},
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(result.stdout.startswith(INSTALLED_ZH))
        self.assertNotIn(INSTALLED_EN.strip(), result.stdout)
        self.assertEqual("GLOBAL-ONEVOKE\n", marker.read_text(encoding="utf-8"))
        self.assertEqual("legacy\n", legacy.read_text(encoding="utf-8"))
        self.assertNotIn("退役", result.stderr)

    def test_project_install_rejects_non_git_and_does_not_fallback(self) -> None:
        directory = self.root / "not-git"
        directory.mkdir()
        result = self.run_installer("--project", str(directory))

        self.assertNotEqual(0, result.returncode)
        self.assertIn("项目不是 Git 仓库", result.stderr)
        self.assertFalse((directory / ".onevoke").exists())
        self.assert_home_has_no_onevoke(self.home)
        self.assertNotIn(INSTALLED_ZH.strip(), result.stdout)

    def test_project_install_rejects_missing_directory(self) -> None:
        missing = self.root / "missing"
        result = self.run_installer("--project", str(missing))
        self.assertNotEqual(0, result.returncode)
        self.assertIn("项目目录不存在", result.stderr)
        self.assert_home_has_no_onevoke(self.home)

    def test_project_install_rejects_symlink_target(self) -> None:
        project = self.init_git_repo(self.root / "app")
        link = self.root / "app-link"
        link.symlink_to(project)
        result = self.run_installer("--project", str(link))

        self.assertNotEqual(0, result.returncode)
        self.assertTrue(
            "符号链接" in result.stderr or "symlink" in result.stderr,
            result.stderr,
        )
        self.assertFalse((project / ".onevoke").exists())
        self.assert_home_has_no_onevoke(self.home)

    def test_project_install_rejects_onevoke_symlink(self) -> None:
        project = self.init_git_repo(self.root / "app")
        outside = self.root / "payload"
        outside.mkdir()
        marker = outside / "keep"
        marker.write_text("outside\n", encoding="utf-8")
        (project / ".onevoke").symlink_to(outside)

        result = self.run_installer("--project", str(project))

        self.assertNotEqual(0, result.returncode)
        self.assertIn("符号链接", result.stderr)
        self.assertTrue((project / ".onevoke").is_symlink())
        self.assertEqual("outside\n", marker.read_text(encoding="utf-8"))
        self.assertEqual(["keep"], [path.name for path in outside.iterdir()])
        self.assert_home_has_no_onevoke(self.home)

    def test_project_install_rejects_directory_file_target(self) -> None:
        project = self.init_git_repo(self.root / "app")
        blocked = project / ".onevoke" / "bin" / "kanban"
        blocked.mkdir(parents=True)
        result = self.run_installer("--project", str(project))

        self.assertEqual(1, result.returncode)
        self.assertIn("安装目标是目录", result.stderr)
        self.assertTrue(blocked.is_dir())
        self.assertFalse((project / ".onevoke" / "bin" / "onevoke").exists())
        exclude = project / ".git" / "info" / "exclude"
        self.assertNotIn(
            "/.onevoke/",
            exclude.read_text(encoding="utf-8").splitlines(),
        )
        self.assert_home_has_no_onevoke(self.home)

    def test_project_install_rejects_invalid_arguments(self) -> None:
        project = self.init_git_repo(self.root / "app")
        missing = self.run_installer("--project")
        self.assertEqual(2, missing.returncode)
        self.assertIn("用法: install.sh", missing.stderr)
        self.assertIn("--project 需要目录", missing.stderr)

        extra = self.run_installer("--project", str(project), "--force")
        self.assertEqual(2, extra.returncode)
        self.assertIn("用法: install.sh", extra.stderr)
        self.assertFalse((project / ".onevoke").exists())

        duplicate = self.run_installer(
            "--project",
            str(project),
            "--project",
            str(project),
        )
        self.assertEqual(2, duplicate.returncode)
        self.assertIn("--project 只能指定一次", duplicate.stderr)

        help_text = self.run_installer("--help")
        self.assertEqual(0, help_text.returncode, help_text.stderr)
        self.assertIn("--project", help_text.stdout)
        self.assert_home_has_no_onevoke(self.home)


class KanbanProjectInstallTest(unittest.TestCase):
    """项目安装入口必须使用主 worktree `.onevoke/`, 且不得回落全局资源."""

    def setUp(self) -> None:
        self.language = mock.patch.dict(os.environ, {"ONEVOKE_LANG": "zh"})
        self.language.start()
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
                "ONEVOKE_SHARE",
                "KANBAN_DIR",
            )
        }
        os.environ["HOME"] = str(self.home)
        os.environ["USERPROFILE"] = str(self.home)
        for name in ("ONEVOKE_CONFIG", "ONEVOKE_SHARE", "KANBAN_DIR"):
            os.environ.pop(name, None)
        global_rules = self.home / ".agents"
        global_rules.mkdir(parents=True)
        (global_rules / "KANBAN-RULES.md").write_text(
            "# 全局文件看板规则\nglobal-rules-marker\n",
            encoding="utf-8",
        )
        global_share = self.home / ".local" / "share" / "onevoke" / "kanban-web"
        global_share.mkdir(parents=True)
        (global_share / "board.html").write_text("GLOBAL-ASSET\n", encoding="utf-8")
        self.env = os.environ.copy()
        self.env["HOME"] = str(self.home)
        self.env["USERPROFILE"] = str(self.home)
        self.env["ONEVOKE_LANG"] = "zh"
        for name in ("ONEVOKE_CONFIG", "ONEVOKE_SHARE", "KANBAN_DIR", "TMUX", "TMUX_PANE"):
            self.env.pop(name, None)

    def tearDown(self) -> None:
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.temp.cleanup()
        self.language.stop()

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

    def install_project_layout(self, project: Path, *, with_rules: bool = True, with_share: bool = True) -> Path:
        bin_dir = project / ".onevoke" / "bin"
        bin_dir.mkdir(parents=True)
        for name in (
            "kanban",
            "onevoke_config.py",
            "onevoke_fs.py",
            "kanban_web.py",
            "kanban_tui.py",
        ):
            shutil.copy2(PROJECT_ROOT / "bin" / name, bin_dir / name)
        if with_rules:
            rules_dir = project / ".onevoke" / "rules"
            rules_dir.mkdir(parents=True, exist_ok=True)
            (rules_dir / "KANBAN-RULES.md").write_text(
                "# 项目看板规则\nproject-rules-marker\n",
                encoding="utf-8",
            )
        if with_share:
            share_dir = project / ".onevoke" / "share" / "kanban-web"
            share_dir.mkdir(parents=True, exist_ok=True)
            (share_dir / "board.html").write_text("PROJECT-ASSET\n", encoding="utf-8")
        (project / "AGENTS.md").write_text("# 目标项目规则\n", encoding="utf-8")
        return bin_dir / "kanban"

    def run_entry(
        self,
        entry: Path,
        *args: str,
        cwd: Optional[Path] = None,
        env: Optional[dict] = None,
        succeeds: bool = True,
    ) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [sys.executable, str(entry), *args],
            cwd=str(cwd or entry.parent),
            env=env or self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        if succeeds and result.returncode != 0:
            self.fail(result.stderr)
        if not succeeds and result.returncode == 0:
            self.fail(f"command unexpectedly succeeded: {' '.join(args)}")
        return result

    def run_module(self, bin_dir: Path, script: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(bin_dir),
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_source_tree_rules_stay_global_even_if_project_layout_exists(self) -> None:
        project = self.init_git_repo(self.root / "app")
        self.install_project_layout(project)
        result = subprocess.run(
            [sys.executable, str(COMMAND), "rules"],
            cwd=str(project),
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("global-rules-marker", result.stdout)
        self.assertNotIn("project-rules-marker", result.stdout)

    def test_project_rules_from_main_and_task_worktree(self) -> None:
        main = self.init_git_repo(self.root / "app")
        entry = self.install_project_layout(main)
        linked = self.root / "app-work"
        subprocess.run(
            ["git", "-C", str(main), "worktree", "add", "-q", str(linked), "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        from_main = self.run_entry(entry, "rules", cwd=main)
        from_work = self.run_entry(entry, "rules", cwd=linked)
        self.assertIn("project-rules-marker", from_main.stdout)
        self.assertIn("project-rules-marker", from_work.stdout)
        self.assertNotIn("global-rules-marker", from_main.stdout)
        self.assertNotIn("global-rules-marker", from_work.stdout)
        init = self.run_entry(entry, "init", cwd=main)
        init_text = init.stdout.replace("\\", "/")
        self.assertIn("/.onevoke/rules/KANBAN-RULES.md", init_text)
        self.assertNotIn("/.agents/KANBAN-RULES.md", init_text)

    def test_project_rules_missing_does_not_fall_back_to_global(self) -> None:
        project = self.init_git_repo(self.root / "app")
        entry = self.install_project_layout(project, with_rules=False)
        result = self.run_entry(entry, "rules", cwd=project, succeeds=False)
        self.assertIn("项目规则不存在", result.stderr)
        self.assertIn("/.onevoke/rules/KANBAN-RULES.md", result.stderr.replace("\\", "/"))
        self.assertNotIn("全局规则不存在", result.stderr)
        self.assertNotIn("global-rules-marker", result.stdout)
        self.assertNotIn("project-rules-marker", result.stdout)

    def test_project_entry_without_git_does_not_use_global_rules(self) -> None:
        project = self.root / "not-git"
        entry = self.install_project_layout(project)
        result = self.run_entry(entry, "rules", cwd=project, succeeds=False)
        self.assertIn("项目不是 Git 仓库", result.stderr)
        self.assertNotIn("global-rules-marker", result.stdout)

    @unittest.skipUnless(os.name == "posix", "symlink install-root rejection is POSIX-specific")
    def test_project_rules_rejects_symlinked_install_root(self) -> None:
        project = self.init_git_repo(self.root / "app")
        real = self.root / "payload"
        entry = self.install_project_layout(real)
        (project / ".onevoke").symlink_to(real / ".onevoke")
        linked_entry = project / ".onevoke" / "bin" / "kanban"
        result = self.run_entry(linked_entry, "rules", cwd=project, succeeds=False)
        self.assertIn("路径分量不得是符号链接", result.stderr)
        self.assertNotIn("project-rules-marker", result.stdout)
        self.assertNotIn("global-rules-marker", result.stdout)
        self.assertTrue(entry.is_file())

    def test_project_web_uses_local_share_and_skips_global(self) -> None:
        project = self.init_git_repo(self.root / "app")
        entry = self.install_project_layout(project)
        self.env["ONEVOKE_SHARE"] = str(self.home / ".local" / "share" / "onevoke")
        resolved = self.run_module(
            entry.parent,
            "import kanban_web; print(kanban_web.resolve_share_dir())",
        )
        self.assertEqual(0, resolved.returncode, resolved.stderr)
        share = Path(resolved.stdout.strip())
        self.assertIn("/.onevoke/share/kanban-web", str(share).replace("\\", "/"))
        self.assertNotIn("/.local/share/onevoke/kanban-web", str(share).replace("\\", "/"))
        self.assertEqual("PROJECT-ASSET\n", (share / "board.html").read_text(encoding="utf-8"))

    def test_project_web_missing_assets_do_not_use_global_share(self) -> None:
        project = self.init_git_repo(self.root / "app")
        entry = self.install_project_layout(project, with_share=False)
        self.env["ONEVOKE_SHARE"] = str(self.home / ".local" / "share" / "onevoke")
        resolved = self.run_module(
            entry.parent,
            "import kanban_web\n"
            "try:\n"
            "    kanban_web.resolve_share_dir()\n"
            "except kanban_web.KanbanWebError as error:\n"
            "    raise SystemExit(str(error))\n",
        )
        self.assertNotEqual(0, resolved.returncode)
        self.assertIn("未找到项目 kanban web 资源", resolved.stderr)
        self.assertIn("/.onevoke/share/kanban-web", resolved.stderr.replace("\\", "/"))
        self.assertNotIn("install.sh", resolved.stderr)
        self.assertNotIn("GLOBAL-ASSET", resolved.stdout)

    def test_project_tui_prefs_use_project_config_dir(self) -> None:
        project = self.init_git_repo(self.root / "app")
        entry = self.install_project_layout(project)
        resolved = self.run_module(
            entry.parent,
            "import kanban_tui; print(kanban_tui.prefs_path())",
        )
        self.assertEqual(0, resolved.returncode, resolved.stderr)
        self.assertIn("/.onevoke/tui.json", resolved.stdout.strip().replace("\\", "/"))
        self.assertNotIn("/.config/onevoke/tui.json", resolved.stdout.replace("\\", "/"))
        global_prefs = self.run_module(
            COMMAND.parent,
            "import kanban_tui; print(kanban_tui.prefs_path())",
        )
        self.assertEqual(0, global_prefs.returncode, global_prefs.stderr)
        self.assertIn(
            "/.config/onevoke/tui.json",
            global_prefs.stdout.strip().replace("\\", "/"),
        )
        self.assertNotIn("/.onevoke/tui.json", global_prefs.stdout.replace("\\", "/"))

    def test_start_prompt_uses_absolute_project_paths(self) -> None:
        import importlib.util
        from importlib.machinery import SourceFileLoader

        sys.path.insert(0, str(COMMAND.parent))
        try:
            loader = SourceFileLoader("kanban_prompt_test", str(COMMAND))
            spec = importlib.util.spec_from_loader(loader.name, loader)
            if spec is None:
                self.fail(f"unable to load {COMMAND}")
            kanban_mod = importlib.util.module_from_spec(spec)
            loader.exec_module(kanban_mod)
            import onevoke_config
        finally:
            sys.path.pop(0)
        global_paths = onevoke_config.install_paths(entry=COMMAND)
        global_prompt = kanban_mod.start_agent_prompt("20260825-demo-task", global_paths)
        self.assertIn("先运行 kanban rules", global_prompt)
        self.assertIn("遵守目标项目 AGENTS.md", global_prompt)
        self.assertNotIn(".onevoke/bin/kanban", global_prompt)

        project = self.init_git_repo(self.root / "app")
        entry = self.install_project_layout(project)
        project_paths = onevoke_config.install_paths(entry=entry)
        prompt = kanban_mod.start_agent_prompt("20260825-demo-task", project_paths)
        self.assertEqual("project", project_paths.mode)
        kanban_cmd = str(project_paths.bin_dir / "kanban")
        agents_md = str((project_paths.project_root or project) / "AGENTS.md")
        self.assertIn(f"先运行 {kanban_cmd} rules", prompt)
        self.assertIn(f"{kanban_cmd} show 20260825-demo-task", prompt)
        self.assertIn(f"遵守 {agents_md}", prompt)
        self.assertNotIn("先运行 kanban rules", prompt)

    @unittest.skipUnless(os.name == "posix", "tmux launcher coverage requires POSIX")
    def test_project_start_from_task_worktree_uses_project_command_and_config(self) -> None:
        main = self.init_git_repo(self.root / "app")
        entry = self.install_project_layout(main)
        linked = self.root / "app-work"
        subprocess.run(
            ["git", "-C", str(main), "worktree", "add", "-q", str(linked), "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        board = main / "kanban"
        self.run_entry(entry, "init", cwd=linked)
        today = datetime.now().strftime("%Y%m%d")
        task_id = f"{today}-project-start-task"
        self.run_entry(entry, "new", "chore", "project-start", "项目启动", cwd=linked)
        task = board / "backlog" / f"{task_id}.md"
        text = task.read_text(encoding="utf-8")
        replacements = ("实现目标", "产生可验证结果", "满足验收", "无额外范围")
        for replacement in replacements:
            text = text.replace("<填写>", replacement, 1)
        task.write_text(text, encoding="utf-8")
        self.run_entry(entry, "move", task_id, "todo", cwd=linked)

        global_config = self.home / ".config" / "onevoke" / "config.json"
        global_config.parent.mkdir(parents=True)
        global_config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "welcome_complete": True,
                    "kanban_agent": "claude",
                    "launcher": "tmux",
                    "reviewers": {
                        "PM": "codex",
                        "CSA": "codex",
                        "Hacker": "codex",
                        "QA": "codex",
                    },
                    "memsearch": {"enabled": False},
                }
            ),
            encoding="utf-8",
        )
        (main / ".onevoke" / "config.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "welcome_complete": True,
                    "kanban_agent": "grok",
                    "launcher": "tmux",
                    "reviewers": {
                        "PM": "codex",
                        "CSA": "codex",
                        "Hacker": "codex",
                        "QA": "codex",
                    },
                    "memsearch": {"enabled": False},
                }
            ),
            encoding="utf-8",
        )

        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        tmux = fake_bin / "tmux"
        tmux.write_text(
            """#!/bin/sh
if [ "$1" = "display-message" ]; then
    printf '%s\\n' '$42'
    exit 0
fi
if [ "$1" = "respawn-pane" ]; then
    printf '%s\\n' "$5" > "$KANBAN_TMUX_LOG.command"
    exit 0
fi
if [ "$1" = "set-option" ] && [ "${2:-}" = "-p" ]; then
    printf '%s\\n' "$@" > "$KANBAN_TMUX_LOG.pane-setopt"
    exit 0
fi
printf '%s\\n' "$@" > "$KANBAN_TMUX_LOG"
printf '%s\\t%s\\n' '@9' '%9'
""",
            encoding="utf-8",
        )
        tmux.chmod(0o755)
        for name in ("codex", "claude", "grok", "cursor-agent"):
            agent = fake_bin / name
            agent.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            agent.chmod(0o755)
        env = self.env.copy()
        env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
        env["TMUX"] = "/tmp/fake-tmux,1,0"
        env["TMUX_PANE"] = "%7"
        env["KANBAN_TMUX_LOG"] = str(self.root / "tmux.log")

        result = self.run_entry(entry, "start", task_id, cwd=linked, env=env)
        self.assertIn(f"已启动: {task_id}", result.stdout)
        self.assertIn("Agent=grok", result.stdout)
        self.assertTrue((board / "working" / f"{task_id}.md").is_file())
        command = (self.root / "tmux.log.command").read_text(encoding="utf-8").rstrip("\n")
        self.assertIn(str(fake_bin / "grok"), command)
        self.assertIn(str(entry), command)
        self.assertIn(str(main / "AGENTS.md"), command)
        self.assertIn(str(main), (self.root / "tmux.log").read_text(encoding="utf-8"))
        self.assertNotIn("先运行 kanban rules", command)


if __name__ == "__main__":
    unittest.main()
