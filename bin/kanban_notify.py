#!/usr/bin/env python3

"""Notify target validation, stale-address lookup, and busy retry policy."""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from kanban_liveness import (
    HERDR_WINDOW_RE,
    TMUX_WINDOW_RE,
    TmuxPaneLocation,
    herdr_reverse_lookup,
    tmux_reverse_lookup,
)
from kanban_probe import KanbanError, probe_herdr_pane, probe_tmux_pane
from onevoke_config import agent_executable_name, language_text


t = language_text
NOTIFY_POLL_INTERVAL = 0.1


class AgentSession(Protocol):
    agent: str
    reference: str


@dataclass(frozen=True)
class NotifyTargetProbe:
    state: str
    detail: str
    pane: dict | None = None


@dataclass(frozen=True)
class DirectNotifyTarget:
    kind: str
    program: str
    pane_id: str
    window: str
    timeout: float


class NotifyBusyError(KanbanError):
    pass


def herdr_session_reference(pane: dict) -> str | None:
    identity = pane.get("agent_session")
    reference = identity.get("value") if isinstance(identity, dict) else None
    return reference if isinstance(reference, str) and reference else None


def _herdr_pane(herdr: str, pane_id: str) -> dict:
    probe = probe_herdr_pane(herdr, pane_id)
    if probe.pane is None:
        raise KanbanError(t(
            f"pane 不存在: {pane_id}: {probe.gone_detail}",
            f"pane does not exist: {pane_id}: {probe.gone_detail}",
        ))
    return probe.pane


def herdr_notify_target(herdr: str, pane_id: str, session: AgentSession) -> dict:
    pane = _herdr_pane(herdr, pane_id)
    actual_agent = pane.get("agent")
    if actual_agent != session.agent:
        raise KanbanError(t(
            f"Agent 不匹配: 卡片={session.agent}, pane={actual_agent or 'N/A'}",
            f"Agent mismatch: task={session.agent}, pane={actual_agent or 'N/A'}",
        ))
    status = pane.get("agent_status")
    if status not in ("idle", "done"):
        raise KanbanError(t(
            f"pane 状态不可投递: {pane_id}: {status or 'N/A'}",
            f"pane status does not accept delivery: {pane_id}: {status or 'N/A'}",
        ))
    actual_session = herdr_session_reference(pane)
    if actual_session != session.reference:
        raise KanbanError(t(
            f"会话不匹配: 卡片={session.reference}, pane={actual_session or 'N/A'}",
            f"Session mismatch: task={session.reference}, pane={actual_session or 'N/A'}",
        ))
    return pane


def herdr_notify_probe(
    herdr: str, pane_id: str, session: AgentSession
) -> NotifyTargetProbe:
    pane_probe = probe_herdr_pane(herdr, pane_id)
    if pane_probe.pane is None:
        return NotifyTargetProbe("stale", t(
            f"pane 不存在: {pane_id}: {pane_probe.gone_detail}",
            f"pane does not exist: {pane_id}: {pane_probe.gone_detail}",
        ))
    pane = pane_probe.pane
    actual_agent = pane.get("agent")
    if actual_agent != session.agent:
        return NotifyTargetProbe("stale", t(
            f"Agent 不匹配: 卡片={session.agent}, pane={actual_agent or 'N/A'}",
            f"Agent mismatch: task={session.agent}, pane={actual_agent or 'N/A'}",
        ))
    actual_session = herdr_session_reference(pane)
    if actual_session != session.reference:
        return NotifyTargetProbe("stale", t(
            f"会话不匹配: 卡片={session.reference}, pane={actual_session or 'N/A'}",
            f"Session mismatch: task={session.reference}, pane={actual_session or 'N/A'}",
        ))
    status = pane.get("agent_status")
    if status not in ("idle", "done"):
        return NotifyTargetProbe("busy", t(
            f"pane 状态不可投递: {pane_id}: {status or 'N/A'}",
            f"pane status does not accept delivery: {pane_id}: {status or 'N/A'}",
        ), pane)
    return NotifyTargetProbe("ready", "", pane)


def herdr_explicit_target(herdr: str, pane_id: str) -> tuple[str, str]:
    pane = _herdr_pane(herdr, pane_id)
    tab_id = pane.get("tab_id")
    if not isinstance(tab_id, str) or not tab_id or "\x00" in tab_id:
        raise KanbanError(t(
            f"pane 缺少可用 tab id: {pane_id}",
            f"pane has no usable tab id: {pane_id}",
        ))
    return tab_id, pane_id


def tmux_notify_target(tmux: str, pane_id: str, session: AgentSession) -> None:
    pane_probe = probe_tmux_pane(tmux, pane_id)
    if pane_probe.facts is None:
        raise KanbanError(t(
            f"tmux pane 不存在: {pane_id}: {pane_probe.gone_detail}",
            f"tmux pane does not exist: {pane_id}: {pane_probe.gone_detail}",
        ))
    facts = pane_probe.facts
    if facts.dead != "0":
        raise KanbanError(t(f"tmux pane 已退出: {pane_id}", f"tmux pane is dead: {pane_id}"))
    if facts.in_mode != "0":
        raise KanbanError(t(
            f"tmux pane 处于 copy-mode: {pane_id}",
            f"tmux pane is in copy mode: {pane_id}",
        ))
    expected = Path(agent_executable_name(session.agent)).name
    if facts.command != expected:
        raise KanbanError(t(
            f"tmux 前台进程不匹配: 期望={expected}, 实际={facts.command or 'N/A'}",
            f"tmux foreground process mismatch: expected={expected}, actual={facts.command or 'N/A'}",
        ))
    if not facts.session_marker:
        raise KanbanError(t(
            f"tmux pane 缺少会话标记: {pane_id}",
            f"tmux pane has no session marker: {pane_id}",
        ))
    if facts.session_marker != session.reference:
        raise KanbanError(t(
            f"tmux 会话不匹配: 卡片={session.reference}, pane={facts.session_marker}",
            f"tmux session mismatch: task={session.reference}, pane={facts.session_marker}",
        ))


def tmux_notify_probe(
    tmux: str, pane_id: str, session: AgentSession
) -> NotifyTargetProbe:
    pane_probe = probe_tmux_pane(tmux, pane_id)
    if pane_probe.facts is None:
        return NotifyTargetProbe("stale", t(
            f"tmux pane 不存在: {pane_id}: {pane_probe.gone_detail}",
            f"tmux pane does not exist: {pane_id}: {pane_probe.gone_detail}",
        ))
    facts = pane_probe.facts
    if facts.dead != "0":
        return NotifyTargetProbe("stale", t(
            f"tmux pane 已退出: {pane_id}", f"tmux pane is dead: {pane_id}"
        ))
    expected = Path(agent_executable_name(session.agent)).name
    if facts.command != expected:
        return NotifyTargetProbe("stale", t(
            f"tmux 前台进程不匹配: 期望={expected}, 实际={facts.command or 'N/A'}",
            f"tmux foreground process mismatch: expected={expected}, actual={facts.command or 'N/A'}",
        ))
    if not facts.session_marker:
        return NotifyTargetProbe("fallback", t(
            f"tmux pane 缺少会话标记: {pane_id}",
            f"tmux pane has no session marker: {pane_id}",
        ))
    if facts.session_marker != session.reference:
        return NotifyTargetProbe("stale", t(
            f"tmux 会话不匹配: 卡片={session.reference}, pane={facts.session_marker}",
            f"tmux session mismatch: task={session.reference}, pane={facts.session_marker}",
        ))
    if facts.in_mode != "0":
        return NotifyTargetProbe("busy", t(
            f"tmux pane 处于 copy-mode: {pane_id}",
            f"tmux pane is in copy mode: {pane_id}",
        ))
    return NotifyTargetProbe("ready", "")


def render_tmux_window(launcher: str, location: TmuxPaneLocation) -> str:
    container = location.session_id if launcher == "tmux" else location.session_name
    return f"{launcher}:{container}:{location.window_id}:{location.pane_id}"


def wait_for_notify_target(
    probe: Callable[[], NotifyTargetProbe], timeout: float
) -> tuple[NotifyTargetProbe, float]:
    deadline = time.monotonic() + timeout
    while True:
        result = probe()
        if result.state != "busy":
            return result, max(0.0, deadline - time.monotonic())
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise NotifyBusyError(t(
                f"目标 Agent 忙, 未投递: {result.detail}",
                f"Target Agent is busy; nothing was delivered: {result.detail}",
            ))
        time.sleep(min(NOTIFY_POLL_INTERVAL, remaining))


def _ready(probe: NotifyTargetProbe) -> None:
    if probe.state != "ready":
        raise KanbanError(probe.detail)


def _stale_lookup(detail: str, lookup: Callable[[], DirectNotifyTarget]) -> DirectNotifyTarget:
    try:
        return lookup()
    except NotifyBusyError:
        raise
    except (KanbanError, OSError, UnicodeError, ValueError) as error:
        raise KanbanError(t(
            f"地址过期: {detail}; 反查={error}",
            f"stale address: {detail}; lookup={error}",
        )) from error


def resolve_notify_target(
    window: str,
    pane_override: str | None,
    session: AgentSession,
    timeout: float,
) -> DirectNotifyTarget:
    """Resolve and fully validate a direct target before any payload is created."""
    herdr_match = HERDR_WINDOW_RE.fullmatch(window)
    tmux_match = TMUX_WINDOW_RE.fullmatch(window)
    if pane_override:
        herdr = shutil.which("herdr")
        if not herdr:
            raise KanbanError(t("herdr 不在 PATH", "herdr is not in PATH"))
        tab_id, pane_id = herdr_explicit_target(herdr, pane_override)
        probe, remaining = wait_for_notify_target(
            lambda: herdr_notify_probe(herdr, pane_id, session), timeout
        )
        _ready(probe)
        return DirectNotifyTarget("herdr", herdr, pane_id, f"herdr:{tab_id}:{pane_id}", remaining)
    if herdr_match:
        _tab_id, pane_id = herdr_match.groups()
        herdr = shutil.which("herdr")
        if not herdr:
            raise KanbanError(t("herdr 不在 PATH", "herdr is not in PATH"))
        probe, remaining = wait_for_notify_target(
            lambda: herdr_notify_probe(herdr, pane_id, session), timeout
        )
        if probe.state != "stale":
            _ready(probe)
            return DirectNotifyTarget("herdr", herdr, pane_id, "", remaining)

        def lookup_herdr() -> DirectNotifyTarget:
            tab_id, discovered_pane = herdr_reverse_lookup(herdr, session)
            discovered, final_timeout = wait_for_notify_target(
                lambda: herdr_notify_probe(herdr, discovered_pane, session),
                remaining,
            )
            _ready(discovered)
            return DirectNotifyTarget(
                "herdr",
                herdr,
                discovered_pane,
                f"herdr:{tab_id}:{discovered_pane}",
                final_timeout,
            )

        return _stale_lookup(probe.detail, lookup_herdr)
    if tmux_match:
        launcher, _session, _window, pane_id = tmux_match.groups()
        tmux = shutil.which("tmux")
        if not tmux:
            raise KanbanError(t("tmux 不在 PATH", "tmux is not in PATH"))
        probe, remaining = wait_for_notify_target(
            lambda: tmux_notify_probe(tmux, pane_id, session), timeout
        )
        if probe.state != "stale":
            _ready(probe)
            return DirectNotifyTarget("tmux", tmux, pane_id, "", remaining)

        def lookup_tmux() -> DirectNotifyTarget:
            location = tmux_reverse_lookup(tmux, session)
            discovered, final_timeout = wait_for_notify_target(
                lambda: tmux_notify_probe(tmux, location.pane_id, session),
                remaining,
            )
            _ready(discovered)
            return DirectNotifyTarget(
                "tmux",
                tmux,
                location.pane_id,
                render_tmux_window(launcher, location),
                final_timeout,
            )

        return _stale_lookup(probe.detail, lookup_tmux)
    if not window:
        herdr = shutil.which("herdr")
        if not herdr:
            raise KanbanError(t(
                "herdr 不在 PATH, 无法反查 pane",
                "herdr is not in PATH; cannot look up a pane",
            ))
        tab_id, pane_id = herdr_reverse_lookup(herdr, session)
        probe, remaining = wait_for_notify_target(
            lambda: herdr_notify_probe(herdr, pane_id, session), timeout
        )
        _ready(probe)
        return DirectNotifyTarget(
            "herdr", herdr, pane_id, f"herdr:{tab_id}:{pane_id}", remaining
        )
    raise KanbanError(t(
        f"无可用直投地址: {window or 'N/A'}",
        f"No direct notification address: {window or 'N/A'}",
    ))
