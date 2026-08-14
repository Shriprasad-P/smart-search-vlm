#!/usr/bin/env python3
"""Small, dependency-free HTTP gateway for Smart Stack mobile access.

The server intentionally binds to localhost by default. Use Tailscale Serve to
publish it privately to the tailnet instead of exposing it to the local LAN.
"""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from mm_stack.api import chat, photos_list, search
from mm_stack.config import StackConfig
from mm_stack.db import connect_sqlite, ensure_schema


LOG = logging.getLogger("smart_stack.mobile")
MAX_JSON_BODY = 64 * 1024
MAX_TOP_K = 20


class MobileApp:
    """Request-facing facade that keeps HTTP details out of Smart Stack core."""

    def __init__(
        self,
        cfg: StackConfig | None = None,
        *,
        search_fn: Callable[..., dict[str, Any]] = search,
        chat_fn: Callable[..., dict[str, Any]] = chat,
    ) -> None:
        self.cfg = cfg or StackConfig()
        self.search_fn = search_fn
        self.chat_fn = chat_fn
        # MLX/PyTorch model loads are RAM-heavy. Keep inference requests serial.
        self.inference_lock = threading.Lock()
        self.started_at = time.time()

    @staticmethod
    def _top_k(value: Any, default: int) -> int:
        try:
            return min(MAX_TOP_K, max(1, int(value)))
        except (TypeError, ValueError):
            return default

    def health(self) -> dict[str, Any]:
        listing = photos_list(limit=1, offset=0, cfg=self.cfg)
        return {
            "status": "ok",
            "service": "Smart Stack Mobile",
            "total_indexed": listing["total_indexed"],
            "uptime_seconds": round(time.time() - self.started_at, 1),
        }

    def photos(self, query: dict[str, list[str]]) -> dict[str, Any]:
        try:
            limit = min(100, max(1, int((query.get("limit") or [30])[0])))
        except (TypeError, ValueError):
            limit = 30
        try:
            offset = max(0, int((query.get("offset") or [0])[0]))
        except (TypeError, ValueError):
            offset = 0
        return photos_list(
            limit=limit,
            offset=offset,
            include_missing=False,
            check_paths=True,
            cfg=self.cfg,
        )

    def image_path(self, image_id: str) -> Path | None:
        if not image_id or len(image_id) > 128:
            return None
        conn = connect_sqlite(self.cfg)
        ensure_schema(conn)
        try:
            row = conn.execute(
                "SELECT file_path FROM images WHERE id = ? LIMIT 1",
                (image_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        path = Path(str(row["file_path"] or ""))
        return path if path.is_file() else None

    def run_search(self, payload: dict[str, Any]) -> dict[str, Any]:
        query = str(payload.get("query", "")).strip()
        if not query:
            raise ValueError("Enter a search query.")
        mode = str(payload.get("mode", "auto")).strip().lower()
        if mode not in {"auto", "keyword", "semantic"}:
            mode = "auto"
        with self.inference_lock:
            return self.search_fn(
                query=query,
                top_k=self._top_k(payload.get("top_k"), 8),
                mode=mode,
                auto_strategy="hybrid",
                verify=False,
                cfg=self.cfg,
            )

    def run_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        query = str(payload.get("query", "")).strip()
        if not query:
            raise ValueError("Enter a question.")
        history = payload.get("history")
        if not isinstance(history, list):
            history = None
        attached_id = str(payload.get("attached_image_id", "")).strip() or None
        with self.inference_lock:
            return self.chat_fn(
                query=query,
                top_k=self._top_k(payload.get("top_k"), 3),
                cfg=self.cfg,
                attached_image_id=attached_id,
                history=history,
            )


def make_handler(app: MobileApp, static_dir: Path) -> type[BaseHTTPRequestHandler]:
    class MobileHandler(BaseHTTPRequestHandler):
        server_version = "SmartStackMobile/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            LOG.info("%s - %s", self.client_address[0], fmt % args)

        def _security_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; "
                "style-src 'self'; script-src 'self'; connect-src 'self'",
            )

        def _json(self, status: int, data: dict[str, Any]) -> None:
            body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self._security_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: int, message: str) -> None:
            self._json(status, {"error": message})

        def _read_json(self) -> dict[str, Any]:
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
            if content_type != "application/json":
                raise ValueError("Content-Type must be application/json.")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("Invalid Content-Length.") from exc
            if length <= 0 or length > MAX_JSON_BODY:
                raise ValueError("Request body is empty or too large.")
            try:
                payload = json.loads(self.rfile.read(length))
            except json.JSONDecodeError as exc:
                raise ValueError("Invalid JSON body.") from exc
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object.")
            return payload

        def _static(self, filename: str, content_type: str) -> None:
            path = static_dir / filename
            if not path.is_file():
                self._error(HTTPStatus.NOT_FOUND, "Not found.")
                return
            body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self._security_headers()
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _image(self, image_id: str) -> None:
            path = app.image_path(unquote(image_id))
            if path is None:
                self._error(HTTPStatus.NOT_FOUND, "Image is missing from the index or disk.")
                return
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            try:
                size = path.stat().st_size
                self.send_response(HTTPStatus.OK)
                self._security_headers()
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "private, max-age=300")
                self.send_header("Content-Length", str(size))
                self.end_headers()
                with path.open("rb") as handle:
                    while chunk := handle.read(256 * 1024):
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                return
            except OSError:
                if not self.wfile.closed:
                    self._error(HTTPStatus.NOT_FOUND, "Unable to read image.")

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            parsed = urlparse(self.path)
            try:
                if parsed.path in {"/", "/index.html"}:
                    self._static("index.html", "text/html; charset=utf-8")
                elif parsed.path == "/favicon.ico":
                    self.send_response(HTTPStatus.NO_CONTENT)
                    self._security_headers()
                    self.send_header("Cache-Control", "public, max-age=86400")
                    self.end_headers()
                elif parsed.path == "/app.css":
                    self._static("app.css", "text/css; charset=utf-8")
                elif parsed.path == "/app.js":
                    self._static("app.js", "text/javascript; charset=utf-8")
                elif parsed.path == "/api/health":
                    self._json(HTTPStatus.OK, app.health())
                elif parsed.path == "/api/photos":
                    self._json(HTTPStatus.OK, app.photos(parse_qs(parsed.query)))
                elif parsed.path.startswith("/api/image/"):
                    self._image(parsed.path.removeprefix("/api/image/"))
                else:
                    self._error(HTTPStatus.NOT_FOUND, "Not found.")
            except Exception as exc:  # keep server alive during model/DB errors
                LOG.exception("GET %s failed", parsed.path)
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            parsed = urlparse(self.path)
            try:
                payload = self._read_json()
                if parsed.path == "/api/search":
                    self._json(HTTPStatus.OK, app.run_search(payload))
                elif parsed.path == "/api/chat":
                    self._json(HTTPStatus.OK, app.run_chat(payload))
                else:
                    self._error(HTTPStatus.NOT_FOUND, "Not found.")
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            except Exception as exc:  # keep server alive during model errors
                LOG.exception("POST %s failed", parsed.path)
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    return MobileHandler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve Smart Stack's mobile web interface")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: localhost only)")
    parser.add_argument("--port", type=int, default=8787, help="Bind port (default: 8787)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    static_dir = Path(__file__).resolve().parent / "mobile_web"
    app = MobileApp()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app, static_dir))
    LOG.info("Smart Stack Mobile listening at http://%s:%s", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
