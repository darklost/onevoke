#!/usr/bin/env python3

"""Read-only kanban board web UI served with the Python standard library."""

from __future__ import annotations

import json
import mimetypes
import re
import socket
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from string import Template
from typing import Callable, Optional
from urllib.parse import unquote, urlsplit

BoardPayload = Callable[[], dict]
TaskPayload = Callable[[str], dict]


class KanbanWebError(Exception):
    pass


def resolve_share_dir(explicit: Optional[Path] = None) -> Path:
    if explicit is not None:
        path = explicit.resolve()
        if not path.is_dir():
            raise KanbanWebError(f"web assets directory not found: {path}")
        return path
    candidates = []
    env_share = __import__("os").environ.get("ONEVOKE_SHARE")
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
    raise KanbanWebError(
        "kanban web assets not found; reinstall with ./install.sh or set ONEVOKE_SHARE"
    )


def render_board_page(share_dir: Path, context: dict) -> bytes:
    template_path = share_dir / "board.html"
    try:
        source = template_path.read_text(encoding="utf-8")
    except OSError as error:
        raise KanbanWebError(f"failed to read board template: {error}") from error
    try:
        return Template(source).substitute(context).encode("utf-8")
    except KeyError as error:
        raise KanbanWebError(f"board template missing placeholder: {error}") from error
    except ValueError as error:
        raise KanbanWebError(f"invalid board template: {error}") from error


def json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class KanbanWebHandler(BaseHTTPRequestHandler):
    server_version = "KanbanWeb/1.0"

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
                self._send_json(HTTPStatus.OK, self.server.get_board())
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
        except Exception as error:  # noqa: BLE001 - keep the server alive for UI polls
            self._send_error_message(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))
            return
        self._send_error_message(HTTPStatus.NOT_FOUND, "not found")

    def _serve_static(self, relative: str) -> None:
        if not relative or Path(relative).name != relative or ".." in relative:
            raise FileNotFoundError("not found")
        path = self.server.share_dir / relative
        if not path.is_file():
            raise FileNotFoundError("not found")
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

    def __init__(
        self,
        server_address: tuple[str, int],
        share_dir: Path,
        page_context: dict,
        get_board: BoardPayload,
        get_task: TaskPayload,
    ) -> None:
        super().__init__(server_address, KanbanWebHandler)
        self.share_dir = share_dir
        self.page_context = page_context
        self.get_board = get_board
        self.get_task = get_task


def validate_bind(host: str, port: int) -> None:
    if not host.strip():
        raise KanbanWebError("host must not be empty")
    if not (0 < port < 65536):
        raise KanbanWebError(f"invalid port: {port}")


def serve(
    *,
    host: str,
    port: int,
    share_dir: Path,
    page_context: dict,
    get_board: BoardPayload,
    get_task: TaskPayload,
    open_browser: bool = False,
) -> None:
    validate_bind(host, port)
    share_dir = resolve_share_dir(share_dir)
    try:
        server = KanbanWebServer((host, port), share_dir, page_context, get_board, get_task)
    except OSError as error:
        raise KanbanWebError(f"failed to bind {host}:{port}: {error}") from error
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
