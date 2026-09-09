from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import unittest

from threadsyphon.engine import FourChanClient, Manifest, safe_filename
from threadsyphon.gallery import write_gallery
from threadsyphon.models import ThreadConfig, parse_thread_url, want_media
from threadsyphon.storage import ConfigStore


class RangeHandler(BaseHTTPRequestHandler):
    body = b"threadsyphon-test-data-" * 1000

    def do_GET(self) -> None:  # noqa: N802
        start = 0
        requested = self.headers.get("Range", "")
        if requested.startswith("bytes="):
            start = int(requested.removeprefix("bytes=").removesuffix("-"))
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(self.body) - 1}/{len(self.body)}")
        else:
            self.send_response(200)
        data = self.body[start:]
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, _format: str, *_args: object) -> None:
        pass


class CoreTests(unittest.TestCase):
    scratch_root = Path(__file__).parent / "scratch"

    def setUp(self) -> None:
        for name in ("manifest", "store", "download", "gallery"):
            directory = self.scratch_root / name
            directory.mkdir(parents=True, exist_ok=True)
            for item in directory.iterdir():
                if item.is_file():
                    item.unlink()

    def test_parses_and_normalizes_supported_thread_urls(self) -> None:
        board, number, url = parse_thread_url("boards.4channel.org/g/thread/123456/a-title#p1")
        self.assertEqual((board, number), ("g", 123456))
        self.assertEqual(url, "https://boards.4chan.org/g/thread/123456")

    def test_rejects_other_hosts_and_non_thread_paths(self) -> None:
        for value in ("https://example.com/g/thread/123", "https://boards.4chan.org/g/catalog"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_thread_url(value)

    def test_filename_is_safe_on_windows(self) -> None:
        self.assertEqual(safe_filename("CON"), "_CON")
        self.assertEqual(safe_filename("bad<name>: x?.jpg"), "bad_name__ x_.jpg")
        self.assertNotIn("/", safe_filename("one/two"))

    def test_manifest_gives_duplicate_original_names_stable_unique_paths(self) -> None:
        directory = self.scratch_root / "manifest"
        manifest = Manifest(directory)
        first = {"tim": 100, "ext": ".jpg", "filename": "photo", "fsize": 12, "board": "g"}
        second = {"tim": 200, "ext": ".jpg", "filename": "photo", "fsize": 15, "board": "g"}
        self.assertEqual(manifest.filename_for(first, "original", directory)[1], "photo.jpg")
        self.assertEqual(manifest.filename_for(second, "original", directory)[1], "photo [200].jpg")
        reloaded = Manifest(directory)
        self.assertEqual(reloaded.filename_for(first, "original", directory)[1], "photo.jpg")

    def test_numbered_filename_mode(self) -> None:
        directory = self.scratch_root / "manifest"
        manifest = Manifest(directory)
        post = {"tim": 300, "ext": ".webm", "filename": "clip", "fsize": 9, "board": "g"}
        self.assertEqual(manifest.filename_for(post, "numbered", directory, index=7)[1], "0007_clip.webm")

    def test_media_filter(self) -> None:
        self.assertTrue(want_media(".jpg", "images"))
        self.assertFalse(want_media(".webm", "images"))
        self.assertTrue(want_media(".webm", "video"))
        self.assertTrue(want_media(".png", "all"))

    def test_config_store_round_trip_and_corrupt_recovery(self) -> None:
        directory = self.scratch_root / "store"
        path = directory / "threads.json"
        store = ConfigStore(path)
        config = ThreadConfig("https://boards.4chan.org/g/thread/123", "g", 123, str(directory / "files"), "Test")
        store.save([config])
        loaded = store.load()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].id, config.id)
        self.assertEqual(store.settings.default_interval, 30)
        path.write_text("not json", encoding="utf-8")
        self.assertEqual(store.load(), [])

    def test_gallery_writer(self) -> None:
        directory = self.scratch_root / "gallery"
        image = directory / "photo.jpg"
        image.write_bytes(b"fake")
        write_gallery(directory, {"100.jpg": {"filename": "photo.jpg"}}, "Demo", "g", 1)
        html = (directory / "gallery.html").read_text(encoding="utf-8")
        self.assertIn("photo.jpg", html)
        self.assertIn("Demo", html)

    def test_download_resumes_a_partial_file_and_replaces_atomically(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            target = self.scratch_root / "download" / "file.bin"
            part = target.with_name("file.bin.part")
            part.write_bytes(RangeHandler.body[:321])
            progress: list[int] = []
            client = FourChanClient(timeout=2)
            size = client.download(
                f"http://127.0.0.1:{server.server_port}/file.bin",
                target,
                len(RangeHandler.body),
                threading.Event(),
                threading.Event(),
                lambda done, _total: progress.append(done),
            )
            self.assertEqual(size, len(RangeHandler.body))
            self.assertEqual(target.read_bytes(), RangeHandler.body)
            self.assertFalse(part.exists())
            self.assertTrue(progress)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
