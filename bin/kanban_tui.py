#!/usr/bin/env python3

import curses
import json
import locale
import os
import sys
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Optional


ACTIVE_STATES = ("backlog", "todo", "working", "done")
ALL_STATES = ACTIVE_STATES + ("archived", "trash")
CARD_HEIGHT = 5
MIN_COLUMN_WIDTH = 20
MIN_BOARD_HEIGHT = 9

FANCY_GLYPHS = {"vbar": "│", "bar": "▎", "hbar": "─", "left": "‹", "right": "›"}
ASCII_GLYPHS = {"vbar": "|", "bar": ">", "hbar": "-", "left": "<", "right": ">"}

THEMES = ("auto", "light", "dark")

THEME_PALETTES = {
    "dark": {
        "text": curses.COLOR_WHITE,
        "backlog": curses.COLOR_WHITE,
        "todo": curses.COLOR_YELLOW,
        "working": curses.COLOR_CYAN,
        "done": curses.COLOR_GREEN,
        "archived": curses.COLOR_BLUE,
        "trash": curses.COLOR_RED,
        "accent": curses.COLOR_MAGENTA,
    },
    "light": {
        "text": curses.COLOR_BLACK,
        "backlog": curses.COLOR_BLACK,
        "todo": curses.COLOR_YELLOW,
        "working": curses.COLOR_BLUE,
        "done": curses.COLOR_GREEN,
        "archived": curses.COLOR_MAGENTA,
        "trash": curses.COLOR_RED,
        "accent": curses.COLOR_MAGENTA,
    },
}
THEME_BACKGROUNDS = {"light": curses.COLOR_WHITE, "dark": curses.COLOR_BLACK}


class KanbanTuiError(Exception):
    pass


def display_width(text: str) -> int:
    return sum(
        0
        if unicodedata.combining(char)
        else 2
        if unicodedata.east_asian_width(char) in "WF"
        else 1
        for char in text
    )


def printable_text(text: str) -> str:
    return "".join(
        char
        if char == "\n" or unicodedata.category(char) not in {"Cc", "Cf"}
        else " "
        for char in text
    )


def clip_text(text: str, width: int) -> str:
    if width <= 0:
        return ""
    normalized = printable_text(text.replace("\t", "    ").replace("\n", " "))
    if display_width(normalized) <= width:
        return normalized
    suffix = "..." if width >= 4 else "." * width
    available = width - display_width(suffix)
    result = []
    used = 0
    for char in normalized:
        char_width = (
            0
            if unicodedata.combining(char)
            else 2
            if unicodedata.east_asian_width(char) in "WF"
            else 1
        )
        if used + char_width > available:
            break
        result.append(char)
        used += char_width
    return "".join(result) + suffix


def pad_text(text: str, width: int) -> str:
    clipped = clip_text(text, width)
    return clipped + " " * max(0, width - display_width(clipped))


def wrap_text(text: str, width: int) -> list[str]:
    if width <= 0:
        return []
    result = []
    for source_line in printable_text(text.expandtabs(4)).split("\n"):
        if not source_line:
            result.append("")
            continue
        current = []
        current_width = 0
        for char in source_line:
            char_width = (
                0
                if unicodedata.combining(char)
                else 2
                if unicodedata.east_asian_width(char) in "WF"
                else 1
            )
            if current and current_width + char_width > width:
                result.append("".join(current))
                current = []
                current_width = 0
            current.append(char)
            current_width += char_width
        result.append("".join(current))
    return result or [""]


def task_matches(task: dict, keyword: str) -> bool:
    needle = keyword.strip().casefold()
    if not needle:
        return True
    haystack = " ".join(
        str(task.get(name) or "")
        for name in ("title", "task_id", "task_group", "type", "assignee", "state")
    ).casefold()
    return needle in haystack


def board_content_key(tasks: list[dict]) -> str:
    return json.dumps(tasks, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def visible_column_count(width: int, total: int, *, single: bool = False) -> int:
    if single or total <= 1:
        return 1
    # n 栏需要 n 个最小宽度和 n-1 条分隔线.
    maximum = max(1, (width + 1) // (MIN_COLUMN_WIDTH + 1))
    return min(total, maximum)


class ScreenBuffer:
    def __init__(self, height: int, width: int) -> None:
        self.height = height
        self.width = width
        self.cells = [[(" ", 0) for _ in range(width)] for _ in range(height)]

    def write(
        self,
        y: int,
        x: int,
        text: str,
        attr: int = 0,
        width: Optional[int] = None,
    ) -> None:
        if y < 0 or y >= self.height or x < 0 or x >= self.width:
            return
        available = self.width - x if width is None else min(width, self.width - x)
        if available <= 0:
            return
        rendered = clip_text(text, available)
        column = x
        last_col: Optional[int] = None
        limit = x + available
        for char in rendered:
            char_width = display_width(char)
            if char_width == 0:
                if last_col is not None:
                    previous, previous_attr = self.cells[y][last_col]
                    self.cells[y][last_col] = (previous + char, previous_attr)
                continue
            if column >= self.width or column + char_width > limit:
                break
            self.cells[y][column] = (char, attr)
            if char_width == 2 and column + 1 < self.width:
                self.cells[y][column + 1] = ("", attr)
            last_col = column
            column += char_width

    def blit(self, screen, previous: Optional["ScreenBuffer"]) -> None:
        reuse = (
            previous is not None
            and previous.height == self.height
            and previous.width == self.width
        )
        for y in range(self.height):
            x = 0
            while x < self.width:
                char, attr = self.cells[y][x]
                if not char:
                    x += 1
                    continue
                old = previous.cells[y][x] if reuse else None
                span = max(1, display_width(char))
                if old == (char, attr):
                    x += span
                    continue
                start = x
                run = [char]
                run_attr = attr
                x += span
                while x < self.width:
                    char, attr = self.cells[y][x]
                    if not char:
                        x += 1
                        continue
                    old = previous.cells[y][x] if reuse else None
                    if attr != run_attr or old == (char, attr):
                        break
                    run.append(char)
                    x += max(1, display_width(char))
                try:
                    screen.addstr(y, start, "".join(run), run_attr)
                except (curses.error, UnicodeEncodeError):
                    pass


@dataclass
class BoardModel:
    single: bool = False
    tasks: list[dict] = field(default_factory=list)
    query: str = ""
    show_archived: bool = False
    column_index: int = 0
    column_offset: int = 0
    selected_ids: dict[str, Optional[str]] = field(
        default_factory=lambda: {state: None for state in ALL_STATES}
    )
    selected_indexes: dict[str, int] = field(
        default_factory=lambda: {state: 0 for state in ALL_STATES}
    )
    scrolls: dict[str, int] = field(
        default_factory=lambda: {state: 0 for state in ALL_STATES}
    )
    generated_at: str = ""
    content_key: str = ""
    refresh_error: str = ""
    detail_error: str = ""

    @property
    def error(self) -> str:
        return self.refresh_error or self.detail_error

    @property
    def states(self) -> tuple[str, ...]:
        return ALL_STATES if self.show_archived else ACTIVE_STATES

    @property
    def current_state(self) -> str:
        return self.states[self.column_index]

    def set_board(self, payload: dict) -> bool:
        tasks = payload.get("tasks", [])
        if not isinstance(tasks, list):
            raise KanbanTuiError("board payload tasks must be a list")
        parsed = [
            task
            for task in tasks
            if isinstance(task, dict) and task.get("state") in ALL_STATES
        ]
        generated_at = str(payload.get("generated_at") or "")
        next_key = board_content_key(parsed)
        error_cleared = bool(self.refresh_error)
        self.generated_at = generated_at
        self.refresh_error = ""
        if next_key == self.content_key:
            return error_cleared
        self.tasks = parsed
        self.content_key = next_key
        self.normalize()
        return True

    def tasks_for(self, state: str) -> list[dict]:
        return [
            task
            for task in self.tasks
            if task.get("state") == state and task_matches(task, self.query)
        ]

    def normalize(self) -> None:
        self.column_index = min(self.column_index, len(self.states) - 1)
        self.column_offset = max(0, min(self.column_offset, self.column_index))
        for state in ALL_STATES:
            tasks = self.tasks_for(state)
            task_ids = [str(task.get("task_id") or "") for task in tasks]
            selected = self.selected_ids[state]
            if selected in task_ids:
                self.selected_indexes[state] = task_ids.index(selected)
            elif task_ids:
                index = min(self.selected_indexes[state], len(task_ids) - 1)
                self.selected_ids[state] = task_ids[max(0, index)]
                self.selected_indexes[state] = max(0, index)
            else:
                self.selected_ids[state] = None
            self.scrolls[state] = max(
                0, min(self.scrolls[state], max(0, len(tasks) - 1))
            )

    def move_column(self, delta: int) -> None:
        self.column_index = (self.column_index + delta) % len(self.states)

    def ensure_column_visible(self, visible_count: int) -> None:
        visible_count = max(1, min(visible_count, len(self.states)))
        if self.column_index < self.column_offset:
            self.column_offset = self.column_index
        elif self.column_index >= self.column_offset + visible_count:
            self.column_offset = self.column_index - visible_count + 1
        self.column_offset = max(
            0, min(self.column_offset, max(0, len(self.states) - visible_count))
        )

    def visible_states(self, visible_count: int) -> tuple[str, ...]:
        self.ensure_column_visible(visible_count)
        end = self.column_offset + visible_count
        return self.states[self.column_offset : end]

    def move_task(self, delta: int) -> None:
        state = self.current_state
        tasks = self.tasks_for(state)
        if not tasks:
            self.selected_ids[state] = None
            return
        task_ids = [str(task.get("task_id") or "") for task in tasks]
        try:
            index = task_ids.index(self.selected_ids[state])
        except ValueError:
            index = 0
        index = max(0, min(len(task_ids) - 1, index + delta))
        self.selected_ids[state] = task_ids[index]
        self.selected_indexes[state] = index

    def selected_task(self) -> Optional[dict]:
        selected_id = self.selected_ids[self.current_state]
        return next(
            (
                task
                for task in self.tasks_for(self.current_state)
                if task.get("task_id") == selected_id
            ),
            None,
        )

    def toggle_archived(self) -> None:
        current = self.current_state
        self.show_archived = not self.show_archived
        if current in self.states:
            self.column_index = self.states.index(current)
        else:
            self.column_index = len(self.states) - 1
        self.normalize()


class KanbanTui:
    def __init__(
        self,
        screen,
        *,
        single: bool,
        refresh_interval: int,
        context: dict,
        get_board: Callable[[], dict],
        get_task: Callable[[str], dict],
        theme: str = "auto",
    ) -> None:
        self.screen = screen
        self.model = BoardModel(single=single)
        self.refresh_interval = refresh_interval
        self.theme = theme
        self.has_colors = False
        self.has_default_colors = False
        self.context = context
        self.get_board = get_board
        self.get_task = get_task
        self.searching = False
        self.detail: Optional[dict] = None
        self.detail_scroll = 0
        self.last_refresh = time.monotonic()
        self.running = True
        self.colors: dict[str, int] = {}
        self.glyphs = dict(ASCII_GLYPHS)
        self.frame: Optional[ScreenBuffer] = None
        self.prev_frame: Optional[ScreenBuffer] = None
        self.cursor_pos: Optional[tuple[int, int]] = None

    def run(self, initial_board: dict) -> None:
        self._init_style()
        self.model.set_board(initial_board)
        self.screen.keypad(True)
        self.screen.timeout(200)
        self._set_cursor(False)
        self._render(force=True)
        while self.running:
            try:
                key = self.screen.get_wch()
            except curses.error:
                key = None
            if key == curses.KEY_RESIZE:
                self._render(force=True)
            elif key is not None:
                if self.detail is not None:
                    self._handle_detail_key(key)
                elif self.searching:
                    self._handle_search_key(key)
                else:
                    self._handle_board_key(key)
                if self.running:
                    self._render()
            if time.monotonic() - self.last_refresh >= self.refresh_interval:
                if self._refresh():
                    self._render()

    def _init_style(self) -> None:
        encoding = getattr(self.screen, "encoding", "") or "ascii"
        try:
            for glyph in FANCY_GLYPHS.values():
                glyph.encode(encoding)
        except (UnicodeEncodeError, LookupError):
            pass
        else:
            self.glyphs = dict(FANCY_GLYPHS)
        try:
            self.has_colors = curses.has_colors()
        except curses.error:
            self.has_colors = False
        if self.has_colors:
            # 默认色扩展只影响 auto 主题; 不支持时 light/dark 仍可用固定背景色.
            try:
                curses.use_default_colors()
                self.has_default_colors = True
            except curses.error:
                self.has_default_colors = False
        self._apply_theme()

    def _set_background(self, attr: int) -> None:
        try:
            self.screen.bkgd(" ", attr)
        except curses.error:
            pass

    def _apply_theme(self) -> None:
        self.prev_frame = None
        self.colors = {}
        if not self.has_colors:
            return
        if self.theme == "auto":
            if not self.has_default_colors:
                # auto 降级为纯属性渲染时清除显式主题遗留的窗口背景.
                self._set_background(0)
                return
            background_code = os.environ.get("COLORFGBG", "").rsplit(";", 1)[-1]
            light_background = background_code in {"7", "15"}
            palette = dict(THEME_PALETTES["light" if light_background else "dark"])
            if not background_code:
                # 背景未知时文本和 backlog 用终端默认前景色, 避免浅色终端白底白字.
                palette["text"] = -1
                palette["backlog"] = -1
            background = -1
        else:
            palette = THEME_PALETTES[self.theme]
            background = THEME_BACKGROUNDS[self.theme]
        for index, name in enumerate(
            ("text", "backlog", "todo", "working", "done", "archived", "trash", "accent"),
            start=1,
        ):
            try:
                curses.init_pair(index, palette[name], background)
            except (curses.error, ValueError):
                # 终端颜色对不足 (COLOR_PAIRS 小) 时保留已建颜色, 缺失项回退到属性 0.
                continue
            self.colors[name] = curses.color_pair(index)
        self.colors["id"] = self.colors.get("working", 0)
        self.colors["group"] = self.colors.get("todo", 0)
        self.colors["error"] = self.colors.get("trash", 0)
        self._set_background(
            0 if self.theme == "auto" else self.colors.get("text", 0)
        )

    def _set_cursor(self, visible: bool) -> None:
        try:
            curses.curs_set(1 if visible else 0)
        except curses.error:
            pass

    def _refresh(self) -> bool:
        previous_error = self.model.refresh_error
        try:
            changed = self.model.set_board(self.get_board())
        except Exception as error:  # 刷新失败时保留上一份有效看板.
            self.model.refresh_error = str(error)
            self.last_refresh = time.monotonic()
            return self.model.refresh_error != previous_error
        self.last_refresh = time.monotonic()
        detail_changed = self._refresh_open_detail()
        return changed or detail_changed

    def _refresh_open_detail(self) -> bool:
        if self.detail is None:
            return False
        task_id = str(self.detail.get("task_id") or "")
        if not task_id:
            return False
        previous_error = self.model.detail_error
        try:
            next_detail = self.get_task(task_id)
        except Exception as error:
            self.model.detail_error = str(error)
            return self.model.detail_error != previous_error
        self.model.detail_error = ""
        if next_detail == self.detail:
            return bool(previous_error)
        self.detail = next_detail
        return True

    def _open_detail(self) -> None:
        selected = self.model.selected_task()
        if selected is None:
            return
        try:
            self.detail = self.get_task(str(selected.get("task_id") or ""))
            self.detail_scroll = 0
            self.model.detail_error = ""
        except Exception as error:
            self.model.detail_error = str(error)

    def _page_size(self) -> int:
        # 与 _render_column 的卡片容量保持一致: body_top=4, 页脚占 1 行.
        return max(1, (self.screen.getmaxyx()[0] - 4) // CARD_HEIGHT)

    def _page(self, direction: int) -> None:
        # 选中项和视口同步移动一整页, 渲染时再保证选中项可见.
        page = self._page_size()
        state = self.model.current_state
        self.model.move_task(direction * page)
        task_count = len(self.model.tasks_for(state))
        self.model.scrolls[state] = max(
            0,
            min(
                self.model.scrolls[state] + direction * page,
                max(0, task_count - page),
            ),
        )

    def _handle_board_key(self, key) -> None:
        if key in ("q", "Q"):
            self.running = False
        elif key in (curses.KEY_LEFT, "h", "H"):
            self.model.move_column(-1)
        elif key in (curses.KEY_RIGHT, "l", "L", "\t"):
            self.model.move_column(1)
        elif key in (curses.KEY_UP, "k", "K"):
            self.model.move_task(-1)
        elif key in (curses.KEY_DOWN, "j", "J"):
            self.model.move_task(1)
        elif key == curses.KEY_PPAGE:
            self._page(-1)
        elif key == curses.KEY_NPAGE:
            self._page(1)
        elif key == curses.KEY_HOME:
            self.model.move_task(-len(self.model.tasks))
        elif key == curses.KEY_END:
            self.model.move_task(len(self.model.tasks))
        elif key == "/":
            self.searching = True
            self._set_cursor(True)
        elif key in ("a", "A"):
            self.model.toggle_archived()
        elif key in ("t", "T"):
            self.theme = THEMES[(THEMES.index(self.theme) + 1) % len(THEMES)]
            self._apply_theme()
        elif key in ("r", "R"):
            self._refresh()
        elif key in ("\n", "\r", curses.KEY_ENTER):
            self._open_detail()

    def _handle_search_key(self, key) -> None:
        if key in ("\n", "\r", curses.KEY_ENTER):
            self.searching = False
            self._set_cursor(False)
        elif key == "\x1b":
            self.model.query = ""
            self.model.normalize()
            self.searching = False
            self._set_cursor(False)
        elif key in (curses.KEY_BACKSPACE, "\b", "\x7f"):
            self.model.query = self.model.query[:-1]
            self.model.normalize()
        elif isinstance(key, str) and key.isprintable():
            self.model.query += key
            self.model.normalize()

    def _handle_detail_key(self, key) -> None:
        if key in ("q", "Q", "\x1b", curses.KEY_BACKSPACE, "\b", "\x7f"):
            self.detail = None
            self.detail_scroll = 0
        elif key in (curses.KEY_UP, "k", "K"):
            self.detail_scroll = max(0, self.detail_scroll - 1)
        elif key in (curses.KEY_DOWN, "j", "J"):
            self.detail_scroll += 1
        elif key == curses.KEY_PPAGE:
            page_height = max(1, self.screen.getmaxyx()[0] - 5)
            self.detail_scroll = max(0, self.detail_scroll - page_height)
        elif key == curses.KEY_NPAGE:
            self.detail_scroll += max(1, self.screen.getmaxyx()[0] - 5)
        elif key == curses.KEY_HOME:
            self.detail_scroll = 0
        elif key == curses.KEY_END:
            self.detail_scroll = sys.maxsize

    def _render(self, *, force: bool = False) -> None:
        height, width = self.screen.getmaxyx()
        self.cursor_pos = None
        self.frame = ScreenBuffer(height, width)
        if self.detail is not None:
            self._render_detail()
        else:
            self._render_board()
        self.frame.blit(self.screen, None if force else self.prev_frame)
        self.prev_frame = self.frame
        self.frame = None
        if self.searching and self.cursor_pos is not None:
            try:
                self.screen.move(*self.cursor_pos)
            except curses.error:
                pass
        try:
            self.screen.refresh()
        except curses.error:
            pass

    def _add(
        self,
        y: int,
        x: int,
        text: str,
        attr: int = 0,
        width: Optional[int] = None,
    ) -> None:
        if self.frame is not None:
            self.frame.write(y, x, text, attr, width)
            return
        height, screen_width = self.screen.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= screen_width:
            return
        available = screen_width - x if width is None else min(width, screen_width - x)
        rendered = clip_text(text, available)
        try:
            self.screen.addstr(y, x, rendered, attr)
        except (curses.error, UnicodeEncodeError):
            pass

    def _render_board(self) -> None:
        height, width = self.screen.getmaxyx()
        title = self.context.get("title", "Task Board")
        accent = self.colors.get("accent", 0)
        self._add(0, 0, title, accent | curses.A_BOLD, width)
        # 高度 8 时首张卡片末行会被提示栏覆盖, 因此最小高度为 9.
        if height < MIN_BOARD_HEIGHT or width < MIN_COLUMN_WIDTH:
            message = self.context.get("too_small", "Terminal is too small.")
            self._add(2, 0, message, curses.A_BOLD, width)
            quit_help = self.context.get("quit_help", "q quit")
            self._add(height - 1, 0, quit_help, self._footer_attr(), width)
            return

        query_prefix = self.context.get("search", "Search") + ": "
        query_text = self.model.query
        mode = (
            self.context.get("all", "all")
            if self.model.show_archived
            else self.context.get("active", "active")
        )
        stamp = self.model.generated_at or "-"
        theme_label = self.context.get("theme_labels", {}).get(self.theme, self.theme)
        toolbar_right = (
            f"{self.context.get('theme', 'Theme')} {theme_label} | "
            f"{mode} | {self.context.get('updated', 'Updated')} {stamp}"
        )
        toolbar_left_width = max(
            12,
            display_width(query_prefix) + 4,
            width // 3,
        )
        toolbar_right_width = max(0, width - toolbar_left_width - 1)
        toolbar_right = clip_text(toolbar_right, toolbar_right_width)
        if self.searching:
            search_attr = accent | curses.A_BOLD
        elif query_text:
            search_attr = curses.A_BOLD
        else:
            search_attr = curses.A_DIM
        self._add(1, 0, query_prefix + query_text, search_attr, toolbar_left_width)
        right_x = max(0, width - display_width(toolbar_right))
        self._add(1, right_x, toolbar_right, curses.A_DIM, width - right_x)

        if self.searching:
            cursor_text = clip_text(query_prefix + query_text, toolbar_left_width)
            cursor_x = min(width - 1, display_width(cursor_text))
            self.cursor_pos = (1, cursor_x)
            if self.frame is None:
                try:
                    self.screen.move(1, cursor_x)
                except curses.error:
                    pass

        count = visible_column_count(
            width, len(self.model.states), single=self.model.single
        )
        states = self.model.visible_states(count)
        more_left = self.model.column_offset > 0
        more_right = self.model.column_offset + len(states) < len(self.model.states)
        body_top = 4
        body_height = height - body_top - 1
        for index, state in enumerate(states):
            x = index * width // len(states)
            end = (index + 1) * width // len(states)
            separator = index < len(states) - 1
            column_width = end - x - (1 if separator else 0)
            if separator:
                for y in range(2, height - 1):
                    self._add(y, end - 1, self.glyphs["vbar"], curses.A_DIM, 1)
            self._render_column(
                state,
                x,
                column_width,
                body_top,
                body_height,
                focused=state == self.model.current_state,
                first_visible=index == 0,
                last_visible=index == len(states) - 1,
                more_left=more_left,
                more_right=more_right,
            )
        self._render_footer(height, width)

    def _render_column(
        self,
        state: str,
        x: int,
        width: int,
        body_top: int,
        body_height: int,
        focused: bool,
        first_visible: bool = True,
        last_visible: bool = True,
        more_left: bool = False,
        more_right: bool = False,
    ) -> None:
        tasks = self.model.tasks_for(state)
        label = self.context.get("state_labels", {}).get(state, state)
        state_color = self.colors.get(state, 0)
        heading_text = f"{label} ({len(tasks)})"
        if self.model.single or (
            first_visible and last_visible and len(self.model.states) > 1
        ):
            heading_text = (
                f"{self.glyphs['left']} {heading_text} {self.glyphs['right']}"
            )
        else:
            if first_visible and more_left:
                heading_text = f"{self.glyphs['left']} {heading_text}"
            if last_visible and more_right:
                heading_text = f"{heading_text} {self.glyphs['right']}"
        heading_attr = state_color | curses.A_BOLD
        if focused:
            heading_attr |= curses.A_REVERSE
        self._add(2, x, pad_text(f" {heading_text}", width), heading_attr, width)
        if not tasks:
            empty = self.context.get("empty", "No tasks")
            self._add(body_top, x + 2, empty, curses.A_DIM, max(0, width - 3))
            return

        # 最后一张卡不需要卡片间的空行, 因此容量按 body_height + 1 计算.
        capacity = max(1, (body_height + 1) // CARD_HEIGHT)
        task_ids = [str(task.get("task_id") or "") for task in tasks]
        selected_id = self.model.selected_ids[state]
        try:
            selected_index = task_ids.index(selected_id)
        except ValueError:
            selected_index = 0
            self.model.selected_ids[state] = task_ids[0]
        scroll = self.model.scrolls[state]
        if selected_index < scroll:
            scroll = selected_index
        elif selected_index >= scroll + capacity:
            scroll = selected_index - capacity + 1
        scroll = max(0, min(scroll, max(0, len(tasks) - capacity)))
        self.model.scrolls[state] = scroll

        content_width = max(1, width - 3)
        for row, task in enumerate(tasks[scroll : scroll + capacity]):
            task_index = scroll + row
            y = body_top + row * CARD_HEIGHT
            selected = focused and task_index == selected_index
            group_or_type = task.get("task_group") or " / ".join(
                value
                for value in (
                    str(task.get("type") or "-"),
                    self.context.get("size_labels", {}).get(
                        task.get("kind"), str(task.get("kind") or "-")
                    ),
                )
                if value
            )
            assignee = task.get("assignee") or self.context.get(
                "unassigned", "Unassigned"
            )
            lines = (
                (str(task.get("title") or task.get("task_id") or ""), curses.A_BOLD),
                (str(task.get("task_id") or ""), self.colors.get("id", 0)),
                (str(group_or_type), self.colors.get("group", 0)),
                (f"{assignee} | {task.get('time') or '-'}", curses.A_DIM),
            )
            bar_attr = state_color | (curses.A_BOLD if selected else curses.A_DIM)
            for offset, (line, attr) in enumerate(lines):
                if selected:
                    attr = state_color | curses.A_REVERSE | (attr & curses.A_BOLD)
                self._add(y + offset, x, self.glyphs["bar"], bar_attr, 1)
                self._add(
                    y + offset,
                    x + 2,
                    pad_text(line, content_width),
                    attr,
                    content_width,
                )

    def _footer_attr(self, error: bool = False) -> int:
        # 提示栏用强调色反白, 与状态色的选中卡和栏目高亮区分.
        if error:
            return self.colors.get("error", 0) | curses.A_REVERSE | curses.A_BOLD
        return self.colors.get("accent", 0) | curses.A_REVERSE

    def _render_footer(self, height: int, width: int) -> None:
        if self.model.error:
            footer = f"{self.context.get('error', 'Error')}: {self.model.error}"
        elif self.searching:
            footer = self.context.get("search_help", "Enter apply | Esc clear")
        else:
            footer = self.context.get(
                "help",
                "arrows/hjkl move | / search | Enter detail | a archive | r refresh | q quit",
            )
        self._add(
            height - 1,
            0,
            pad_text(" " + footer, width),
            self._footer_attr(bool(self.model.error)),
            width,
        )

    def _render_detail(self) -> None:
        height, width = self.screen.getmaxyx()
        if height < 6 or width < 20:
            message = self.context.get("too_small", "Terminal is too small.")
            self._add(0, 0, message, curses.A_BOLD, width)
            return
        task = self.detail or {}
        accent = self.colors.get("accent", 0)
        state_color = self.colors.get(str(task.get("state") or ""), 0)
        title = str(task.get("title") or task.get("task_id") or "")
        self._add(0, 0, self.glyphs["bar"], state_color | curses.A_BOLD, 1)
        self._add(0, 2, title, curses.A_BOLD, max(0, width - 2))
        meta = " | ".join(
            str(value)
            for value in (
                task.get("task_id"),
                self.context.get("state_labels", {}).get(
                    task.get("state"), task.get("state")
                ),
                self.context.get("size_labels", {}).get(
                    task.get("kind"), task.get("kind")
                ),
                task.get("type"),
                task.get("assignee") or self.context.get("unassigned", "Unassigned"),
            )
            if value
        )
        self._add(1, 0, self.glyphs["bar"], state_color | curses.A_BOLD, 1)
        self._add(1, 2, meta, curses.A_DIM, max(0, width - 2))
        self._add(2, 0, self.glyphs["hbar"] * width, curses.A_DIM, width)
        body_height = height - 4
        lines = wrap_text(str(task.get("document") or ""), max(1, width - 1))
        maximum_scroll = max(0, len(lines) - body_height)
        self.detail_scroll = max(0, min(self.detail_scroll, maximum_scroll))
        visible_lines = lines[
            self.detail_scroll : self.detail_scroll + body_height
        ]
        for index, line in enumerate(visible_lines):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                attr = accent | curses.A_BOLD
            elif stripped.startswith(">"):
                attr = curses.A_DIM
            else:
                attr = 0
            self._add(3 + index, 0, line, attr, width - 1)
        visible_end = min(len(lines), self.detail_scroll + body_height)
        position = f"{self.detail_scroll + 1}-{visible_end}/{len(lines)}"
        if self.model.error:
            footer = f"{self.context.get('error', 'Error')}: {self.model.error}"
        else:
            help_text = self.context.get(
                "detail_help", "arrows/jk scroll | PgUp/PgDn | q/Esc back"
            )
            footer = f"{help_text} | {position}"
        self._add(
            height - 1,
            0,
            pad_text(" " + footer, width),
            self._footer_attr(bool(self.model.error)),
            width,
        )


def run(
    *,
    single: bool,
    refresh_interval: int,
    context: dict,
    get_board: Callable[[], dict],
    get_task: Callable[[str], dict],
    theme: str = "auto",
) -> None:
    if theme not in THEMES:
        raise KanbanTuiError(f"unknown theme: {theme}")

    try:
        initial_board = get_board()
    except Exception as error:
        raise KanbanTuiError(str(error)) from error

    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass

    def start(screen) -> None:
        KanbanTui(
            screen,
            single=single,
            refresh_interval=refresh_interval,
            context=context,
            get_board=get_board,
            get_task=get_task,
            theme=theme,
        ).run(initial_board)

    try:
        curses.wrapper(start)
    except KeyboardInterrupt:
        return
    except (curses.error, ValueError) as error:
        raise KanbanTuiError(f"failed to initialize terminal: {error}") from error
