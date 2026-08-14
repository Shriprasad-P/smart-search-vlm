#!/usr/bin/env python3
"""Small, dependency-free HTTP gateway for Smart Stack mobile access.

The server intentionally binds to localhost by default. Use Tailscale Serve to
publish it privately to the tailnet instead of exposing it to the local LAN.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from mm_stack.api import chat, photos_list, search
from mm_stack.config import StackConfig
from mm_stack.db import connect_sqlite, ensure_schema
from mm_stack.ingestion import MultimodalIngestor


LOG = logging.getLogger("smart_stack.mobile")
MAX_JSON_BODY = 64 * 1024
MAX_TOP_K = 20
MAX_UPLOAD_BODY = 80 * 1024 * 1024
MAX_UPLOAD_FILE = 25 * 1024 * 1024
MAX_UPLOAD_FILES = 10
MAX_IMAGE_PIXELS = 40_000_000
IMAGE_FORMAT_EXTENSIONS = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
    "BMP": ".bmp",
    "TIFF": ".tiff",
    "HEIC": ".heic",
    "HEIF": ".heif",
}


def _ingest_paths(paths: list[Path], cfg: StackConfig) -> dict[str, Any]:
    ingestor = MultimodalIngestor(cfg, image_batch_size=max(1, len(paths)))
    return ingestor.ingest_batch(paths, safe_reprocess=False)


class MobileApp:
    """Request-facing facade that keeps HTTP details out of Smart Stack core."""

    def __init__(
        self,
        cfg: StackConfig | None = None,
        *,
        search_fn: Callable[..., dict[str, Any]] = search,
        chat_fn: Callable[..., dict[str, Any]] = chat,
        ingest_fn: Callable[[list[Path], StackConfig], dict[str, Any]] = _ingest_paths,
    ) -> None:
        self.cfg = cfg or StackConfig()
        self.search_fn = search_fn
        self.chat_fn = chat_fn
        self.ingest_fn = ingest_fn
        # MLX/PyTorch model loads are RAM-heavy. Keep inference requests serial.
        self.inference_lock = threading.Lock()
        self.preview_lock = threading.Lock()
        self.started_at = time.time()

    @staticmethod
    def _validated_image(data: bytes) -> tuple[str, int, int]:
        if not data:
            raise ValueError("The selected image is empty.")
        if len(data) > MAX_UPLOAD_FILE:
            raise ValueError("Each image must be 25 MB or smaller.")
        try:
            from PIL import Image

            with Image.open(io.BytesIO(data)) as image:
                image_format = str(image.format or "").upper()
                width, height = image.size
                if image_format not in IMAGE_FORMAT_EXTENSIONS:
                    raise ValueError(f"Unsupported image format: {image_format or 'unknown'}.")
                if width <= 0 or height <= 0 or (width * height) > MAX_IMAGE_PIXELS:
                    raise ValueError("Image dimensions are invalid or too large.")
                image.verify()
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("The selected file is not a readable image.") from exc
        return IMAGE_FORMAT_EXTENSIONS[image_format], int(width), int(height)

    @staticmethod
    def _safe_stem(filename: str) -> str:
        stem = Path(filename or "mobile-photo").stem
        stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-_")
        return (stem or "mobile-photo")[:48]

    def _uploaded_items(self, paths: list[Path]) -> list[dict[str, Any]]:
        if not paths:
            return []
        conn = connect_sqlite(self.cfg)
        ensure_schema(conn)
        placeholders = ",".join("?" for _ in paths)
        try:
            rows = conn.execute(
                f"""
                SELECT id, file_path, caption, summary, tags, created_at
                FROM images
                WHERE file_path IN ({placeholders})
                """,
                [str(path) for path in paths],
            ).fetchall()
        finally:
            conn.close()
        items: list[dict[str, Any]] = []
        for row in rows:
            try:
                tags = json.loads(str(row["tags"] or "[]"))
                if not isinstance(tags, list):
                    tags = []
            except Exception:
                tags = []
            items.append(
                {
                    "image_id": str(row["id"]),
                    "file_path": str(row["file_path"]),
                    "caption": str(row["caption"] or ""),
                    "summary": str(row["summary"] or ""),
                    "tags": [str(tag) for tag in tags],
                    "created_at": str(row["created_at"] or ""),
                    "exists_on_disk": True,
                }
            )
        return items

    def ingest_uploads(self, uploads: list[tuple[str, str, bytes]]) -> dict[str, Any]:
        if not uploads:
            raise ValueError("Choose at least one image.")
        if len(uploads) > MAX_UPLOAD_FILES:
            raise ValueError(f"Select no more than {MAX_UPLOAD_FILES} images at once.")

        validated: list[tuple[str, str, bytes, str, int, int]] = []
        for filename, content_type, data in uploads:
            extension, width, height = self._validated_image(data)
            validated.append((filename, content_type, data, extension, width, height))

        capture_dir = self.cfg.vault_root / "PhoneCaptures"
        capture_dir.mkdir(parents=True, exist_ok=True)
        saved_paths: list[Path] = []
        temporary_paths: list[Path] = []
        dimensions: list[dict[str, int]] = []
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        try:
            for filename, _content_type, data, extension, width, height in validated:
                destination = capture_dir / (
                    f"{timestamp}-{self._safe_stem(filename)}-{uuid.uuid4().hex[:10]}{extension}"
                )
                temporary = destination.with_suffix(destination.suffix + ".part")
                temporary_paths.append(temporary)
                temporary.write_bytes(data)
                temporary.replace(destination)
                temporary_paths.remove(temporary)
                saved_paths.append(destination)
                dimensions.append({"width": width, "height": height})
        except Exception:
            for path in temporary_paths:
                path.unlink(missing_ok=True)
            for path in saved_paths:
                path.unlink(missing_ok=True)
            raise

        with self.inference_lock:
            result = self.ingest_fn(saved_paths, self.cfg)

        failures = list(result.get("failed", []))
        ingested = int(result.get("ingested", 0))
        duplicates = int(result.get("skipped_duplicates", 0))
        if failures and not (ingested or duplicates):
            raise RuntimeError(str(failures[0]))
        return {
            "uploaded": len(saved_paths),
            "saved_paths": [str(path) for path in saved_paths],
            "dimensions": dimensions,
            "ingestion": result,
            "items": self._uploaded_items(saved_paths),
        }

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

    def image_preview_path(self, image_id: str, size: str = "thumb") -> Path | None:
        if not image_id or len(image_id) > 128:
            return None
        conn = connect_sqlite(self.cfg)
        ensure_schema(conn)
        try:
            row = conn.execute(
                "SELECT file_path, sha256_hash FROM images WHERE id = ? LIMIT 1",
                (image_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        original = Path(str(row["file_path"] or ""))
        content_hash = str(row["sha256_hash"] or "").strip()
        normalized = self.cfg.preprocessed_dir / f"{content_hash}.jpg"

        preview_size = "full" if size == "full" else "thumb"
        # Ingestion already creates a browser-safe normalized JPEG (max 1024px).
        # Serving it directly avoids serial conversion stalls when the gallery
        # requests many thumbnails at once, and survives a moved original.
        if preview_size == "thumb" and normalized.is_file():
            return normalized

        source = original if original.is_file() else normalized
        if not source.is_file():
            return None
        max_dimension = 1800 if preview_size == "full" else 560
        quality = 90 if preview_size == "full" else 82
        safe_id = re.sub(r"[^A-Za-z0-9_-]+", "-", image_id)[:128]
        preview_dir = self.cfg.vault_root / ".mobile_previews"
        preview_dir.mkdir(parents=True, exist_ok=True)
        preview = preview_dir / f"{safe_id}-{preview_size}.jpg"

        try:
            source_mtime = source.stat().st_mtime_ns
            if preview.is_file() and preview.stat().st_mtime_ns >= source_mtime:
                return preview
        except OSError:
            return None

        with self.preview_lock:
            try:
                if preview.is_file() and preview.stat().st_mtime_ns >= source_mtime:
                    return preview
                from PIL import Image, ImageOps

                with Image.open(source) as opened:
                    image = ImageOps.exif_transpose(opened)
                    if image.mode != "RGB":
                        image = image.convert("RGB")
                    image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
                    temporary = preview.with_suffix(".jpg.part")
                    try:
                        image.save(
                            temporary,
                            format="JPEG",
                            quality=quality,
                            optimize=True,
                            progressive=True,
                        )
                        temporary.replace(preview)
                    finally:
                        temporary.unlink(missing_ok=True)
                return preview
            except Exception:
                LOG.exception("Failed to create mobile preview for %s", source)
                return None

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

        def _read_uploads(self) -> list[tuple[str, str, bytes]]:
            content_type = self.headers.get("Content-Type", "").strip()
            if not content_type.lower().startswith("multipart/form-data"):
                raise ValueError("Content-Type must be multipart/form-data.")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("Invalid Content-Length.") from exc
            if length <= 0 or length > MAX_UPLOAD_BODY:
                raise ValueError("Upload is empty or exceeds the 80 MB request limit.")
            body = self.rfile.read(length)
            header = (
                f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n"
            ).encode("utf-8")
            try:
                message = BytesParser(policy=policy.default).parsebytes(header + body)
            except Exception as exc:
                raise ValueError("Unable to parse the image upload.") from exc
            if not message.is_multipart():
                raise ValueError("Upload does not contain multipart image data.")
            uploads: list[tuple[str, str, bytes]] = []
            for part in message.iter_parts():
                field_name = str(part.get_param("name", header="content-disposition") or "")
                if field_name != "image" or part.get_content_disposition() != "form-data":
                    continue
                filename = str(part.get_filename() or "mobile-photo")
                payload = part.get_payload(decode=True)
                if not isinstance(payload, bytes):
                    continue
                uploads.append((filename, str(part.get_content_type()), payload))
            if not uploads:
                raise ValueError("No image files were found in the upload.")
            if len(uploads) > MAX_UPLOAD_FILES:
                raise ValueError(f"Select no more than {MAX_UPLOAD_FILES} images at once.")
            return uploads

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

        def _image(self, image_id: str, query: dict[str, list[str]]) -> None:
            requested_size = str((query.get("size") or ["thumb"])[0]).lower()
            preview_size = "full" if requested_size == "full" else "thumb"
            path = app.image_preview_path(unquote(image_id), preview_size)
            if path is None:
                self._error(HTTPStatus.NOT_FOUND, "Image is missing from the index or disk.")
                return
            try:
                with path.open("rb") as handle:
                    size = path.stat().st_size
                    self.send_response(HTTPStatus.OK)
                    self._security_headers()
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Cache-Control", "private, max-age=86400")
                    self.send_header("Content-Length", str(size))
                    self.end_headers()
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
                    self._image(
                        parsed.path.removeprefix("/api/image/"),
                        parse_qs(parsed.query),
                    )
                else:
                    self._error(HTTPStatus.NOT_FOUND, "Not found.")
            except Exception as exc:  # keep server alive during model/DB errors
                LOG.exception("GET %s failed", parsed.path)
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/api/ingest":
                    self._json(HTTPStatus.OK, app.ingest_uploads(self._read_uploads()))
                    return
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
