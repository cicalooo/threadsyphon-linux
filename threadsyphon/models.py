from __future__ import annotations

from dataclasses import asdict, dataclass, field
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import uuid4


ALLOWED_HOSTS = {
    "boards.4chan.org",
    "boards.4channel.org",
    "www.4chan.org",
    "www.4channel.org",
    "4chan.org",
    "4channel.org",
}

FILENAME_MODES = ("original", "server", "numbered")
MEDIA_FILTERS = ("all", "images", "video")
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
VIDEO_EXTS = {".webm", ".mp4"}


@dataclass(slots=True)
class AppSettings:
    default_interval: int = 30
    notifications: bool = True
    media_filter: str = "all"
    max_file_mb: int = 0
    filename_mode: str = "original"
    save_gallery: bool = True
    verify_md5: bool = True
    rate_gap: float = 1.0
    cdn_gap: float = 0.25
    watch_clipboard: bool = False
    default_root: str = ""

    def __post_init__(self) -> None:
        self.default_interval = max(15, min(int(self.default_interval), 3600))
        self.media_filter = self.media_filter if self.media_filter in MEDIA_FILTERS else "all"
        self.filename_mode = self.filename_mode if self.filename_mode in FILENAME_MODES else "original"
        self.max_file_mb = max(0, int(self.max_file_mb))
        self.rate_gap = max(0.25, min(float(self.rate_gap), 10.0))
        self.cdn_gap = max(0.05, min(float(self.cdn_gap), 5.0))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict | None) -> "AppSettings":
        if not isinstance(value, dict):
            return cls()
        known = {key: value[key] for key in cls.__dataclass_fields__ if key in value}
        return cls(**known)


@dataclass(slots=True)
class ThreadConfig:
    url: str
    board: str
    thread_no: int
    output_dir: str
    label: str = ""
    subject: str = ""
    interval: int = 30
    filename_mode: str = "original"
    media_filter: str = "all"
    max_file_mb: int = 0
    save_gallery: bool = True
    verify_md5: bool = True
    auto_start: bool = True
    id: str = ""
    original_names: bool = field(default=True, repr=False)

    def __post_init__(self) -> None:
        if not self.id:
            self.id = uuid4().hex
        self.interval = max(15, min(int(self.interval), 3600))
        if self.filename_mode not in FILENAME_MODES:
            self.filename_mode = "original" if self.original_names else "server"
        self.media_filter = self.media_filter if self.media_filter in MEDIA_FILTERS else "all"
        self.max_file_mb = max(0, int(self.max_file_mb))

    @property
    def display_name(self) -> str:
        return self.label.strip() or self.subject.strip() or f"/{self.board}/ · {self.thread_no}"

    @property
    def short_id(self) -> str:
        return f"/{self.board}/{self.thread_no}"

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("original_names", None)
        return data

    @classmethod
    def from_dict(cls, value: dict) -> "ThreadConfig":
        filename_mode = value.get("filename_mode")
        if filename_mode not in FILENAME_MODES:
            filename_mode = "original" if value.get("original_names", True) else "server"
        return cls(
            url=str(value["url"]),
            board=str(value["board"]),
            thread_no=int(value["thread_no"]),
            output_dir=str(value["output_dir"]),
            label=str(value.get("label", "")),
            subject=str(value.get("subject", "")),
            interval=int(value.get("interval", 30)),
            filename_mode=str(filename_mode),
            media_filter=str(value.get("media_filter", "all")),
            max_file_mb=int(value.get("max_file_mb", 0) or 0),
            save_gallery=bool(value.get("save_gallery", True)),
            verify_md5=bool(value.get("verify_md5", True)),
            auto_start=bool(value.get("auto_start", True)),
            id=str(value.get("id", "")),
        )


def parse_thread_url(url: str) -> tuple[str, int, str]:
    """Return board, thread number and a normalized canonical URL."""
    raw = url.strip()
    if not raw:
        raise ValueError("Paste a 4chan thread URL.")
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlsplit(raw)
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise ValueError("Only boards.4chan.org and boards.4channel.org thread URLs are supported.")
    match = re.fullmatch(r"/([a-z0-9]+)/thread/(\d+)(?:/[^/]*)?/?", parsed.path, re.IGNORECASE)
    if not match:
        raise ValueError("That does not look like a 4chan thread URL.")
    board = match.group(1).lower()
    thread_no = int(match.group(2))
    canonical = f"https://boards.4chan.org/{board}/thread/{thread_no}"
    return board, thread_no, canonical


def xdg_download_dir() -> Path:
    env = os.environ.get("XDG_DOWNLOAD_DIR")
    if env:
        return Path(env).expanduser()
    dirs_file = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "user-dirs.dirs"
    try:
        for line in dirs_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("XDG_DOWNLOAD_DIR="):
                raw = line.split("=", 1)[1].strip().strip('"')
                return Path(raw.replace("$HOME", str(Path.home()))).expanduser()
    except OSError:
        pass
    return Path.home() / "Downloads"


def default_root(settings: AppSettings | None = None) -> Path:
    if settings and settings.default_root.strip():
        return Path(settings.default_root).expanduser()
    return xdg_download_dir() / "threadsyphon"


def default_download_dir(board: str, thread_no: int, settings: AppSettings | None = None) -> str:
    return str(default_root(settings) / board / str(thread_no))


def want_media(extension: str, media_filter: str) -> bool:
    ext = extension.lower()
    if media_filter == "images":
        return ext in IMAGE_EXTS
    if media_filter == "video":
        return ext in VIDEO_EXTS
    return True


@dataclass(slots=True)
class WatchRule:
    """Catalog scout rule — runs while the GUI is open."""

    name: str
    board: str
    query: str
    enabled: bool = True
    interval: int = 120
    match_limit: int = 5
    thread_interval: int = 0  # 0 = use app default
    label_prefix: str = ""
    notify: bool = True
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = uuid4().hex
        self.board = self.board.strip().lower().lstrip("/")
        self.name = self.name.strip() or f"/{self.board}/ rule"
        self.query = self.query.strip()
        self.interval = max(60, min(int(self.interval), 3600))
        self.match_limit = max(1, min(int(self.match_limit), 50))
        self.thread_interval = max(0, min(int(self.thread_interval or 0), 3600))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> "WatchRule":
        return cls(
            name=str(value.get("name", "")),
            board=str(value.get("board", "")),
            query=str(value.get("query", "")),
            enabled=bool(value.get("enabled", True)),
            interval=int(value.get("interval", 120)),
            match_limit=int(value.get("match_limit", 5)),
            thread_interval=int(value.get("thread_interval", 0) or 0),
            label_prefix=str(value.get("label_prefix", "")),
            notify=bool(value.get("notify", True)),
            id=str(value.get("id", "")),
        )
