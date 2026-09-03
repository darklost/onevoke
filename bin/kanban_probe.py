#!/usr/bin/env python3

"""Terminal pane fact collection shared by Kanban delivery and liveness policy."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Optional

from onevoke_config import language_text


t = language_text
TMUX_GONE_MARKERS = ("can't find", "no server running", "no sessions")
PANE_SESSION_OPTION = "@onevoke_session"
TMUX_OPTION_MISSING_DETAILS = (
    f"invalid option: {PANE_SESSION_OPTION}",
    f"unknown option: {PANE_SESSION_OPTION}",
)


class KanbanError(Exception):
    pass


@dataclass(frozen=True)
class TmuxPaneFacts:
    command: str
    in_mode: str
    dead: str
    session_marker: str


@dataclass(frozen=True)
class TmuxPaneProbe:
    facts: Optional[TmuxPaneFacts]
    gone_detail: str = ""


@dataclass(frozen=True)
class TmuxContainerFacts:
    session_id: str
    session_name: str
    window_id: str
    pane_count: str


@dataclass(frozen=True)
class HerdrPaneProbe:
    pane: Optional[dict]
    gone_detail: str = ""


def _capture(program: str, *arguments: str, timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [program, *arguments], text=True, capture_output=True, check=False, timeout=timeout
        )
    except (subprocess.TimeoutExpired, ValueError) as error:
        raise KanbanError(t(
            f"herdr 调用失败: {error}", f"herdr invocation failed: {error}"
        )) from error


def _failure_detail(result: subprocess.CompletedProcess) -> str:
    return result.stderr.strip() or f"exit {result.returncode}"


def _herdr_error_code(result: subprocess.CompletedProcess) -> Optional[str]:
    for output in (result.stderr, result.stdout):
        try:
            payload = json.loads(output)
        except (TypeError, json.JSONDecodeError):
            continue
        error = payload.get("error") if isinstance(payload, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        if isinstance(code, str):
            return code
    return None


def probe_herdr_pane(
    herdr: str, pane_id: str, *, timeout: Optional[float] = None
) -> HerdrPaneProbe:
    result = _capture(herdr, "pane", "get", pane_id, timeout=timeout)
    detail = _failure_detail(result)
    if result.returncode != 0:
        if _herdr_error_code(result) == "pane_not_found":
            return HerdrPaneProbe(None, detail)
        raise KanbanError(t(
            f"pane 不存在: {pane_id}: {detail}",
            f"pane does not exist: {pane_id}: {detail}",
        ))
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise KanbanError(t(
            f"herdr pane get 失败: 响应不是 JSON: {error}",
            f"herdr pane get failed: response is not JSON: {error}",
        )) from error
    data = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        message = "响应不是 JSON object" if not isinstance(payload, dict) else "响应缺少 result"
        english = "response is not a JSON object" if not isinstance(payload, dict) else "response is missing result"
        raise KanbanError(t(f"herdr pane get 失败: {message}", f"herdr pane get failed: {english}"))
    pane = data.get("pane")
    actual_id = pane.get("pane_id") if isinstance(pane, dict) else None
    if not isinstance(pane, dict) or actual_id != pane_id or not actual_id or "\x00" in actual_id:
        raise KanbanError(t(f"pane 不存在: {pane_id}", f"pane does not exist: {pane_id}"))
    return HerdrPaneProbe(pane)


def herdr_probe_pane(
    herdr: str, pane_id: str, *, timeout: Optional[float] = None
) -> Optional[dict]:
    return probe_herdr_pane(herdr, pane_id, timeout=timeout).pane


def probe_tmux_pane(tmux: str, pane_id: str) -> TmuxPaneProbe:
    result = subprocess.run(
        [
            tmux, "display-message", "-p", "-t", pane_id,
            "#{pane_current_command}\t#{pane_in_mode}\t#{pane_dead}",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    detail = result.stderr.strip()
    if result.returncode != 0:
        if any(marker in detail.lower() for marker in TMUX_GONE_MARKERS):
            return TmuxPaneProbe(None, detail)
        raise KanbanError(t(
            f"tmux pane 不存在: {pane_id}: {detail or f'exit {result.returncode}'}",
            f"tmux pane does not exist: {pane_id}: {detail or f'exit {result.returncode}'}",
        ))
    fields = result.stdout.strip().split("\t")
    if len(fields) != 3:
        raise KanbanError(t("tmux pane 探查响应无效", "tmux pane probe returned an invalid response"))
    identity = subprocess.run(
        [tmux, "show-options", "-p", "-v", "-t", pane_id, PANE_SESSION_OPTION],
        text=True,
        capture_output=True,
        check=False,
    )
    identity_detail = identity.stderr.strip()
    if identity.returncode != 0:
        lowered = identity_detail.lower()
        if any(marker in lowered for marker in TMUX_GONE_MARKERS):
            return TmuxPaneProbe(None, identity_detail)
        if identity_detail and lowered not in TMUX_OPTION_MISSING_DETAILS:
            raise KanbanError(t(
                f"tmux pane 探查失败: {identity_detail}",
                f"Failed to probe the tmux pane: {identity_detail}",
            ))
    marker = identity.stdout.strip() if identity.returncode == 0 else ""
    return TmuxPaneProbe(TmuxPaneFacts(*fields, marker))


def tmux_pane_facts(tmux: str, pane_id: str) -> Optional[TmuxPaneFacts]:
    return probe_tmux_pane(tmux, pane_id).facts


def probe_tmux_container(tmux: str, pane_id: str) -> TmuxContainerFacts:
    result = subprocess.run(
        [
            tmux, "display-message", "-p", "-t", pane_id,
            "#{session_id}\t#{session_name}\t#{window_id}\t#{window_panes}",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise KanbanError(t(
            f"tmux pane 容器探查失败: {detail}",
            f"Failed to probe the tmux pane container: {detail}",
        ))
    fields = result.stdout.strip().split("\t")
    if len(fields) != 4:
        raise KanbanError(t(
            "tmux pane 容器探查响应无效",
            "tmux pane container probe returned an invalid response",
        ))
    return TmuxContainerFacts(*fields)
