#!/usr/bin/env python3

"""Agent takeover metadata, prompt, session discovery, and old-container cleanup."""

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol

from kanban_probe import KanbanError
from onevoke_config import InstallPaths, agent_executable_name, language_text
from onevoke_fs import is_reparse_point


t = language_text
HERDR_WINDOW_RE = re.compile(r"^herdr:([^:\s]+:[^:\s]+):([^:\s]+:[^:\s]+)$")
TMUX_WINDOW_RE = re.compile(r"^(tmux|tmux-session):([^:\s]+):([^:\s]+):([^:\s]+)$")
POLL_INTERVAL = 0.1
SESSION_DISCOVERY_TIMEOUT = 10.0


class AgentSession(Protocol):
    agent: str
    reference: str


@dataclass(frozen=True)
class CleanupResult:
    cleaned: bool
    old_window: str
    channel: str
    container: str
    detail: str = ""


@dataclass(frozen=True)
class CleanupOperations:
    probe_herdr_pane: Callable[[str, str], object]
    herdr_tab_panes: Callable[[str, str], list[str]]
    validate_herdr_container: Callable[[str, str, str, dict], None]
    herdr_close_tab: Callable[[str, str], Optional[str]]
    herdr_agent_prompt: Callable[[str, str, str], None]
    herdr_wait_agent_exit: Callable[[str, str, str, AgentSession, float], bool]
    probe_tmux_pane: Callable[[str, str], object]
    validate_tmux_container: Callable[[str, str, str, str, str], None]
    tmux_close_window: Callable[[str, str], Optional[str]]
    tmux_send_agent_exit: Callable[[str, str, str], None]
    tmux_wait_agent_exit: Callable[[str, str, str, str, str, AgentSession, float], bool]
    tmux_window_exists: Callable[[str, str], bool]
    agent_exit_command: Callable[[str], str]


def start_agent_prompt(task_id: str, paths: InstallPaths, task_group: str = "") -> str:
    if paths.mode == "project":
        if paths.project_root is None:
            raise KanbanError(t(
                "项目安装路径缺少主 worktree",
                "project install paths are missing the main worktree",
            ))
        kanban_cmd = str(paths.bin_dir / "kanban")
        follow = f"遵守 {paths.project_root / 'AGENTS.md'}"
    else:
        kanban_cmd = "kanban"
        follow = "遵守目标项目 AGENTS.md"
    head = (
        f"执行 Kanban 任务 {task_id}. 先运行 {kanban_cmd} rules, 再运行 "
        f"{kanban_cmd} show {task_id}. 卡片已进入 working. {follow}, "
    )
    if task_group:
        return head + (
            "补充任务分支, 按看板规则的任务组流程完成任务、验证, 提交 push 并汇入组集成分支, "
            f"写好实施与验证后执行 {kanban_cmd} move {task_id} review 并退出; "
            "审核、集成和看板收尾由任务组主控负责, 不要自行审核或合回 develop."
        )
    return head + "补充任务分支, 并按项目规则完成任务、验证、审核、集成和看板收尾."


def resume_agent_prompt(task_id: str, message: str, paths: InstallPaths) -> str:
    if paths.mode == "project":
        if paths.project_root is None:
            raise KanbanError(t("项目安装路径缺少主 worktree", "project install paths are missing the main worktree"))
        kanban_cmd = str(paths.bin_dir / "kanban")
        agents_md = str(paths.project_root / "AGENTS.md")
    else:
        kanban_cmd = "kanban"
        agents_md = t("目标项目 AGENTS.md", "the target project's AGENTS.md")
    return (
        f"继续 Kanban 任务 {task_id}. 卡片已迁回 working. 先运行 {kanban_cmd} rules, 再运行 "
        f"{kanban_cmd} show {task_id} 重新核对卡片, 然后处理以下事项:\n\n{message}\n\n"
        f"遵守 {agents_md} 和看板规则; 处理完成后更新任务卡, 提交并 push, 再按规则迁移卡片状态."
    )


def read_task_message(args, command: str) -> str:
    if bool(args.message) == bool(args.message_file):
        raise KanbanError(t(
            f"{command} 必须且只能提供 --message 或 --message-file 之一",
            f"{command} requires exactly one of --message or --message-file",
        ))
    if args.message:
        message = args.message
    else:
        path = Path(args.message_file)
        if is_reparse_point(path):
            raise KanbanError(t(f"消息文件不得是符号链接/重解析点: {path}", f"Message file must not be a symlink/reparse point: {path}"))
        try:
            message = path.read_text(encoding="utf-8")
        except OSError as error:
            raise KanbanError(t(f"读取消息文件失败: {error}", f"Failed to read message file: {error}")) from error
    message = message.strip()
    if not message:
        raise KanbanError(t(f"{command} 消息不得为空", f"{command} message must not be empty"))
    return message


def takeover_agent_prompt(
    task_id: str,
    message: str,
    paths: InstallPaths,
    previous_agent: str,
) -> str:
    if paths.mode == "project":
        if paths.project_root is None:
            raise KanbanError(t(
                "项目安装路径缺少主 worktree",
                "project install paths are missing the main worktree",
            ))
        kanban_cmd = str(paths.bin_dir / "kanban")
        agents_md = str(paths.project_root / "AGENTS.md")
    else:
        kanban_cmd = "kanban"
        agents_md = t("目标项目 AGENTS.md", "the target project's AGENTS.md")
    return (
        f"接管 Kanban 任务 {task_id}. 原执行 Agent {previous_agent} 已停止; 本会话是全新会话, "
        "没有原会话上下文. "
        f"先运行 {kanban_cmd} rules 和 {kanban_cmd} show {task_id}, 再进入任务 worktree, "
        "用 git status、git log、任务分支及卡片的实施与验证重建进度; 不重复已完成提交. "
        f"然后处理以下事项:\n\n{message}\n\n遵守 {agents_md} 和看板规则; "
        "处理完成后更新任务卡, 提交并 push, 再按规则迁移卡片状态."
    )


def takeover_prompt_prefixes(task_id: str) -> tuple[str, str]:
    head = f"接管 Kanban 任务 {task_id}"
    return (
        f"{head}; full instructions are in the UTF-8 task file at ",
        f"{head}.",
    )


def _replace_unique_field(
    text: str, name: str, value: str, error_type: type[Exception]
) -> str:
    pattern = rf"(?m)^- {re.escape(name)}:.*$"
    if len(re.findall(pattern, text)) != 1:
        raise error_type(t(
            f"任务文档缺少唯一元数据字段: {name}",
            f"Task document must contain exactly one metadata field: {name}",
        ))
    return re.sub(pattern, lambda _match: f"- {name}: {value}".rstrip(), text, count=1)


def render_takeover_metadata(
    text: str,
    agent: str,
    session: str,
    window: str,
    error_type: type[Exception],
) -> str:
    for name, value in (("负责人", agent), ("会话", session)):
        text = _replace_unique_field(text, name, value, error_type)
    window_lines = re.findall(r"(?m)^- 窗口:.*$", text)
    if len(window_lines) > 1:
        raise error_type(t(
            "任务文档缺少唯一元数据字段: 窗口",
            "Task document must contain exactly one metadata field: 窗口",
        ))
    if window_lines:
        return _replace_unique_field(text, "窗口", window, error_type)
    return re.sub(
        r"(?m)^(- 会话:.*)$",
        lambda match: f"{match.group(1)}\n- 窗口: {window}".rstrip(),
        text, count=1,
    )


def render_session_metadata(
    text: str, session: str, error_type: type[Exception]
) -> str:
    return _replace_unique_field(text, "会话", session, error_type)


def discover_new_codex_session(
    task_id: str,
    previous_sessions: set[str],
    sessions_for_task: Callable[[str], tuple[str, ...]],
) -> str:
    deadline = time.monotonic() + SESSION_DISCOVERY_TIMEOUT
    while True:
        try:
            candidates = set(sessions_for_task(task_id))
        except KanbanError as resolve_error:
            error = resolve_error
        else:
            new_sessions = candidates - previous_sessions
            if len(new_sessions) == 1:
                return next(iter(new_sessions))
            if len(new_sessions) > 1:
                raise KanbanError(t(
                    f"本次启动发现多个新 Codex 会话: {len(new_sessions)}",
                    f"Multiple new Codex sessions appeared during launch: {len(new_sessions)}",
                ))
            error = KanbanError(t(
                "尚未发现本次新建的 Codex 会话",
                "The newly started Codex session has not appeared yet",
            ))
        if time.monotonic() >= deadline:
            raise error
        time.sleep(POLL_INTERVAL)


def _closed(old_window: str, channel: str, container: str) -> CleanupResult:
    return CleanupResult(True, old_window or "N/A", channel, container)


def _retained(old_window: str, detail: str) -> CleanupResult:
    return CleanupResult(False, old_window or "N/A", "", "", " ".join(detail.split()))


def _close_error(error: Optional[str]) -> None:
    if error:
        raise KanbanError(error)


def cleanup_takeover_container(
    old_window: str,
    old_session: AgentSession,
    new_window: str,
    timeout: float,
    operations: CleanupOperations,
) -> CleanupResult:
    """Best-effort cleanup after takeover; every unsafe or failed case is retained."""
    if os.name == "nt" or not old_window or old_window in ("foreground", "console"):
        return _closed("N/A", "N/A", "N/A")
    if old_window == new_window:
        return _closed("N/A", "N/A", "N/A")
    herdr_match = HERDR_WINDOW_RE.fullmatch(old_window)
    tmux_match = TMUX_WINDOW_RE.fullmatch(old_window)
    if not herdr_match and not tmux_match:
        return _retained(old_window, t("原窗口记录无效", "old window metadata is invalid"))
    try:
        if herdr_match:
            tab_id, pane_id = herdr_match.groups()
            herdr = shutil.which("herdr")
            if not herdr:
                raise KanbanError(t("herdr 不在 PATH", "herdr is not in PATH"))
            probe = operations.probe_herdr_pane(herdr, pane_id)
            pane = probe.pane
            if pane is None:
                panes = operations.herdr_tab_panes(herdr, tab_id)
                if panes:
                    raise KanbanError(t(
                        f"原 pane 已消失但 tab 仍含其他 pane: {','.join(panes)}",
                        f"The old pane is gone but its tab still has panes: {','.join(panes)}",
                    ))
                _close_error(operations.herdr_close_tab(herdr, tab_id))
                return _closed(old_window, "herdr", tab_id)
            operations.validate_herdr_container(herdr, tab_id, pane_id, pane)
            actual_agent = pane.get("agent")
            if actual_agent:
                identity = pane.get("agent_session")
                actual_session = identity.get("value") if isinstance(identity, dict) else None
                if actual_agent != old_session.agent or (
                    old_session.reference and actual_session != old_session.reference
                ):
                    raise KanbanError(t(
                        "原 pane 的 Agent 或会话身份不匹配",
                        "The old pane Agent or session identity does not match",
                    ))
                if pane.get("agent_status") not in ("idle", "done"):
                    raise KanbanError(t(
                        f"原 pane 状态不可遣散: {pane.get('agent_status') or 'N/A'}",
                        f"The old pane status cannot be dismissed: {pane.get('agent_status') or 'N/A'}",
                    ))
                operations.herdr_agent_prompt(
                    herdr, pane_id, operations.agent_exit_command(old_session.agent)
                )
                pane_exists = operations.herdr_wait_agent_exit(
                    herdr, tab_id, pane_id, old_session, timeout
                )
                if pane_exists:
                    _close_error(operations.herdr_close_tab(herdr, tab_id))
            else:
                _close_error(operations.herdr_close_tab(herdr, tab_id))
            return _closed(old_window, "herdr", tab_id)

        launcher, session_id, window_id, pane_id = tmux_match.groups()
        tmux = shutil.which("tmux")
        if not tmux:
            raise KanbanError(t("tmux 不在 PATH", "tmux is not in PATH"))
        probe = operations.probe_tmux_pane(tmux, pane_id)
        if probe.facts is None:
            if not operations.tmux_window_exists(tmux, window_id):
                return _closed(old_window, launcher, window_id)
            raise KanbanError(t(
                "原 pane 已消失但 window 仍存在",
                "The old pane is gone but its window still exists",
            ))
        facts = probe.facts
        operations.validate_tmux_container(
            tmux, pane_id, launcher, session_id, window_id
        )
        expected = Path(agent_executable_name(old_session.agent)).name
        if facts.dead == "1":
            _close_error(operations.tmux_close_window(tmux, window_id))
            return _closed(old_window, launcher, window_id)
        if facts.dead != "0" or facts.command != expected or (
            old_session.reference and facts.session_marker != old_session.reference
        ):
            raise KanbanError(t(
                "原 tmux pane 的 Agent 或会话身份不匹配",
                "The old tmux pane Agent or session identity does not match",
            ))
        if facts.in_mode != "0":
            raise KanbanError(t(
                "原 tmux pane 处于 copy-mode",
                "The old tmux pane is in copy mode",
            ))
        operations.tmux_send_agent_exit(
            tmux, pane_id, operations.agent_exit_command(old_session.agent)
        )
        window_exists = operations.tmux_wait_agent_exit(
            tmux, launcher, session_id, window_id, pane_id, old_session, timeout
        )
        if window_exists:
            _close_error(operations.tmux_close_window(tmux, window_id))
        return _closed(old_window, launcher, window_id)
    except (KanbanError, OSError, UnicodeError, ValueError) as error:
        return _retained(old_window, str(error))
