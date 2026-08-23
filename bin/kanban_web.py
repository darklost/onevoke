#!/usr/bin/env python3

"""Read-only kanban board web UI served with the Python standard library."""

from __future__ import annotations

import json
import mimetypes
import os
import queue
import re
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from string import Template
from typing import Callable, Optional
from urllib.parse import unquote, urlsplit

from onevoke_config import language_text

BoardPayload = Callable[[], dict]
TaskPayload = Callable[[str], dict]

t = language_text


class KanbanWebError(Exception):
    pass


def resolve_share_dir(explicit: Optional[Path] = None) -> Path:
    if explicit is not None:
        path = explicit.resolve()
        if not path.is_dir():
            raise KanbanWebError(t(f"Web 资源目录不存在: {path}", f"web assets directory not found: {path}"))
        return path
    candidates = []
    env_share = os.environ.get("ONEVOKE_SHARE")
    if env_share:
        candidates.append(Path(env_share) / "kanban-web")
    here = Path(__file__).resolve()
    candidates.extend(
        (
            Path.home() / ".local" / "share" / "onevoke" / "kanban-web",
            here.parent.parent / "share" / "kanban-web",
            here.parent / "share" / "kanban-web",
        )
    )
    for candidate in candidates:
        if candidate.is_dir() and (candidate / "board.html").is_file():
            return candidate
    if os.name == "nt":
        raise KanbanWebError(
            t(
                "未找到 kanban web 资源; 请在 Onevoke 仓库根目录运行 "
                "powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\\install.ps1 "
                "重装, 或设置 ONEVOKE_SHARE",
                "kanban web assets not found; from the Onevoke repository root, run "
                "powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\\install.ps1 "
                "to reinstall, or set ONEVOKE_SHARE",
            )
        )
    raise KanbanWebError(
        t(
            "未找到 kanban web 资源; 请运行 ./install.sh 重装或设置 ONEVOKE_SHARE",
            "kanban web assets not found; reinstall with ./install.sh or set ONEVOKE_SHARE",
        )
    )


def render_board_page(share_dir: Path, context: dict) -> bytes:
    template_path = share_dir / "board.html"
    try:
        source = template_path.read_text(encoding="utf-8")
    except OSError as error:
        raise KanbanWebError(
            t(f"读取 board 模板失败: {error}", f"failed to read board template: {error}")
        ) from error
    try:
        return Template(source).substitute(context).encode("utf-8")
    except KeyError as error:
        raise KanbanWebError(
            t(f"board 模板缺少占位符: {error}", f"board template missing placeholder: {error}")
        ) from error
    except ValueError as error:
        raise KanbanWebError(
            t(f"board 模板无效: {error}", f"invalid board template: {error}")
        ) from error


def json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def board_fingerprint(payload: dict) -> str:
    stable = {key: value for key, value in payload.items() if key != "generated_at"}
    return json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class KanbanWebHandler(BaseHTTPRequestHandler):
    server_version = "KanbanWeb/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: HTTPStatus, payload: dict) -> None:
        self._send(status, json_bytes(payload), "application/json; charset=utf-8")

    def _send_error_message(self, status: HTTPStatus, message: str) -> None:
        self._send_json(status, {"error": message})

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch(include_body=False)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch(include_body=True)

    def _dispatch(self, include_body: bool) -> None:
        del include_body  # body omission is handled in _send via self.command
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        try:
            if path in {"/", "/index.html"}:
                body = render_board_page(self.server.share_dir, self.server.page_context)
                self._send(HTTPStatus.OK, body, "text/html; charset=utf-8")
                return
            if path == "/api/board":
                self._send_json(HTTPStatus.OK, self.server.current_board())
                return
            if path == "/api/events":
                self._serve_events()
                return
            match = re.fullmatch(r"/api/tasks/([^/]+)", path)
            if match:
                task_id = match.group(1)
                self._send_json(HTTPStatus.OK, self.server.get_task(task_id))
                return
            if path.startswith("/static/"):
                self._serve_static(path[len("/static/") :])
                return
        except KanbanWebError as error:
            self._send_error_message(HTTPStatus.BAD_REQUEST, str(error))
            return
        except FileNotFoundError as error:
            self._send_error_message(HTTPStatus.NOT_FOUND, str(error))
            return
        except Exception as error:  # noqa: BLE001 - keep the server alive for later requests
            self._send_error_message(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))
            return
        self._send_error_message(HTTPStatus.NOT_FOUND, self._not_found_message())

    def _not_found_message(self) -> str:
        return self.server.page_context.get("not_found", t("未找到", "not found"))

    def _serve_events(self) -> None:
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        if self.command == "HEAD":
            return

        subscriber = self.server.subscribe()
        try:
            while not self.server.monitor_stopped():
                try:
                    event_name, revision, payload = subscriber.get(timeout=15)
                    data = json_bytes(payload).decode("utf-8")
                    message = (
                        f"id: {revision}\n"
                        f"event: {event_name}\n"
                        f"data: {data}\n\n"
                    ).encode("utf-8")
                except queue.Empty:
                    message = b": keep-alive\n\n"
                self.wfile.write(message)
                self.wfile.flush()
        except OSError:
            pass
        finally:
            self.server.unsubscribe(subscriber)

    def _serve_static(self, relative: str) -> None:
        if not relative or Path(relative).name != relative or ".." in relative:
            raise FileNotFoundError(self._not_found_message())
        path = self.server.share_dir / relative
        if not path.is_file():
            raise FileNotFoundError(self._not_found_message())
        data = path.read_bytes()
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type.endswith("+json") or content_type in {
            "application/javascript",
            "text/javascript",
            "application/json",
        }:
            content_type = f"{content_type}; charset=utf-8"
        self._send(HTTPStatus.OK, data, content_type)


class KanbanWebServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        share_dir: Path,
        page_context: dict,
        get_board: BoardPayload,
        get_task: TaskPayload,
        scan_interval: int = 60,
    ) -> None:
        if scan_interval < 1:
            raise KanbanWebError(t("扫描间隔必须 >= 1 秒", "scan interval must be >= 1 second"))
        super().__init__(server_address, KanbanWebHandler)
        self.share_dir = share_dir
        self.page_context = page_context
        self.get_board = get_board
        self.get_task = get_task
        self.scan_interval = scan_interval
        self._board_payload: Optional[dict] = None
        self._board_error: Optional[str] = None
        self._state_key: Optional[tuple[str, str]] = None
        self._revision = 0
        self._subscribers: set[queue.Queue] = set()
        self._state_lock = threading.Lock()
        self._monitor_stop = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        self._refresh_board(force=True)
        self._monitor_thread = threading.Thread(
            target=self._monitor_board,
            name="kanban-web-monitor",
            daemon=True,
        )
        self._monitor_thread.start()
        try:
            super().serve_forever(poll_interval=poll_interval)
        finally:
            self._monitor_stop.set()
            self._monitor_thread.join(timeout=1)

    def _monitor_board(self) -> None:
        while not self._monitor_stop.wait(self.scan_interval):
            self._refresh_board()

    def _refresh_board(self, *, force: bool = False) -> None:
        try:
            payload = self.get_board()
            state_key = ("board", board_fingerprint(payload))
            event_name = "board"
        except Exception as error:  # noqa: BLE001 - publish scan failures to clients
            payload = {"error": str(error)}
            state_key = ("board-error", str(error))
            event_name = "board-error"

        with self._state_lock:
            changed = force or state_key != self._state_key
            self._state_key = state_key
            if event_name == "board":
                self._board_payload = payload
                self._board_error = None
            else:
                self._board_error = payload["error"]
            if not changed:
                return
            self._revision += 1
            revision = self._revision
            subscribers = tuple(self._subscribers)

        for subscriber in subscribers:
            self._enqueue(subscriber, event_name, revision, payload)

    @staticmethod
    def _enqueue(
        subscriber: queue.Queue,
        event_name: str,
        revision: int,
        payload: dict,
    ) -> None:
        item = (event_name, revision, payload)
        try:
            subscriber.put_nowait(item)
        except queue.Full:
            try:
                subscriber.get_nowait()
            except queue.Empty:
                pass
            subscriber.put_nowait(item)

    def subscribe(self) -> queue.Queue:
        subscriber: queue.Queue = queue.Queue(maxsize=1)
        with self._state_lock:
            revision = self._revision
            if self._board_error is not None:
                initial = ("board-error", revision, {"error": self._board_error})
            elif self._board_payload is not None:
                initial = ("board", revision, self._board_payload)
            else:
                initial = None
            if initial is not None:
                self._enqueue(subscriber, *initial)
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue) -> None:
        with self._state_lock:
            self._subscribers.discard(subscriber)

    def current_board(self) -> dict:
        with self._state_lock:
            error = self._board_error
            payload = self._board_payload
        if error is not None:
            raise KanbanWebError(error)
        if payload is None:
            raise KanbanWebError(t("看板数据不可用", "board data is not available"))
        return payload

    def monitor_stopped(self) -> bool:
        return self._monitor_stop.is_set()


def validate_bind(host: str, port: int) -> None:
    if not host.strip():
        raise KanbanWebError(t("host 不能为空", "host must not be empty"))
    if not (0 < port < 65536):
        raise KanbanWebError(t(f"无效端口: {port}", f"invalid port: {port}"))


def serve(
    *,
    host: str,
    port: int,
    share_dir: Path,
    page_context: dict,
    get_board: BoardPayload,
    get_task: TaskPayload,
    scan_interval: int = 60,
    open_browser: bool = False,
) -> None:
    validate_bind(host, port)
    share_dir = resolve_share_dir(share_dir)
    try:
        server = KanbanWebServer(
            (host, port),
            share_dir,
            page_context,
            get_board,
            get_task,
            scan_interval=scan_interval,
        )
    except OSError as error:
        raise KanbanWebError(
            t(f"绑定 {host}:{port} 失败: {error}", f"failed to bind {host}:{port}: {error}")
        ) from error
    display_host = "localhost" if host in {"0.0.0.0", "::", "[::]"} else host
    if ":" in display_host and not display_host.startswith("["):
        url = f"http://[{display_host}]:{server.server_address[1]}/"
    else:
        url = f"http://{display_host}:{server.server_address[1]}/"
    print(url, flush=True)
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("", flush=True)
    finally:
        server.server_close()


def unused_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])
