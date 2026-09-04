#!/usr/bin/env python3

"""Window metadata writeback helpers for Kanban Agent launches."""

import re
from datetime import datetime
from typing import Callable, Optional

from onevoke_config import language_text


t = language_text
SESSION_FIELD = "会话"
WINDOW_FIELD = "窗口"


def render_start_metadata(
    text: str, agent: str, session: str, window: str, error_type: type[Exception]
) -> str:
    for name, value in (
        ("负责人", agent),
        ("开始时间", datetime.now().strftime("%Y-%m-%d %H:%M")),
    ):
        pattern = rf"(?m)^- {name}:\s*$"
        if len(re.findall(pattern, text)) != 1:
            raise error_type(t(
                f"任务文档缺少唯一元数据字段: {name}",
                f"Task document must contain exactly one metadata field: {name}",
            ))
        text = re.sub(pattern, f"- {name}: {value}", text, count=1)
    session_lines = re.findall(rf"(?m)^- {SESSION_FIELD}:.*$", text)
    if len(session_lines) > 1:
        raise error_type(t(
            f"任务文档缺少唯一元数据字段: {SESSION_FIELD}",
            f"Task document must contain exactly one metadata field: {SESSION_FIELD}",
        ))
    rendered = f"- {SESSION_FIELD}: {session}"
    if session_lines:
        text = re.sub(rf"(?m)^- {SESSION_FIELD}:.*$", lambda _match: rendered, text, count=1)
    else:
        text = re.sub(
            r"(?m)^(- 负责人: .*)$", lambda match: f"{match.group(1)}\n{rendered}",
            text, count=1,
        )
    window_lines = re.findall(rf"(?m)^- {WINDOW_FIELD}:.*$", text)
    if len(window_lines) > 1:
        raise error_type(t(
            f"任务文档缺少唯一元数据字段: {WINDOW_FIELD}",
            f"Task document must contain exactly one metadata field: {WINDOW_FIELD}",
        ))
    if window_lines:
        text = re.sub(rf"(?m)^- {WINDOW_FIELD}:.*\n?", "", text, count=1)
    rendered_window = f"- {WINDOW_FIELD}: {window}".rstrip()
    return re.sub(
        rf"(?m)^(- {SESSION_FIELD}:.*)$",
        lambda match: f"{match.group(1)}\n{rendered_window}",
        text, count=1,
    )


def render_window_metadata(
    text: str, value: str, error_type: type[Exception]
) -> str:
    lines = re.findall(rf"(?m)^- {WINDOW_FIELD}:.*$", text)
    if len(lines) > 1:
        raise error_type(t(
            f"任务文档缺少唯一元数据字段: {WINDOW_FIELD}",
            f"Task document must contain exactly one metadata field: {WINDOW_FIELD}",
        ))
    if not lines:
        session_lines = re.findall(rf"(?m)^- {SESSION_FIELD}:.*$", text)
        if len(session_lines) != 1:
            raise error_type(t(
                f"任务文档缺少唯一元数据字段: {SESSION_FIELD}",
                f"Task document must contain exactly one metadata field: {SESSION_FIELD}",
            ))
        return re.sub(
            rf"(?m)^(- {SESSION_FIELD}:.*)$",
            lambda match: f"{match.group(1)}\n- {WINDOW_FIELD}: {value}",
            text,
            count=1,
        )
    return re.sub(
        rf"(?m)^- {WINDOW_FIELD}:.*$",
        lambda _match: f"- {WINDOW_FIELD}: {value}",
        text,
        count=1,
    )


def create_location_recorder(
    plan,
    entry,
    read_document: Callable,
    write_text_atomic: Callable,
    window_metadata: Callable,
) -> Callable:
    """Build a callback that rereads the task before recording a new container."""
    def record(outcome) -> None:
        if plan.launcher == "herdr":
            location = f"herdr:{outcome.tab}:{outcome.pane}"
        else:
            location = f"{plan.launcher}:{plan.session}:{outcome.window}:{outcome.pane}"
        current = read_document(entry)
        write_text_atomic(entry.document, window_metadata(current, location), entry=entry)

    return record


def restore_window_text(
    entry,
    text: str,
    write_text_atomic: Callable,
    caught_errors: tuple[type[BaseException], ...],
) -> Optional[BaseException]:
    try:
        write_text_atomic(entry.document, text, entry=entry)
    except caught_errors as error:
        return error
    return None


def resume_failure_message(
    primary: BaseException,
    cleanup_error: Optional[BaseException | str],
    rollback_error: Optional[BaseException],
) -> Optional[str]:
    details = []
    if cleanup_error is not None:
        details.append(t(f"清理={cleanup_error}", f"cleanup={cleanup_error}"))
    if rollback_error is not None:
        details.append(t(f"窗口回滚={rollback_error}", f"window rollback={rollback_error}"))
    return f"{primary}; " + "; ".join(details) if details else None
