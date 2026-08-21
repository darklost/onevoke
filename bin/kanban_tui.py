#!/usr/bin/env python3

import curses
import json
import locale
import os
import sys
import tempfile
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


ACTIVE_STATES = ("backlog", "todo", "working", "done")
ALL_STATES = ACTIVE_STATES + ("archived", "trash")
CARD_HEIGHT = 4
DEFAULT_COLUMN_WIDTH = 40
MIN_COLUMN_WIDTH = 10
MAX_COLUMN_WIDTH = 120
COLUMN_WIDTH_STEP = 5
MIN_BOARD_HEIGHT = 8
BODY_TOP = 4
MOUSE_SCROLL_STEP = 3

FANCY_GLYPHS = {
    "vbar": "│",
    "bar": "▎",
    "hbar": "─",
    "dot": "·",
    "left": "‹",
    "right": "›",
}
ASCII_GLYPHS = {
    "vbar": "|",
    "bar": ">",
    "hbar": "-",
    "dot": "|",
    "left": "<",
    "right": ">",
}

THEMES = ("auto", "light", "dark")

THEME_PALETTES = {
    "dark": {
        "text": curses.COLOR_WHITE,
        # backlog 不用白/黑: 与正文同色时栏目标题不像栏目, 选中反色也不如彩底醒目.
        "backlog": curses.COLOR_CYAN,
        "todo": curses.COLOR_YELLOW,
        "working": curses.COLOR_BLUE,
        "done": curses.COLOR_GREEN,
        "archived": curses.COLOR_MAGENTA,
        "trash": curses.COLOR_RED,
        "accent": curses.COLOR_MAGENTA,
    },
    "light": {
        "text": curses.COLOR_BLACK,
        # 浅色终端里 BLACK|BOLD 常被渲染成亮黑/灰, 未选中栏目标题几乎看不见.
        "backlog": curses.COLOR_CYAN,
        "todo": curses.COLOR_YELLOW,
        "working": curses.COLOR_BLUE,
        "done": curses.COLOR_GREEN,
        "archived": curses.COLOR_MAGENTA,
        "trash": curses.COLOR_RED,
        "accent": curses.COLOR_MAGENTA,
    },
}
THEME_BACKGROUNDS = {"light": curses.COLOR_WHITE, "dark": curses.COLOR_BLACK}
COLOR_NAMES = (
    "text",
    "backlog",
    "todo",
    "working",
    "done",
    "archived",
    "trash",
    "accent",
)


class KanbanTuiError(Exception):
    pass


def terminal_light_background() -> Optional[bool]:
    """COLORFGBG 背景色: 7/15 为浅色, 有值但非浅色为深色, 缺失则未知."""
    background_code = os.environ.get("COLORFGBG", "").rsplit(";", 1)[-1]
    if not background_code:
        return None
    return background_code in {"7", "15"}


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


def compact_time(value: str) -> str:
    """去掉 YYYY- 年份前缀: 任务 ID 已含年月日, 卡片时间只留月日时分."""
    if len(value) > 5 and value[:4].isdigit() and value[4] == "-":
        return value[5:]
    return value


def task_matches(task: dict, keyword: str) -> bool:
    needle = keyword.strip().casefold()
    if not needle:
        return True
    haystack = " ".join(
        str(task.get(name) or "")
        for name in ("title", "task_id", "task_group", "type", "assignee", "state")
    ).casefold()
    return needle in haystack


def line_match_indexes(lines: list[str], keyword: str) -> list[int]:
    needle = keyword.strip().casefold()
    if not needle:
        return []
    return [index for index, line in enumerate(lines) if needle in line.casefold()]


def match_spans(text: str, keyword: str) -> list[tuple[int, int]]:
    needle = keyword.strip().casefold()
    if not needle:
        return []
    folded = text.casefold()
    # casefold 可能改变长度 (如 ß→ss); 此时整行高亮, 避免错位.
    if len(folded) != len(text):
        return [(0, len(text))] if needle in folded else []
    spans: list[tuple[int, int]] = []
    start = 0
    step = max(1, len(needle))
    while True:
        index = folded.find(needle, start)
        if index < 0:
            break
        spans.append((index, index + len(needle)))
        start = index + step
    return spans


def board_content_key(tasks: list[dict]) -> str:
    return json.dumps(tasks, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def clamp_column_width(width: int) -> int:
    return max(MIN_COLUMN_WIDTH, min(MAX_COLUMN_WIDTH, int(width)))


def prefs_path() -> Path:
    override = os.environ.get("ONEVOKE_CONFIG")
    if override:
        return Path(override).expanduser().with_name("tui.json")
    return Path.home() / ".config" / "onevoke" / "tui.json"


def load_column_width(default: int = DEFAULT_COLUMN_WIDTH) -> int:
    fallback = clamp_column_width(default)
    path = prefs_path()
    if not path.is_file():
        return fallback
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        # ValueError 覆盖 JSONDecodeError, 以及超大整数转换失败.
        return fallback
    if not isinstance(raw, dict):
        return fallback
    value = raw.get("column_width", default)
    if isinstance(value, bool) or not isinstance(value, int):
        return fallback
    if value < MIN_COLUMN_WIDTH or value > MAX_COLUMN_WIDTH:
        return fallback
    return value


def save_column_width(width: int) -> None:
    path = prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"column_width": clamp_column_width(width)}
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def visible_column_count(
    width: int,
    total: int,
    *,
    single: bool = False,
    column_width: int = DEFAULT_COLUMN_WIDTH,
) -> int:
    if single or total <= 1:
        return 1
    preferred = clamp_column_width(column_width)
    # n 栏需要 n 个最小宽度和 n-1 条分隔线.
    maximum = max(1, (width + 1) // (preferred + 1))
    return min(total, maximum)


def column_geometry(width: int, count: int) -> list[tuple[int, int, bool]]:
    count = max(1, count)
    separators = count - 1
    usable = max(0, width - separators)
    layout = []
    cursor = 0
    for index in range(count):
        start = index * usable // count
        end = (index + 1) * usable // count
        column_width = end - start
        has_separator = index < separators
        layout.append((cursor, column_width, has_separator))
        cursor += column_width + (1 if has_separator else 0)
    return layout


def column_task_window(
    model: "BoardModel",
    state: str,
    body_height: int,
) -> tuple[list[dict], int, int]:
    """返回 (tasks, scroll, capacity), scroll 与渲染逻辑一致并写回 model."""
    tasks = model.tasks_for(state)
    capacity = max(1, (body_height + 1) // CARD_HEIGHT)
    if not tasks:
        model.scrolls[state] = 0
        return tasks, 0, capacity
    task_ids = [str(task.get("task_id") or "") for task in tasks]
    selected_id = model.selected_ids[state]
    try:
        selected_index = task_ids.index(selected_id)
    except ValueError:
        selected_index = 0
        model.selected_ids[state] = task_ids[0]
        model.selected_indexes[state] = 0
    scroll = model.scrolls[state]
    if selected_index < scroll:
        scroll = selected_index
    elif selected_index >= scroll + capacity:
        scroll = selected_index - capacity + 1
    scroll = max(0, min(scroll, max(0, len(tasks) - capacity)))
    model.scrolls[state] = scroll
    return tasks, scroll, capacity


def mouse_wheel_delta(bstate: int) -> int:
    button4 = getattr(curses, "BUTTON4_PRESSED", 0)
    button5 = getattr(curses, "BUTTON5_PRESSED", 0)
    if button4 and bstate & button4:
        return -1
    if button5 and bstate & button5:
        return 1
    return 0


def mouse_left_clicked(bstate: int) -> bool:
    # CLICKED 优先; 无 CLICKED 位时回退 PRESSED (部分终端不合成 CLICKED).
    if bstate & curses.BUTTON1_DOUBLE_CLICKED:
        return True
    if bstate & curses.BUTTON1_CLICKED:
        return True
    return bool(
        bstate & curses.BUTTON1_PRESSED
        and not (bstate & curses.BUTTON1_RELEASED)
    )


def mouse_left_double_clicked(bstate: int) -> bool:
    return bool(bstate & curses.BUTTON1_DOUBLE_CLICKED)


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

    def focus_state(self, state: str) -> bool:
        if state not in self.states:
            return False
        self.column_index = self.states.index(state)
        return True

    def select_task_index(self, state: str, index: int) -> bool:
        if not self.focus_state(state):
            return False
        tasks = self.tasks_for(state)
        if not tasks:
            return False
        index = max(0, min(len(tasks) - 1, index))
        self.selected_ids[state] = str(tasks[index].get("task_id") or "")
        self.selected_indexes[state] = index
        return True

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
        column_width: int = DEFAULT_COLUMN_WIDTH,
        persist_column_width: Optional[Callable[[int], None]] = None,
    ) -> None:
        self.screen = screen
        self.model = BoardModel(single=single)
        self.refresh_interval = refresh_interval
        self.theme = theme
        self.column_width = clamp_column_width(column_width)
        self.persist_column_width = persist_column_width
        self.has_colors = False
        self.has_default_colors = False
        self.context = context
        self.get_board = get_board
        self.get_task = get_task
        self.searching = False
        self.detail: Optional[dict] = None
        self.detail_scroll = 0
        self.detail_searching = False
        self.detail_query = ""
        self.detail_match_index = 0
        self.detail_pending_g = False
        self.last_refresh = time.monotonic()
        self.running = True
        self.colors: dict[str, int] = {}
        self.glyphs = dict(ASCII_GLYPHS)
        self.frame: Optional[ScreenBuffer] = None
        self.prev_frame: Optional[ScreenBuffer] = None
        self.cursor_pos: Optional[tuple[int, int]] = None
        self.prefs_error = ""
        self.highlight_colors: dict[str, int] = {}
        self.mouse_enabled = False

    def run(self, initial_board: dict) -> None:
        self._init_style()
        self._enable_mouse()
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
            elif key == curses.KEY_MOUSE:
                self._handle_mouse()
                if self.running:
                    self._render()
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

    def _enable_mouse(self) -> None:
        mask = (
            curses.BUTTON1_PRESSED
            | curses.BUTTON1_CLICKED
            | curses.BUTTON1_DOUBLE_CLICKED
        )
        for name in ("BUTTON4_PRESSED", "BUTTON5_PRESSED"):
            value = getattr(curses, name, 0)
            if value:
                mask |= value
        try:
            available, _old = curses.mousemask(mask)
            self.mouse_enabled = bool(available)
        except curses.error:
            self.mouse_enabled = False

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
        self.highlight_colors = {}
        if not self.has_colors:
            return
        highlight_background = None
        if self.theme == "auto":
            if not self.has_default_colors:
                # auto 降级为纯属性渲染时清除显式主题遗留的窗口背景.
                self._set_background(0)
                return
            light_background = terminal_light_background()
            variant = "light" if light_background else "dark"
            palette = dict(THEME_PALETTES[variant])
            if light_background is None:
                # 背景未知时仅正文用终端默认前景; backlog 保持色相, 避免栏目标题发灰.
                palette["text"] = -1
            background = -1
            # 选中/焦点用显式底色再反色; 默认底 (-1) 上反色在部分终端对比不足.
            highlight_background = THEME_BACKGROUNDS[variant]
        else:
            variant = self.theme
            palette = THEME_PALETTES[self.theme]
            background = THEME_BACKGROUNDS[self.theme]
        for index, name in enumerate(COLOR_NAMES, start=1):
            try:
                curses.init_pair(index, palette[name], background)
            except (curses.error, ValueError):
                # 终端颜色对不足 (COLOR_PAIRS 小) 时保留已建颜色, 缺失项回退到属性 0.
                continue
            self.colors[name] = curses.color_pair(index)
        if highlight_background is not None:
            highlight_palette = dict(palette)
            fallback_fg = (
                curses.COLOR_BLACK
                if highlight_background == curses.COLOR_WHITE
                else curses.COLOR_WHITE
            )
            for name, foreground in highlight_palette.items():
                if foreground == -1:
                    highlight_palette[name] = fallback_fg
            for index, name in enumerate(COLOR_NAMES, start=1 + len(COLOR_NAMES)):
                try:
                    curses.init_pair(
                        index, highlight_palette[name], highlight_background
                    )
                except (curses.error, ValueError):
                    continue
                self.highlight_colors[name] = curses.color_pair(index)
        self.colors["id"] = self.colors.get("working", 0)
        self.colors["group"] = self.colors.get("todo", 0)
        self.colors["error"] = self.colors.get("trash", 0)
        self.colors["muted"] = self._muted_attr(variant, background)
        self._set_background(
            0 if self.theme == "auto" else self.colors.get("text", 0)
        )

    def _muted_attr(self, variant: str, background: int) -> int:
        """接近背景色的弱化前景, 用于卡片分隔线; 不支持时回退 A_DIM."""
        muted_index = 1 + 2 * len(COLOR_NAMES)
        if getattr(curses, "COLORS", 0) >= 256:
            # 256 色: 深底用暗灰, 浅底用亮灰, 都只比背景略微可见.
            foreground = 252 if variant == "light" else 238
            try:
                curses.init_pair(muted_index, foreground, background)
                return curses.color_pair(muted_index)
            except (curses.error, ValueError):
                return curses.A_DIM
        if variant != "light":
            # 8 色深底: 亮黑 (BOLD 黑) 近似深灰.
            try:
                curses.init_pair(muted_index, curses.COLOR_BLACK, background)
                return curses.color_pair(muted_index) | curses.A_BOLD
            except (curses.error, ValueError):
                return curses.A_DIM
        return curses.A_DIM

    def _highlight_attr(self, color_name: str, extra: int = 0) -> int:
        # auto 的选中/焦点走显式底色色对, 其余主题直接反色状态色.
        base = self.highlight_colors.get(color_name, self.colors.get(color_name, 0))
        return base | curses.A_REVERSE | extra

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
        matches = self._detail_matches()
        if matches:
            self.detail_match_index = min(self.detail_match_index, len(matches) - 1)
        else:
            self.detail_match_index = 0
        return True

    def _open_detail(self) -> None:
        selected = self.model.selected_task()
        if selected is None:
            return
        try:
            self.detail = self.get_task(str(selected.get("task_id") or ""))
            self.detail_scroll = 0
            self._reset_detail_search()
            self.model.detail_error = ""
        except Exception as error:
            self.model.detail_error = str(error)

    def _close_detail(self) -> None:
        self.detail = None
        self.detail_scroll = 0
        self._reset_detail_search()

    def _reset_detail_search(self) -> None:
        self.detail_searching = False
        self.detail_query = ""
        self.detail_match_index = 0
        self.detail_pending_g = False
        self._set_cursor(False)

    def _detail_body_height(self) -> int:
        return max(1, self.screen.getmaxyx()[0] - 4)

    def _detail_lines(self) -> list[str]:
        width = max(1, self.screen.getmaxyx()[1] - 1)
        task = self.detail or {}
        return wrap_text(str(task.get("document") or ""), width)

    def _detail_matches(self, lines: Optional[list[str]] = None) -> list[int]:
        return line_match_indexes(
            lines if lines is not None else self._detail_lines(),
            self.detail_query,
        )

    def _scroll_detail_by(self, delta: int) -> None:
        self.detail_scroll = max(0, self.detail_scroll + delta)

    def _reveal_detail_line(self, line_index: int, lines: list[str]) -> None:
        body_height = self._detail_body_height()
        maximum_scroll = max(0, len(lines) - body_height)
        # 尽量把命中行放在可视区上三分之一, 方便阅读上下文.
        preferred = max(0, line_index - max(0, body_height // 3))
        self.detail_scroll = max(0, min(preferred, maximum_scroll))

    def _jump_detail_match(self, direction: int) -> None:
        matches = self._detail_matches()
        if not matches:
            return
        if self.detail_match_index >= len(matches):
            self.detail_match_index = 0
        self.detail_match_index = (self.detail_match_index + direction) % len(matches)
        self._reveal_detail_line(matches[self.detail_match_index], self._detail_lines())

    def _apply_detail_search(self) -> None:
        self.detail_searching = False
        self._set_cursor(False)
        matches = self._detail_matches()
        if not matches:
            self.detail_match_index = 0
            return
        self.detail_match_index = 0
        self._reveal_detail_line(matches[0], self._detail_lines())

    def _page_size(self) -> int:
        # 与 _render_column 的卡片容量保持一致: body_top=BODY_TOP, 页脚占 1 行.
        return max(1, (self.screen.getmaxyx()[0] - BODY_TOP) // CARD_HEIGHT)

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

    def _visible_column_layout(self) -> list[tuple[str, int, int]]:
        width = self.screen.getmaxyx()[1]
        count = visible_column_count(
            width,
            len(self.model.states),
            single=self.model.single,
            column_width=self.column_width,
        )
        states = self.model.visible_states(count)
        return [
            (state, x, column_width)
            for state, (x, column_width, _separator) in zip(
                states, column_geometry(width, len(states))
            )
        ]

    def _handle_mouse(self) -> None:
        if not self.mouse_enabled:
            return
        try:
            _id, x, y, _z, bstate = curses.getmouse()
        except curses.error:
            return
        if self.detail is not None:
            self._handle_detail_mouse(x, y, bstate)
        elif self.searching:
            self._handle_search_mouse(x, y, bstate)
        else:
            self._handle_board_mouse(x, y, bstate)

    def _handle_search_mouse(self, x: int, y: int, bstate: int) -> None:
        if not mouse_left_clicked(bstate):
            return
        # 点到搜索行外则结束编辑并保留当前查询.
        if y != 1:
            self.searching = False
            self._set_cursor(False)

    def _handle_detail_mouse(self, x: int, y: int, bstate: int) -> None:
        if self.detail_searching:
            if mouse_left_clicked(bstate) and y != 2:
                self._apply_detail_search()
            return
        delta = mouse_wheel_delta(bstate)
        if delta:
            self._scroll_detail_by(delta * MOUSE_SCROLL_STEP)
            return
        # 详情页左键暂不关闭, 避免误触; 滚轮负责滚动.

    def _handle_board_mouse(self, x: int, y: int, bstate: int) -> None:
        delta = mouse_wheel_delta(bstate)
        if delta:
            target = self._hit_column_at(x, y)
            if target is not None:
                self.model.focus_state(target)
            self.model.move_task(delta)
            return
        if not mouse_left_clicked(bstate):
            return
        height, width = self.screen.getmaxyx()
        if height < MIN_BOARD_HEIGHT or width < 1:
            return
        if y == 1:
            self.searching = True
            self._set_cursor(True)
            return
        if y >= height - 1:
            return
        hit = self._hit_board(x, y)
        if hit is None:
            return
        kind = hit[0]
        if kind == "nav":
            self.model.move_column(hit[1])
            return
        if kind == "column":
            self.model.focus_state(hit[1])
            return
        if kind == "task":
            _kind, state, index = hit
            self.model.select_task_index(state, index)
            if mouse_left_double_clicked(bstate):
                self._open_detail()

    def _hit_column_at(self, x: int, y: int) -> Optional[str]:
        if y < 2:
            return None
        for state, col_x, col_width in self._visible_column_layout():
            if col_x <= x < col_x + col_width:
                return state
        return None

    def _hit_board(self, x: int, y: int) -> Optional[tuple]:
        height, _width = self.screen.getmaxyx()
        layout = self._visible_column_layout()
        if not layout:
            return None
        body_height = height - BODY_TOP - 1
        for index, (state, col_x, col_width) in enumerate(layout):
            if not (col_x <= x < col_x + col_width):
                continue
            local_x = x - col_x
            first_visible = index == 0
            last_visible = index == len(layout) - 1
            single_nav = self.model.single or (
                first_visible and last_visible and len(self.model.states) > 1
            )
            if y == 2 and single_nav and len(self.model.states) > 1:
                # 单栏标题两侧的 ‹ › 用于切栏.
                if local_x <= 2:
                    return ("nav", -1)
                if local_x >= max(0, col_width - 3):
                    return ("nav", 1)
            if y <= 3:
                return ("column", state)
            if y < BODY_TOP or body_height <= 0:
                return ("column", state)
            tasks, scroll, capacity = column_task_window(
                self.model, state, body_height
            )
            if not tasks:
                return ("column", state)
            row = (y - BODY_TOP) // CARD_HEIGHT
            if row < 0 or row >= capacity:
                return ("column", state)
            task_index = scroll + row
            if task_index >= len(tasks):
                return ("column", state)
            # 卡片间隙行仍算该卡.
            return ("task", state, task_index)
        return None

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
        elif key in ("-", "_"):
            self._adjust_column_width(-COLUMN_WIDTH_STEP)
        elif key in ("=", "+"):
            self._adjust_column_width(COLUMN_WIDTH_STEP)
        elif key in ("r", "R"):
            self._refresh()
        elif key in ("\n", "\r", curses.KEY_ENTER):
            self._open_detail()

    def _adjust_column_width(self, delta: int) -> None:
        next_width = clamp_column_width(self.column_width + delta)
        if next_width == self.column_width:
            return
        self.column_width = next_width
        if self.persist_column_width is None:
            self.prefs_error = ""
            return
        try:
            self.persist_column_width(self.column_width)
            self.prefs_error = ""
        except Exception as error:  # 记住失败时仍保留本次会话中的新宽度.
            self.prefs_error = str(error)

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

    def _handle_detail_search_key(self, key) -> None:
        if key in ("\n", "\r", curses.KEY_ENTER):
            self._apply_detail_search()
        elif key == "\x1b":
            self.detail_query = ""
            self.detail_match_index = 0
            self.detail_searching = False
            self._set_cursor(False)
        elif key in (curses.KEY_BACKSPACE, "\b", "\x7f"):
            self.detail_query = self.detail_query[:-1]
        elif isinstance(key, str) and key.isprintable():
            self.detail_query += key

    def _handle_detail_key(self, key) -> None:
        if self.detail_searching:
            self._handle_detail_search_key(key)
            return
        if self.detail_pending_g:
            self.detail_pending_g = False
            if key == "g":
                self.detail_scroll = 0
                return
            # 未组成 gg 时继续处理本次按键 (例如 g 后按 G 仍跳到底).
        page_height = self._detail_body_height()
        half_page = max(1, page_height // 2)
        if key in ("q", "Q", "\x1b", curses.KEY_BACKSPACE, "\b", "\x7f"):
            self._close_detail()
        elif key in (curses.KEY_UP, "k", "K"):
            self._scroll_detail_by(-1)
        elif key in (curses.KEY_DOWN, "j", "J"):
            self._scroll_detail_by(1)
        elif key in (curses.KEY_PPAGE, "\x02"):  # Ctrl-b
            self._scroll_detail_by(-page_height)
        elif key in (curses.KEY_NPAGE, "\x06"):  # Ctrl-f
            self._scroll_detail_by(page_height)
        elif key == "\x15":  # Ctrl-u
            self._scroll_detail_by(-half_page)
        elif key == "\x04":  # Ctrl-d
            self._scroll_detail_by(half_page)
        elif key == "g":
            self.detail_pending_g = True
        elif key in ("G", curses.KEY_END):
            self.detail_scroll = sys.maxsize
        elif key == curses.KEY_HOME:
            self.detail_scroll = 0
        elif key == "/":
            self.detail_searching = True
            self.detail_pending_g = False
            self._set_cursor(True)
        elif key == "n":
            self._jump_detail_match(1)
        elif key == "N":
            self._jump_detail_match(-1)
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
        if (
            (self.searching or self.detail_searching)
            and self.cursor_pos is not None
        ):
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
        # MD 风格 app bar: 整行强调色反白, 与底部提示栏呼应.
        self._add(
            0,
            0,
            pad_text(" " + title, width),
            accent | curses.A_REVERSE | curses.A_BOLD,
            width,
        )
        # 高度 7 时首张卡片末行会被提示栏覆盖, 因此最小高度为 8.
        # 宽度低于最小栏宽时仍按实际宽度画单栏, 不因过窄拒绝渲染.
        if height < MIN_BOARD_HEIGHT or width < 1:
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
        width_label = self.context.get("width", "Width")
        width_token = f"{width_label} {self.column_width}"
        width_token_span = display_width(width_token)
        toolbar_extra = (
            f" | {self.context.get('theme', 'Theme')} {theme_label} | "
            f"{mode} | {self.context.get('updated', 'Updated')} {stamp}"
        )
        preferred_left = max(
            display_width(query_prefix) + 4,
            min(12, width // 3),
            width // 3,
        )
        # 栏宽数值优先保留完整可见, 必要时压缩搜索区.
        toolbar_left_width = min(
            preferred_left,
            max(0, width - width_token_span - 1),
        )
        remaining = max(0, width - toolbar_left_width - 1)
        if remaining >= width_token_span:
            toolbar_right = width_token + clip_text(
                toolbar_extra, remaining - width_token_span
            )
        else:
            toolbar_right = clip_text(width_token, remaining)
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
            width,
            len(self.model.states),
            single=self.model.single,
            column_width=self.column_width,
        )
        states = self.model.visible_states(count)
        more_left = self.model.column_offset > 0
        more_right = self.model.column_offset + len(states) < len(self.model.states)
        body_top = BODY_TOP
        body_height = height - body_top - 1
        for index, (state, (x, column_width, separator)) in enumerate(
            zip(states, column_geometry(width, len(states)))
        ):
            if separator:
                divider = self.colors.get("muted", curses.A_DIM)
                for y in range(2, height - 1):
                    self._add(y, x + column_width, self.glyphs["vbar"], divider, 1)
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
        tasks, scroll, capacity = column_task_window(
            self.model, state, body_height
        )
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
        if focused:
            heading_attr = self._highlight_attr(state, curses.A_BOLD)
        else:
            heading_attr = state_color | curses.A_BOLD
        self._add(2, x, pad_text(f" {heading_text}", width), heading_attr, width)
        self._add(
            3,
            x,
            self.glyphs["hbar"] * width,
            self.colors.get("muted", curses.A_DIM),
            width,
        )
        if not tasks:
            empty = self.context.get("empty", "No tasks")
            self._add(body_top, x + 1, empty, curses.A_DIM, max(0, width - 2))
            return

        content_width = max(1, width - 2)
        # 紧凑卡片: 3 行内容 (标题/ID/元信息) 加 1 行空行间隔.
        for row, task in enumerate(tasks[scroll : scroll + capacity]):
            y = body_top + row * CARD_HEIGHT
            selected = focused and str(task.get("task_id") or "") == str(
                self.model.selected_ids.get(state) or ""
            )
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
            dot = self.glyphs["dot"]
            meta_head = str(group_or_type)
            meta_time = compact_time(str(task.get("time") or "-"))
            meta_tail = f" {dot} {assignee} {dot} {meta_time}"
            highlight = self._highlight_attr(state, curses.A_BOLD)
            lines = (
                (str(task.get("title") or task.get("task_id") or ""), curses.A_BOLD),
                (str(task.get("task_id") or ""), self.colors.get("id", 0)),
            )
            for offset, (line, attr) in enumerate(lines):
                if selected:
                    # 整卡反色加粗, 彩底选中比只反色状态色更易辨认.
                    attr = highlight
                self._add(
                    y + offset,
                    x + 1,
                    pad_text(line, content_width),
                    attr,
                    content_width,
                )
            # 元信息行: 任务组/类型保持色相, 负责人和时间弱化显示.
            meta_y = y + 2
            if selected:
                self._add(
                    meta_y,
                    x + 1,
                    pad_text(meta_head + meta_tail, content_width),
                    highlight,
                    content_width,
                )
            else:
                head = clip_text(meta_head, content_width)
                self._add(meta_y, x + 1, head, self.colors.get("group", 0))
                used = display_width(head)
                if used < content_width:
                    self._add(
                        meta_y,
                        x + 1 + used,
                        meta_tail,
                        curses.A_DIM,
                        content_width - used,
                    )
            if selected:
                # 左侧留 1 列: 选中时画靠左竖线, 未选中保持空白.
                for offset in range(3):
                    self._add(
                        y + offset,
                        x,
                        self.glyphs["bar"],
                        state_color | curses.A_BOLD,
                        1,
                    )

    def _footer_attr(self, error: bool = False) -> int:
        # 提示栏用强调色反白, 与状态色的选中卡和栏目高亮区分.
        if error:
            return self.colors.get("error", 0) | curses.A_REVERSE | curses.A_BOLD
        return self.colors.get("accent", 0) | curses.A_REVERSE

    def _status_error(self) -> str:
        # 看板/详情错误优先, 避免长偏好路径把刷新失败裁掉.
        parts = [part for part in (self.model.error, self.prefs_error) if part]
        return " | ".join(parts)

    def _render_footer(self, height: int, width: int) -> None:
        status_error = self._status_error()
        if status_error:
            footer = f"{self.context.get('error', 'Error')}: {status_error}"
        elif self.searching:
            footer = self.context.get("search_help", "Enter apply | Esc clear")
        else:
            footer = self.context.get(
                "help",
                "arrows/hjkl move | -/= width | / search | Enter detail | a archive | r refresh | q quit",
            )
        self._add(
            height - 1,
            0,
            pad_text(" " + footer, width),
            self._footer_attr(bool(status_error)),
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
        query_prefix = self.context.get("search", "Search") + ": "
        if self.detail_searching:
            query_text = self.detail_query
            search_attr = accent | curses.A_BOLD
            self._add(2, 0, query_prefix + query_text, search_attr, width)
            self.cursor_pos = (
                2,
                min(width - 1, display_width(query_prefix + query_text)),
            )
        else:
            self._add(2, 0, self.glyphs["hbar"] * width, curses.A_DIM, width)
        body_height = height - 4
        lines = wrap_text(str(task.get("document") or ""), max(1, width - 1))
        matches = self._detail_matches(lines)
        if matches:
            self.detail_match_index = min(self.detail_match_index, len(matches) - 1)
        else:
            self.detail_match_index = 0
        current_match_line = (
            matches[self.detail_match_index] if matches else None
        )
        maximum_scroll = max(0, len(lines) - body_height)
        self.detail_scroll = max(0, min(self.detail_scroll, maximum_scroll))
        visible_lines = lines[
            self.detail_scroll : self.detail_scroll + body_height
        ]
        for index, line in enumerate(visible_lines):
            line_index = self.detail_scroll + index
            stripped = line.lstrip()
            if stripped.startswith("#"):
                base_attr = accent | curses.A_BOLD
            elif stripped.startswith(">"):
                base_attr = curses.A_DIM
            else:
                base_attr = 0
            spans = (
                match_spans(line, self.detail_query)
                if self.detail_query.strip()
                else []
            )
            if not spans:
                self._add(3 + index, 0, line, base_attr, width - 1)
                continue
            current = line_index == current_match_line
            match_attr = (
                (accent | curses.A_REVERSE | curses.A_BOLD)
                if current
                else (curses.A_REVERSE | curses.A_BOLD)
            )
            cursor = 0
            column = 0
            for start, end in spans:
                if start > cursor:
                    chunk = line[cursor:start]
                    self._add(3 + index, column, chunk, base_attr, width - 1 - column)
                    column += display_width(chunk)
                chunk = line[start:end]
                self._add(3 + index, column, chunk, match_attr, width - 1 - column)
                column += display_width(chunk)
                cursor = end
            if cursor < len(line):
                chunk = line[cursor:]
                self._add(3 + index, column, chunk, base_attr, width - 1 - column)
        visible_end = min(len(lines), self.detail_scroll + body_height)
        position = f"{self.detail_scroll + 1}-{visible_end}/{len(lines)}"
        status_error = self._status_error()
        if status_error:
            footer = f"{self.context.get('error', 'Error')}: {status_error}"
        elif self.detail_searching:
            footer = self.context.get("search_help", "Enter apply | Esc clear")
        else:
            help_text = self.context.get(
                "detail_help",
                "jk scroll | Ctrl-d/u half | Ctrl-f/b page | gg/G | / n N | q/Esc back",
            )
            if self.detail_query.strip():
                if matches:
                    match_info = f"{self.detail_match_index + 1}/{len(matches)}"
                else:
                    match_info = self.context.get("no_match", "no match")
                footer = f"{help_text} | {match_info} | {position}"
            else:
                footer = f"{help_text} | {position}"
        self._add(
            height - 1,
            0,
            pad_text(" " + footer, width),
            self._footer_attr(bool(status_error)),
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
    column_width: Optional[int] = None,
    persist_column_width: Optional[Callable[[int], None]] = None,
) -> None:
    if theme not in THEMES:
        raise KanbanTuiError(f"unknown theme: {theme}")
    preferred_width = (
        DEFAULT_COLUMN_WIDTH if column_width is None else clamp_column_width(column_width)
    )

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
            column_width=preferred_width,
            persist_column_width=persist_column_width,
        ).run(initial_board)

    try:
        curses.wrapper(start)
    except KeyboardInterrupt:
        return
    except (curses.error, ValueError) as error:
        raise KanbanTuiError(f"failed to initialize terminal: {error}") from error
