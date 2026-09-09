from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
import hashlib
import html
import json
import os
from pathlib import Path
import random
import re
import shutil
import threading
import time
from typing import Callable
import urllib.error
import urllib.request

from .gallery import write_gallery, write_thread_archive
from .models import ThreadConfig, want_media
from .storage import atomic_json_write


USER_AGENT = "threadsyphon/2.0 (+desktop thread archiver; respectful polling)"
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))
}


class DownloadCancelled(Exception):
    pass


class SharedRateLimiter:
    """A process-wide courtesy gap prevents several watchers from bursting together."""

    def __init__(self, gap: float = 1.0) -> None:
        self.gap = gap
        self._lock = threading.Lock()
        self._next = 0.0

    def set_gap(self, gap: float) -> None:
        self.gap = max(0.05, float(gap))

    def wait(self, stop: threading.Event) -> None:
        with self._lock:
            delay = max(0.0, self._next - time.monotonic())
            self._next = max(self._next, time.monotonic()) + self.gap
        if delay and stop.wait(delay):
            raise DownloadCancelled


API_LIMITER = SharedRateLimiter(1.0)
CDN_LIMITER = SharedRateLimiter(0.25)


def safe_filename(value: str, fallback: str = "media") -> str:
    value = html.unescape(value or "")
    value = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "_", value).strip(" .")
    value = re.sub(r"\s+", " ", value)
    if not value:
        value = fallback
    if value.split(".", 1)[0].upper() in WINDOWS_RESERVED:
        value = "_" + value
    return value[:160].rstrip(" .") or fallback


def format_bytes(value: int | None) -> str:
    if value is None:
        return ""
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def file_md5_b64(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode("ascii")


def op_subject(payload: dict) -> str:
    posts = payload.get("posts") or []
    if not posts or not isinstance(posts[0], dict):
        return ""
    sub = html.unescape(str(posts[0].get("sub") or ""))
    return re.sub(r"<[^>]+>", "", sub).strip()


@dataclass(slots=True)
class ThreadResponse:
    payload: dict | None
    etag: str = ""
    last_modified: str = ""
    not_modified: bool = False


class FourChanClient:
    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout

    def fetch_thread(
        self,
        config: ThreadConfig,
        etag: str = "",
        last_modified: str = "",
        stop: threading.Event | None = None,
    ) -> ThreadResponse:
        url = f"https://a.4cdn.org/{config.board}/thread/{config.thread_no}.json"
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        request = urllib.request.Request(url, headers=headers)
        API_LIMITER.wait(stop or threading.Event())
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, dict) or not isinstance(payload.get("posts"), list):
                    raise ValueError("The server returned an unexpected thread response.")
                return ThreadResponse(
                    payload=payload,
                    etag=response.headers.get("ETag", ""),
                    last_modified=response.headers.get("Last-Modified", ""),
                )
        except urllib.error.HTTPError as error:
            if error.code == 304:
                return ThreadResponse(None, etag, last_modified, True)
            raise

    def download(
        self,
        url: str,
        target: Path,
        expected_size: int | None,
        stop: threading.Event,
        paused: threading.Event,
        progress: Callable[[int, int | None], None],
    ) -> int:
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        existing = part.stat().st_size if part.exists() else 0
        if expected_size is not None and existing > expected_size:
            part.unlink(missing_ok=True)
            existing = 0
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        if existing:
            headers["Range"] = f"bytes={existing}-"
        CDN_LIMITER.wait(stop)
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            status = getattr(response, "status", 200)
            append = bool(existing and status == 206)
            completed = existing if append else 0
            mode = "ab" if append else "wb"
            with part.open(mode) as stream:
                while True:
                    if stop.is_set():
                        raise DownloadCancelled
                    while paused.is_set():
                        if stop.wait(0.2):
                            raise DownloadCancelled
                    block = response.read(128 * 1024)
                    if not block:
                        break
                    stream.write(block)
                    completed += len(block)
                    progress(completed, expected_size)
                stream.flush()
                os.fsync(stream.fileno())
        if expected_size is not None and completed != expected_size:
            raise OSError(f"Incomplete download: expected {expected_size} bytes, received {completed}.")
        os.replace(part, target)
        return completed


def media_posts(payload: dict) -> list[dict]:
    posts = []
    for post in payload.get("posts", []):
        if isinstance(post, dict) and "tim" in post and "ext" in post:
            posts.append(post)
    return posts


class Manifest:
    def __init__(self, directory: Path) -> None:
        self.path = directory / ".threadsyphon.json"
        self.media: dict[str, dict] = {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(payload.get("media"), dict):
                self.media = payload["media"]
        except (OSError, ValueError, AttributeError):
            pass

    def save(self) -> None:
        atomic_json_write(self.path, {"version": 1, "media": self.media})

    def filename_for(self, post: dict, mode: str, directory: Path, index: int = 1) -> tuple[str, str]:
        key = f"{post['tim']}{post['ext']}"
        saved = self.media.get(key, {}).get("filename")
        if saved:
            return key, str(saved)
        extension = str(post["ext"])
        if mode == "server":
            proposed = key
        elif mode == "numbered":
            stem = safe_filename(str(post.get("filename", post["tim"])), str(post["tim"]))
            proposed = f"{index:04d}_{stem}{extension}"
        else:
            stem = safe_filename(str(post.get("filename", post["tim"])), str(post["tim"]))
            proposed = stem + extension
        used = {str(item.get("filename", "")).casefold() for item in self.media.values()}
        if proposed.casefold() in used or (directory / proposed).exists():
            proposed = f"{Path(proposed).stem} [{post['tim']}]{extension}"
        self.media[key] = {
            "filename": proposed,
            "size": int(post["fsize"]) if post.get("fsize") is not None else None,
            "url": f"https://i.4cdn.org/{post.get('board', '')}/{key}",
            "md5": post.get("md5"),
        }
        self.save()
        return key, proposed


EventSink = Callable[[dict], None]


class ThreadWorker:
    def __init__(self, config: ThreadConfig, events: EventSink, client: FourChanClient | None = None) -> None:
        self.config = config
        self.events = events
        self.client = client or FourChanClient()
        self.stop_event = threading.Event()
        self.paused_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._status = "Ready"

    @property
    def alive(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self) -> None:
        if self.alive:
            self.paused_event.clear()
            self.wake_event.set()
            return
        self.stop_event.clear()
        self.paused_event.clear()
        self.wake_event.clear()
        self.thread = threading.Thread(target=self._run, name=f"watch-{self.config.thread_no}", daemon=True)
        self.thread.start()

    def pause(self) -> None:
        if self.alive:
            self.paused_event.set()
            self.wake_event.set()
            self._emit("Paused", "Watching is paused")

    def resume(self) -> None:
        if self.alive:
            self.paused_event.clear()
            self.wake_event.set()
        else:
            self.start()

    def check_now(self) -> None:
        if self.alive:
            self.wake_event.set()
        else:
            self.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.paused_event.clear()
        self.wake_event.set()

    def join(self, timeout: float = 2.0) -> None:
        if self.thread:
            self.thread.join(timeout)

    def _emit(self, status: str | None = None, message: str = "", **values: object) -> None:
        if status:
            self._status = status
        event = {
            "thread_id": self.config.id,
            "status": self._status,
            "message": message,
            "updated": datetime.now().strftime("%H:%M:%S"),
        }
        event.update(values)
        self.events(event)

    def _interruptible_wait(self, seconds: float) -> bool:
        end = time.monotonic() + seconds
        while not self.stop_event.is_set():
            if self.paused_event.is_set():
                self._emit("Paused", "Watching is paused", next_check="—")
                if self.wake_event.wait(0.25):
                    self.wake_event.clear()
                continue
            remaining = end - time.monotonic()
            if remaining <= 0:
                return True
            next_check = datetime.fromtimestamp(time.time() + remaining).strftime("%H:%M:%S")
            if self.wake_event.wait(min(1.0, remaining)):
                self.wake_event.clear()
                return True
            self._emit("Watching", "Waiting for the next check", next_check=next_check)
        return False

    def _download_with_retries(self, url: str, target: Path, size: int | None, name: str, index: int, total: int) -> int:
        for attempt in range(1, 5):
            try:
                def report(done: int, expected: int | None) -> None:
                    percent = int(done * 100 / expected) if expected else 0
                    self._emit(
                        "Downloading",
                        f"{name} · {format_bytes(done)} / {format_bytes(expected) or '?'}",
                        current=name,
                        progress=percent,
                        item_index=index,
                        item_total=total,
                    )

                return self.client.download(url, target, size, self.stop_event, self.paused_event, report)
            except DownloadCancelled:
                raise
            except urllib.error.HTTPError as error:
                if error.code not in (408, 425, 429, 500, 502, 503, 504) or attempt == 4:
                    raise
                delay = min(60.0, (2 ** attempt) + random.random() * 2)
                self._emit("Retrying", f"Media server returned {error.code}; retrying in {delay:.0f}s")
            except (OSError, urllib.error.URLError):
                if attempt == 4:
                    raise
                delay = min(60.0, (2 ** attempt) + random.random() * 2)
                self._emit("Retrying", f"Download interrupted; retrying in {delay:.0f}s")
            if not self._interruptible_wait(delay):
                raise DownloadCancelled
        raise OSError("Download retry limit reached")

    def _count_saved(self, manifest: Manifest, directory: Path) -> tuple[int, int]:
        downloaded = 0
        nbytes = 0
        for row in manifest.media.values():
            path = directory / str(row.get("filename", ""))
            if path.is_file():
                downloaded += 1
                nbytes += path.stat().st_size
        return downloaded, nbytes

    def _run(self) -> None:
        directory = Path(self.config.output_dir).expanduser()
        etag = ""
        last_modified = ""
        failures = 0
        missing_count = 0
        downloaded = 0
        known = 0
        nbytes = 0
        try:
            directory.mkdir(parents=True, exist_ok=True)
            manifest = Manifest(directory)
            downloaded, nbytes = self._count_saved(manifest, directory)
            self._emit("Starting", "Preparing watcher", downloaded=downloaded, known=known, bytes=nbytes, output=str(directory))
            while not self.stop_event.is_set():
                while self.paused_event.is_set() and not self.stop_event.is_set():
                    self._emit("Paused", "Watching is paused", downloaded=downloaded, known=known, bytes=nbytes)
                    self.wake_event.wait(0.25)
                    self.wake_event.clear()
                if self.stop_event.is_set():
                    break
                try:
                    self._emit("Checking", "Checking thread for new media", downloaded=downloaded, known=known, bytes=nbytes, progress=0)
                    response = self.client.fetch_thread(self.config, etag, last_modified, self.stop_event)
                    failures = 0
                    missing_count = 0
                    if response.not_modified:
                        self._emit("Watching", "No changes found", downloaded=downloaded, known=known, bytes=nbytes, new_files=0)
                    else:
                        etag, last_modified = response.etag, response.last_modified
                        payload = response.payload or {}
                        subject = op_subject(payload)
                        if subject:
                            self.config.subject = subject
                        posts = [post for post in media_posts(payload) if want_media(str(post.get("ext", "")), self.config.media_filter)]
                        known = len(posts)
                        new_files = 0
                        skipped = 0
                        for index, post in enumerate(posts, 1):
                            post = dict(post)
                            post["board"] = self.config.board
                            key, filename = manifest.filename_for(post, self.config.filename_mode, directory, index)
                            target = directory / filename
                            size = int(post["fsize"]) if post.get("fsize") is not None else None
                            if self.config.max_file_mb and size and size > self.config.max_file_mb * 1024 * 1024:
                                skipped += 1
                                continue
                            if target.is_file() and (size is None or target.stat().st_size == size):
                                continue
                            if size:
                                free = shutil.disk_usage(directory).free
                                if free < size + 8 * 1024 * 1024:
                                    raise OSError("Not enough disk space for the next file.")
                            url = f"https://i.4cdn.org/{self.config.board}/{key}"
                            self._download_with_retries(url, target, size, filename, index, known)
                            expected_md5 = post.get("md5")
                            if self.config.verify_md5 and expected_md5:
                                actual = file_md5_b64(target)
                                if actual != expected_md5:
                                    target.unlink(missing_ok=True)
                                    raise OSError(f"Checksum mismatch for {filename}")
                            manifest.media[key].update(
                                {"size": size, "url": url, "completed": int(time.time()), "md5": expected_md5}
                            )
                            manifest.save()
                            new_files += 1
                            downloaded, nbytes = self._count_saved(manifest, directory)
                            self._emit(
                                "Downloading",
                                f"Saved {filename}",
                                downloaded=downloaded,
                                known=known,
                                bytes=nbytes,
                                progress=100,
                                subject=subject,
                            )
                        if self.config.save_gallery:
                            title = self.config.display_name
                            write_thread_archive(directory, payload, title)
                            write_gallery(directory, manifest.media, title, self.config.board, self.config.thread_no)
                        op = payload.get("posts", [{}])[0] if payload.get("posts") else {}
                        extra = f" · skipped {skipped} over size limit" if skipped else ""
                        if op.get("archived") or op.get("closed"):
                            self._emit(
                                "Complete",
                                f"Thread is closed; all available media is saved{extra}",
                                downloaded=downloaded,
                                known=known,
                                bytes=nbytes,
                                progress=100,
                                new_files=new_files,
                                subject=subject,
                            )
                            return
                        message = "Up to date" if not new_files else f"Saved {new_files} new file{'s' if new_files != 1 else ''}"
                        self._emit(
                            "Watching",
                            message + extra,
                            downloaded=downloaded,
                            known=known,
                            bytes=nbytes,
                            progress=0,
                            new_files=new_files,
                            subject=subject,
                        )
                    self._interruptible_wait(self.config.interval)
                except urllib.error.HTTPError as error:
                    if error.code == 404:
                        missing_count += 1
                        if missing_count >= 3:
                            self._emit("Complete", "Thread is no longer available; watcher finished", downloaded=downloaded, known=known, bytes=nbytes)
                            return
                        delay = min(60, self.config.interval)
                        self._emit("Verifying", f"Thread not found ({missing_count}/3); checking again in {delay}s", downloaded=downloaded, known=known, bytes=nbytes)
                    else:
                        failures += 1
                        delay = min(300, max(10, 2 ** min(failures, 8))) + random.random() * 2
                        self._emit("Retrying", f"Server returned {error.code}; retrying in {delay:.0f}s", downloaded=downloaded, known=known, bytes=nbytes)
                    self._interruptible_wait(delay)
                except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as error:
                    failures += 1
                    delay = min(300, max(5, 2 ** min(failures, 8))) + random.random() * 2
                    self._emit("Retrying", f"{type(error).__name__}: {error} · retrying in {delay:.0f}s", downloaded=downloaded, known=known, bytes=nbytes)
                    self._interruptible_wait(delay)
        except DownloadCancelled:
            pass
        except Exception as error:
            self._emit("Error", f"{type(error).__name__}: {error}", downloaded=downloaded, known=known, bytes=nbytes)
            return
        self._emit("Stopped", "Watcher stopped", downloaded=downloaded, known=known, bytes=nbytes, progress=0)


class WatchManager:
    def __init__(self, events: EventSink) -> None:
        self.events = events
        self.workers: dict[str, ThreadWorker] = {}

    def add(self, config: ThreadConfig) -> ThreadWorker:
        worker = ThreadWorker(config, self.events)
        self.workers[config.id] = worker
        return worker

    def remove(self, config_id: str) -> None:
        worker = self.workers.pop(config_id, None)
        if worker:
            worker.stop()
            worker.join()

    def stop_all(self) -> None:
        for worker in self.workers.values():
            worker.stop()
        for worker in self.workers.values():
            worker.join()

    def apply_limits(self, api_gap: float, cdn_gap: float) -> None:
        API_LIMITER.set_gap(api_gap)
        CDN_LIMITER.set_gap(cdn_gap)
