import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from mm_stack.config import StackConfig
from mm_stack.db import connect_sqlite, ensure_schema, upsert_image_metadata
from mobile_server import MobileApp, make_handler


class MobileServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.static_dir = root / "static"
        self.static_dir.mkdir()
        (self.static_dir / "index.html").write_text("mobile", encoding="utf-8")
        (self.static_dir / "app.css").write_text("", encoding="utf-8")
        (self.static_dir / "app.js").write_text("", encoding="utf-8")
        self.image = root / "photo.jpg"
        self.image.write_bytes(b"image-bytes")
        self.cfg = StackConfig(
            stack_root=root,
            vault_root=root,
            sqlite_path=root / "smart_stack.db",
            lancedb_path=root / "vectors.lance",
            inbox_dir=root / "inbox",
            processed_dir=root / "processed",
            failed_dir=root / "failed",
            media_dir=root / "media",
            preprocessed_dir=root / ".cache/preprocessed",
            text_embed_daemon_autostart=False,
            search_cross_rerank_enabled=False,
        )
        conn = connect_sqlite(self.cfg)
        ensure_schema(conn)
        upsert_image_metadata(
            conn,
            {
                "id": "photo-1",
                "file_path": str(self.image),
                "sha256_hash": "hash-photo-1",
                "width": 1,
                "height": 1,
                "caption": "test photo",
                "summary": "test photo",
                "tags": ["test"],
                "ocr_structured": [],
                "schema_version": self.cfg.schema_version,
                "embedding_model_clip": self.cfg.clip_model_name,
                "embedding_model_text": self.cfg.text_model_name,
                "embedding_dimension_clip": self.cfg.clip_dimension,
                "embedding_dimension_text": self.cfg.text_dimension,
                "embedding_schema_version_clip": self.cfg.clip_schema_version,
                "embedding_schema_version_text": self.cfg.text_schema_version,
            },
        )
        conn.commit()
        conn.close()
        self.search_calls = []
        self.chat_calls = []
        app = MobileApp(
            self.cfg,
            search_fn=lambda **kwargs: self.search_calls.append(kwargs) or {"results": []},
            chat_fn=lambda **kwargs: self.chat_calls.append(kwargs) or {"answer": "ok", "sources": []},
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app, self.static_dir))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=3) as response:
            return response.status, response.headers, response.read()

    def post(self, path, payload):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())

    def test_health_photos_and_indexed_image(self):
        status, headers, body = self.get("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["total_indexed"], 1)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])

        _, _, body = self.get("/api/photos?limit=30")
        self.assertEqual(json.loads(body)["items"][0]["image_id"], "photo-1")
        _, headers, body = self.get("/api/image/photo-1")
        self.assertEqual(headers["Content-Type"], "image/jpeg")
        self.assertEqual(body, b"image-bytes")

    def test_search_and_chat_delegate_to_existing_api_contract(self):
        status, body = self.post("/api/search", {"query": "mountain", "top_k": 999})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"results": []})
        self.assertEqual(self.search_calls[0]["top_k"], 20)

        status, body = self.post("/api/chat", {"query": "what is here?", "history": []})
        self.assertEqual(status, 200)
        self.assertEqual(body["answer"], "ok")
        self.assertEqual(self.chat_calls[0]["query"], "what is here?")

    def test_arbitrary_paths_are_not_exposed(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/api/image/../../etc/passwd")
        self.assertEqual(caught.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
