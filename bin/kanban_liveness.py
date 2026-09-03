#!/usr/bin/env python3

"""Read-only execution-Agent liveness policy and shared pane reverse lookup."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from kanban_probe import KanbanError, probe_herdr_pane, probe_tmux_pane
from onevoke_config import EXECUTION_AGENTS, agent_executable_name, language_text


t = language_text
LIVENESS_ALIVE = "alive"
LIVENESS_STOPPED = "stopped"
LIVENESS_DRIFTED = "drifted"
LIVENESS_UNKNOWN = "unknown"
SESSION_REFERENCE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
HERDR_WINDOW_RE = re.compile(r"^herdr:([^:\s]+:[^:\s]+):([^:\s]+:[^:\s]+)$")
TMUX_WINDOW_RE = re.compile(r"^(tmux|tmux-session):([^:\s]+):([^:\s]+):([^:\s]+)$")


class TaskEntry(Protocol):
    task_id: str


@dataclass(frozen=True)
class TaskSession:
    agent: str
    reference: str


@dataclass(frozen=True)
class TmuxPaneLocation:
    session_id: str
    session_name: str
    window_id: str
    pane_id: str


@dataclass(frozen=True)
class LivenessReport:
    task_id: str
    agent: str
    status: str
    channel: str
    container: str
    detail: str
    new_window: str = ""


def _metadata(text: str, name: str) -> str:
    match = re.search(rf"(?m)^- {re.escape(name)}:[ \t]*(.*?)[ \t]*$", text)
    return match.group(1) if match else ""


def _session_from(text: str) -> TaskSession | None:
    value = _metadata(text, "会话")
    if not value:
        return None
    parts = value.split()
    agent = parts[0]
    reference = parts[1] if len(parts) > 1 else ""
    if (
        agent not in EXECUTION_AGENTS
        or len(parts) > 2
        or (reference and not SESSION_REFERENCE_RE.fullmatch(reference))
    ):
        return None
    return TaskSession(agent, reference)


def _capture(program: str, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [program, *arguments], text=True, capture_output=True, check=False
    )


def _herdr_result(result: subprocess.CompletedProcess) -> dict:
    if result.returncode != 0:
        raise KanbanError(result.stderr.strip() or f"exit {result.returncode}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise KanbanError(str(error)) from error
    data = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise KanbanError(t("herdr 响应缺少 result", "herdr response is missing result"))
    return data


def _public_id(value: object) -> str | None:
    return value if isinstance(value, str) and value and "\x00" not in value else None


def herdr_reverse_lookup(herdr: str, session: TaskSession) -> tuple[str, str]:
    data = _herdr_result(_capture(herdr, "pane", "list"))
    panes = data.get("panes")
    if not isinstance(panes, list):
        raise KanbanError(t("herdr pane list 响应缺少 panes", "herdr pane list response has no panes"))
    matches: list[tuple[str, str]] = []
    for pane in panes:
        if not isinstance(pane, dict):
            continue
        identity = pane.get("agent_session")
        reference = identity.get("value") if isinstance(identity, dict) else None
        if pane.get("agent") != session.agent or reference != session.reference:
            continue
        tab_id = _public_id(pane.get("tab_id"))
        pane_id = _public_id(pane.get("pane_id"))
        if tab_id and pane_id:
            matches.append((tab_id, pane_id))
    if not matches:
        raise KanbanError(t(
            f"herdr 会话反查无匹配: {session.reference}",
            f"herdr session lookup found no match: {session.reference}",
        ))
    if len(matches) != 1:
        raise KanbanError(t(
            f"herdr 会话反查匹配不唯一: {session.reference}: {len(matches)} 个 pane",
            f"herdr session lookup is ambiguous: {session.reference}: {len(matches)} panes",
        ))
    return matches[0]


def tmux_reverse_lookup(tmux: str, session: TaskSession) -> TmuxPaneLocation:
    result = _capture(
        tmux,
        "list-panes",
        "-a",
        "-F",
        "#{pane_id}\t#{session_id}\t#{session_name}\t#{window_id}\t#{pane_current_command}\t#{pane_dead}\t#{@onevoke_session}",
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise KanbanError(result.stderr.strip() or t("输出为空", "empty output"))
    expected = Path(agent_executable_name(session.agent)).name
    matches: list[TmuxPaneLocation] = []
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 7:
            raise KanbanError(t("tmux 会话反查输出无效", "tmux session lookup returned invalid output"))
        pane_id, session_id, session_name, window_id, command, dead, marker = fields
        if marker == session.reference and dead == "0" and command == expected:
            matches.append(TmuxPaneLocation(session_id, session_name, window_id, pane_id))
    if not matches:
        raise KanbanError(t(
            f"tmux 会话反查无匹配: {session.reference}: 0 个 pane",
            f"tmux session lookup found no match: {session.reference}: 0 panes",
        ))
    if len(matches) != 1:
        raise KanbanError(t(
            f"tmux 会话反查匹配不唯一: {session.reference}: {len(matches)} 个 pane",
            f"tmux session lookup is ambiguous: {session.reference}: {len(matches)} panes",
        ))
    return matches[0]


def _report(
    entry: TaskEntry,
    session: TaskSession | None,
    status: str,
    channel: str,
    container: str,
    detail: str,
    new_window: str = "",
) -> LivenessReport:
    one_line_detail = " ".join(detail.split())
    return LivenessReport(
        entry.task_id,
        session.agent if session else "N/A",
        status,
        channel,
        container or "N/A",
        one_line_detail,
        new_window,
    )


def _stale_report(
    entry: TaskEntry,
    session: TaskSession,
    channel: str,
    container: str,
    detail: str,
    program: str,
    launcher: str,
    allow_reverse_lookup: bool,
) -> LivenessReport:
    if not allow_reverse_lookup or not session.reference:
        if session.agent == "codex" and not session.reference:
            detail += t(
                "; codex 卡直接用 notify, 命令内自动反查",
                "; use notify directly for this Codex task; the command performs lookup",
            )
        return _report(entry, session, LIVENESS_STOPPED, channel, container, detail)
    try:
        if channel == "herdr":
            tab_id, pane_id = herdr_reverse_lookup(program, session)
            new_window = f"herdr:{tab_id}:{pane_id}"
        else:
            location = tmux_reverse_lookup(program, session)
            tmux_container = location.session_id if launcher == "tmux" else location.session_name
            new_window = f"{launcher}:{tmux_container}:{location.window_id}:{location.pane_id}"
    except (KanbanError, OSError, UnicodeError, ValueError):
        return _report(entry, session, LIVENESS_STOPPED, channel, container, detail)
    return _report(entry, session, LIVENESS_DRIFTED, channel, container, detail, new_window)


def _probe_task_liveness(
    entry: TaskEntry, text: str, *, allow_reverse_lookup: bool = True
) -> LivenessReport:
    session = _session_from(text)
    window = _metadata(text, "窗口")
    if session is None:
        return _report(entry, None, LIVENESS_UNKNOWN, "unknown", window, t(
            "缺少或无法解析会话记录", "missing or invalid session metadata"
        ))
    if os.name == "nt":
        return _report(entry, session, LIVENESS_UNKNOWN, "unknown", window, t(
            "Windows 当前不探测 Agent 存活", "Agent liveness is not probed on Windows"
        ))
    if window in ("foreground", "console"):
        return _report(entry, session, LIVENESS_UNKNOWN, window, window, t(
            "该 launcher 没有可探测地址", "this launcher has no probeable address"
        ))
    herdr_match = HERDR_WINDOW_RE.fullmatch(window)
    tmux_match = TMUX_WINDOW_RE.fullmatch(window)
    if not herdr_match and not tmux_match:
        return _report(entry, session, LIVENESS_UNKNOWN, "unknown", window, t(
            "窗口记录为空或无效", "window metadata is empty or invalid"
        ))
    if herdr_match:
        tab_id, pane_id = herdr_match.groups()
        herdr = shutil.which("herdr")
        if not herdr:
            return _report(entry, session, LIVENESS_UNKNOWN, "herdr", tab_id, t(
                "herdr 不在 PATH", "herdr is not in PATH"
            ))
        try:
            pane_probe = probe_herdr_pane(herdr, pane_id)
        except (KanbanError, OSError, UnicodeError, ValueError) as error:
            return _report(entry, session, LIVENESS_UNKNOWN, "herdr", tab_id, str(error))
        if pane_probe.pane is None:
            return _stale_report(entry, session, "herdr", tab_id, t(
                f"pane 不存在: {pane_id}", f"pane does not exist: {pane_id}"
            ), herdr, "herdr", allow_reverse_lookup)
        pane = pane_probe.pane
        actual_agent = pane.get("agent")
        if actual_agent != session.agent:
            return _stale_report(entry, session, "herdr", tab_id, t(
                f"Agent 不匹配: 期望={session.agent}, 实际={actual_agent or 'N/A'}",
                f"Agent mismatch: expected={session.agent}, actual={actual_agent or 'N/A'}",
            ), herdr, "herdr", allow_reverse_lookup)
        identity = pane.get("agent_session")
        actual_session = identity.get("value") if isinstance(identity, dict) else None
        if session.reference and actual_session and actual_session != session.reference:
            return _stale_report(entry, session, "herdr", tab_id, t(
                "会话身份不匹配", "session identity mismatch"
            ), herdr, "herdr", allow_reverse_lookup)
        status = pane.get("agent_status")
        if status not in ("idle", "working", "blocked", "done"):
            return _report(entry, session, LIVENESS_UNKNOWN, "herdr", tab_id, t(
                f"Agent 状态不可判定: {status or 'N/A'}",
                f"Agent status cannot be classified: {status or 'N/A'}",
            ))
        if session.reference and not actual_session:
            return _report(entry, session, LIVENESS_ALIVE, "herdr", tab_id, t(
                "会话身份未上报, 直投不可用, notify 将走恢复通道; 重开容器可重新上报",
                "session identity was not reported; direct delivery is unavailable and notify will resume; reopen the container to report again",
            ))
        return _report(entry, session, LIVENESS_ALIVE, "herdr", tab_id, t(
            f"Agent 状态={status}", f"Agent status={status}"
        ))

    launcher, tmux_container, window_id, pane_id = tmux_match.groups()
    tmux = shutil.which("tmux")
    container = f"{tmux_container}:{window_id}"
    if not tmux:
        return _report(entry, session, LIVENESS_UNKNOWN, launcher, container, t(
            "tmux 不在 PATH", "tmux is not in PATH"
        ))
    try:
        pane_probe = probe_tmux_pane(tmux, pane_id)
    except (KanbanError, OSError, UnicodeError, ValueError) as error:
        return _report(entry, session, LIVENESS_UNKNOWN, launcher, container, str(error))
    if pane_probe.facts is None:
        return _stale_report(entry, session, launcher, container, t(
            f"pane 不存在: {pane_id}", f"pane does not exist: {pane_id}"
        ), tmux, launcher, allow_reverse_lookup)
    facts = pane_probe.facts
    expected = Path(agent_executable_name(session.agent)).name
    if facts.dead != "0" or facts.command != expected:
        detail = t(
            f"pane 已退出或前台进程不匹配: 期望={expected}, 实际={facts.command or 'N/A'}",
            f"pane is dead or foreground process mismatches: expected={expected}, actual={facts.command or 'N/A'}",
        )
        return _stale_report(
            entry, session, launcher, container, detail, tmux, launcher, allow_reverse_lookup
        )
    if not facts.session_marker:
        return _report(entry, session, LIVENESS_UNKNOWN, launcher, container, t(
            "tmux pane 缺少会话标记", "tmux pane has no session marker"
        ))
    if session.reference and facts.session_marker != session.reference:
        return _stale_report(entry, session, launcher, container, t(
            "tmux 会话标记不匹配", "tmux session marker mismatch"
        ), tmux, launcher, allow_reverse_lookup)
    detail = t("Agent 可达", "Agent is reachable")
    if facts.in_mode != "0":
        detail = t(
            "Agent 存活但 pane 处于 copy-mode, 暂不可直投",
            "Agent is alive but the pane is in copy mode and cannot receive direct delivery",
        )
    return _report(entry, session, LIVENESS_ALIVE, launcher, container, detail)


def probe_task_liveness(
    entry: TaskEntry, text: str, *, allow_reverse_lookup: bool = True
) -> LivenessReport:
    """Classify one task without mutating it; all probe failures degrade to unknown."""
    try:
        return _probe_task_liveness(
            entry, text, allow_reverse_lookup=allow_reverse_lookup
        )
    except (KanbanError, OSError, UnicodeError, ValueError) as error:
        session = _session_from(text)
        return _report(
            entry,
            session,
            LIVENESS_UNKNOWN,
            "unknown",
            _metadata(text, "窗口"),
            str(error),
        )
