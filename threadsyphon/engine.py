from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
import hashlib
import html
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import stat
import threading
import time
from typing import Callable
import urllib.error
import urllib.request
from uuid import uuid4

from .gallery import write_gallery, write_thread_archive
from .models import ThreadConfig, is_safe_relative_filename, want_media
from .storage import atomic_json_write, is_safe_regular_file, load_json_file


USER_AGENT = "threadsyphon/2.0 (+desktop thread archiver; respectful polling)"
FREE_SPACE_RESERVE_BYTES = 8 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_TIM_DIGITS = 20
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))
}


class DownloadCancelled(Exception):
    pass


class DownloadPolicyError(OSError):
    """A permanent local policy failure should not be retried forever."""


class MediaDownloadError(OSError):
    """A media request failed; it is not evidence that the thread is gone."""


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
RATE_LIMITER = API_LIMITER


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


def read_json_response(response, maximum: int | None = None) -> object:
    limit = MAX_JSON_BYTES if maximum is None else maximum
    raw = response.read(limit + 1)
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > limit:
        raise ValueError("The server response is too large to decode safely.")
    try:
        return json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ValueError("The server returned invalid JSON.") from error



def _assert_safe_existing_file(path: Path) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise DownloadPolicyError(f"Cannot inspect unsafe local file: {path.name}") from error
    if not is_safe_regular_file(path):
        raise DownloadPolicyError(f"Refusing to use unsafe local file: {path.name}")


def _open_download_part(path: Path, mode: str):
    _assert_safe_existing_file(path)
    binary = getattr(os, "O_BINARY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    base_flags = os.O_WRONLY | binary | nofollow
    if mode == "wb":
        try:
            descriptor = os.open(str(path), base_flags | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            _assert_safe_existing_file(path)
            descriptor = os.open(str(path), base_flags | os.O_TRUNC)
    elif mode == "ab":
        descriptor = os.open(str(path), base_flags | os.O_APPEND)
    else:
        raise ValueError(f"Unsupported download mode: {mode}")
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DownloadPolicyError(f"Refusing to use unsafe local file: {path.name}")
        return os.fdopen(descriptor, mode)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _valid_content_range(response, offset: int, expected_size: int | None) -> bool:
    headers = getattr(response, "headers", {})
    value = headers.get("Content-Range", "") if hasattr(headers, "get") else ""
    match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", str(value).strip(), re.IGNORECASE)
    if not match:
        return False
    start, end = int(match.group(1)), int(match.group(2))
    total = match.group(3)
    if start != offset or end < start:
        return False
    if total != "*":
        total_size = int(total)
        if total_size <= end or total_size > MAX_DOWNLOAD_BYTES:
            return False
        if expected_size is not None and total_size != expected_size:
            return False
    return True



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
                payload = read_json_response(response)
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
        if expected_size is not None and (expected_size < 0 or expected_size > MAX_DOWNLOAD_BYTES):
            raise DownloadPolicyError("The requested media size exceeds the local download limit.")
        part = target.with_name(target.name + ".part")
        _assert_safe_existing_file(part)
        existing = part.stat().st_size if part.exists() else 0
        if existing > MAX_DOWNLOAD_BYTES:
            part.unlink(missing_ok=True)
            existing = 0
        if expected_size is not None and existing > expected_size:
            part.unlink(missing_ok=True)
            existing = 0
        if expected_size is not None and part.exists() and existing == expected_size:
            os.replace(part, target)
            progress(existing, expected_size)
            return existing

        def consume(response, append: bool, offset: int) -> int:
            completed = offset if append else 0
            mode = "ab" if append else "wb"
            # Recheck free space every ~1 MiB instead of every 128 KiB chunk.
            bytes_since_space_check = 0
            with _open_download_part(part, mode) as stream:
                while True:
                    if stop.is_set():
                        raise DownloadCancelled
                    while paused.is_set():
                        if stop.wait(1.0):
                            raise DownloadCancelled
                    block = response.read(128 * 1024)
                    if not block:
                        break
                    if completed + len(block) > MAX_DOWNLOAD_BYTES:
                        raise DownloadPolicyError("The media response exceeds the local download limit.")
                    if expected_size is not None and completed + len(block) > expected_size:
                        raise DownloadPolicyError("The media response exceeds expected size.")
                    if bytes_since_space_check == 0 or bytes_since_space_check >= 1024 * 1024:
                        if shutil.disk_usage(part.parent).free < len(block) + FREE_SPACE_RESERVE_BYTES:
                            raise DownloadPolicyError("Not enough free space remains for this media file.")
                        bytes_since_space_check = 0
                    stream.write(block)
                    completed += len(block)
                    bytes_since_space_check += len(block)
                    progress(completed, expected_size)
                stream.flush()
                os.fsync(stream.fileno())
            return completed

        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        if existing:
            headers["Range"] = f"bytes={existing}-"
        CDN_LIMITER.wait(stop)
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            status = getattr(response, "status", 200)
            append = bool(existing and status == 206)
            if append and not _valid_content_range(response, existing, expected_size):
                part.unlink(missing_ok=True)
                existing = 0
                headers.pop("Range", None)
                CDN_LIMITER.wait(stop)
                retry_request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(retry_request, timeout=self.timeout) as retry_response:
                    completed = consume(retry_response, False, 0)
            else:
                completed = consume(response, append, existing)
        if expected_size is not None and completed != expected_size:
            raise OSError(f"Incomplete download: expected {expected_size} bytes, received {completed}.")
        os.replace(part, target)
        return completed


def media_key(post: dict) -> tuple[str, str]:
    """Validate API filename metadata before it reaches the filesystem."""
    tim = str(post.get("tim", ""))
    extension = str(post.get("ext", ""))
    if not re.fullmatch(r"[0-9]+", tim) or len(tim) > MAX_TIM_DIGITS or not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", extension):
        raise ValueError("The server returned unsafe media filename metadata.")
    return tim + extension, extension


def _valid_media_size(value) -> bool:
    if value is None or isinstance(value, bool):
        return value is None
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return False
    try:
        size = int(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return 0 <= size <= MAX_DOWNLOAD_BYTES


def media_posts(payload: dict) -> list[dict]:
    posts = []
    raw_posts = payload.get("posts", [])
    if not isinstance(raw_posts, list):
        return posts
    for post in raw_posts:
        if isinstance(post, dict) and "tim" in post and "ext" in post and _valid_media_size(post.get("fsize")):
            try:
                media_key(post)
            except ValueError:
                continue
            posts.append(post)
    return posts


class Manifest:
    def __init__(self, directory: Path) -> None:
        self.path = directory / ".threadsyphon.json"
        self.media: dict[str, dict] = {}
        payload = load_json_file(self.path)
        if isinstance(payload, dict) and isinstance(payload.get("media"), dict):
            self.media = {
                str(key): row
                for key, row in payload["media"].items()
                if isinstance(row, dict) and is_safe_relative_filename(row.get("filename"))
            }

    def save(self) -> None:
        atomic_json_write(self.path, {"version": 1, "media": self.media})

    def filename_for(self, post: dict, mode: str | bool, directory: Path, index: int = 1) -> tuple[str, str]:
        if isinstance(mode, bool):
            mode = "original" if mode else "server"
        key, extension = media_key(post)
        saved = self.media.get(key, {}).get("filename")
        if saved and is_safe_relative_filename(saved):
            return key, str(saved)
        if saved:
            self.media.pop(key, None)
        if mode == "server":
            proposed = key
        elif mode == "numbered":
            stem = safe_filename(str(post.get("filename", post["tim"])), str(post["tim"]))
            proposed = f"{index:04d}_{stem}{extension}"
        else:
            stem = safe_filename(str(post.get("filename", post["tim"])), str(post["tim"]))
            proposed = stem + extension
        if not is_safe_relative_filename(proposed):
            raise ValueError("The server returned unsafe media filename metadata.")
        used = {str(item.get("filename", "")).casefold() for item in self.media.values() if is_safe_relative_filename(item.get("filename"))}
        def taken(candidate: str) -> bool:
            return candidate.casefold() in used or (directory / candidate).exists()
        if taken(proposed):
            stem = Path(proposed).stem
            candidate = f"{stem} [{post['tim']}]{extension}"
            counter = 2
            while taken(candidate):
                candidate = f"{stem} [{post['tim']}] ({counter}){extension}"
                counter += 1
            proposed = candidate
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
        self.worker_id = uuid4().hex
        self.stop_event = threading.Event()
        self.paused_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._status = "Ready"
        self._last_emit_key: tuple | None = None

    @property
    def alive(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self, publish_id: Callable[[str], None] | None = None) -> str:
        if self.alive:
            self.paused_event.clear()
            self.wake_event.set()
            if publish_id is not None:
                publish_id(self.worker_id)
            return self.worker_id
        self.stop_event.clear()
        self.paused_event.clear()
        self.wake_event.clear()
        self.worker_id = uuid4().hex
        if publish_id is not None:
            publish_id(self.worker_id)
        self.thread = threading.Thread(target=self._run, name=f"watch-{self.config.thread_no}", daemon=True)
        self.thread.start()
        return self.worker_id

    def pause(self) -> None:
        if self.alive:
            self.paused_event.set()
            self.wake_event.set()
            self._emit("Paused", "Watching is paused")

    def resume(self, publish_id: Callable[[str], None] | None = None) -> str:
        if self.alive:
            self.paused_event.clear()
            self.wake_event.set()
            if publish_id is not None:
                publish_id(self.worker_id)
            return self.worker_id
        return self.start(publish_id=publish_id)

    def check_now(self, publish_id: Callable[[str], None] | None = None) -> str:
        if self.alive:
            self.wake_event.set()
            if publish_id is not None:
                publish_id(self.worker_id)
            return self.worker_id
        return self.start(publish_id=publish_id)

    def stop(self) -> None:
        self.stop_event.set()
        self.paused_event.clear()
        self.wake_event.set()

    def join(self, timeout: float | None = None) -> bool:
        if not self.thread:
            return True
        if timeout is None:
            timeout = max(2.0, float(getattr(self.client, "timeout", 20.0)) + 2.0)
        self.thread.join(timeout)
        return not self.thread.is_alive()

    def _emit(self, status: str | None = None, message: str = "", **values: object) -> None:
        if status:
            self._status = status
        key = (self._status, message, tuple(sorted((k, repr(v)) for k, v in values.items() if k != "updated")))
        if key == self._last_emit_key:
            return
        self._last_emit_key = key
        event = {
            "thread_id": self.config.id,
            "worker_id": self.worker_id,
            "status": self._status,
            "message": message,
            "updated": datetime.now().strftime("%H:%M:%S"),
        }
        event.update(values)
        self.events(event)

    def _interruptible_wait(self, seconds: float) -> bool:
        end = time.monotonic() + max(0.0, seconds)
        emitted_pause = False
        emitted_watch = False
        while not self.stop_event.is_set():
            if self.paused_event.is_set():
                if not emitted_pause:
                    self._emit("Paused", "Watching is paused", next_check="—")
                    emitted_pause = True
                emitted_watch = False
                if self.wake_event.wait(5.0):
                    self.wake_event.clear()
                continue
            remaining = end - time.monotonic()
            if remaining <= 0:
                return True
            if not emitted_watch:
                next_check = datetime.fromtimestamp(time.time() + remaining).strftime("%H:%M:%S")
                self._emit("Watching", "Waiting for the next check", next_check=next_check)
                emitted_watch = True
            emitted_pause = False
            if self.wake_event.wait(min(5.0, remaining)):
                self.wake_event.clear()
                return True
        return False

    def _download_with_retries(self, url: str, target: Path, size: int | None, name: str, index: int, total: int) -> int:
        for attempt in range(1, 5):
            try:
                last_report_at = 0.0
                last_report_pct = -1

                def report(done: int, expected: int | None) -> None:
                    nonlocal last_report_at, last_report_pct
                    percent = int(done * 100 / expected) if expected else 0
                    now = time.monotonic()
                    # Integer-percent + time throttle: avoid flooding the UI queue on every 128 KiB chunk.
                    if (
                        percent != 100
                        and percent == last_report_pct
                        and (now - last_report_at) < 0.2
                    ):
                        return
                    if (
                        percent != 100
                        and last_report_pct >= 0
                        and percent < last_report_pct + 1
                        and (now - last_report_at) < 0.15
                    ):
                        return
                    last_report_at = now
                    last_report_pct = percent
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
            except DownloadPolicyError:
                raise
            except urllib.error.HTTPError as error:
                if error.code not in (408, 425, 429, 500, 502, 503, 504) or attempt == 4:
                    raise MediaDownloadError(f"Media server returned HTTP {error.code}.") from error
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
            filename = row.get("filename", "")
            if not is_safe_relative_filename(filename):
                continue
            path = directory / filename
            if is_safe_regular_file(path):
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
                if self.paused_event.is_set() and not self.stop_event.is_set():
                    self._emit("Paused", "Watching is paused", downloaded=downloaded, known=known, bytes=nbytes)
                    while self.paused_event.is_set() and not self.stop_event.is_set():
                        self.wake_event.wait(5.0)
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
                            _assert_safe_existing_file(target)
                            size = int(post["fsize"]) if post.get("fsize") is not None else None
                            if self.config.max_file_mb and size and size > self.config.max_file_mb * 1024 * 1024:
                                skipped += 1
                                continue
                            if is_safe_regular_file(target) and (size is None or target.stat().st_size == size):
                                continue
                            if size:
                                free = shutil.disk_usage(directory).free
                                if free < size + FREE_SPACE_RESERVE_BYTES:
                                    raise DownloadPolicyError("Not enough disk space for the next file.")
                            url = f"https://i.4cdn.org/{self.config.board}/{key}"
                            self._download_with_retries(url, target, size, filename, index, known)
                            expected_md5 = post.get("md5")
                            if self.config.verify_md5 and expected_md5:
                                actual = file_md5_b64(target)
                                if actual != expected_md5:
                                    target.unlink(missing_ok=True)
                                    raise DownloadPolicyError(f"Checksum mismatch for {filename}")
                            manifest.media[key].update(
                                {"size": size, "url": url, "completed": int(time.time()), "md5": expected_md5}
                            )
                            manifest.save()
                            new_files += 1
                            # Increment counters instead of restatting every saved file after each download.
                            try:
                                file_bytes = target.stat().st_size
                            except OSError:
                                file_bytes = int(size or 0)
                            downloaded += 1
                            nbytes += file_bytes
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
                        op_posts = payload.get("posts")
                        op = op_posts[0] if isinstance(op_posts, list) and op_posts and isinstance(op_posts[0], dict) else {}
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
                except DownloadPolicyError as error:
                    self._emit(
                        "Error",
                        f"{type(error).__name__}: {error}",
                        downloaded=downloaded,
                        known=known,
                        bytes=nbytes,
                    )
                    return
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
        existing = self.workers.get(config.id)
        if existing is not None and existing.alive:
            raise RuntimeError(f"Watcher {config.id} is still shutting down.")
        worker = ThreadWorker(config, self.events)
        self.workers[config.id] = worker
        return worker

    def remove(self, config_id: str) -> bool:
        worker = self.workers.get(config_id)
        if worker is None:
            return True
        worker.stop()
        if not worker.join():
            return False
        self.workers.pop(config_id, None)
        return True

    def stop_all(self) -> bool:
        for worker in self.workers.values():
            worker.stop()
        finished = True
        for worker in self.workers.values():
            finished = worker.join() and finished
        return finished

    def apply_limits(self, api_gap: float, cdn_gap: float) -> None:
        API_LIMITER.set_gap(api_gap)
        CDN_LIMITER.set_gap(cdn_gap)
