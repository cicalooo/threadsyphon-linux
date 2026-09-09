from __future__ import annotations

from dataclasses import dataclass
import json
import re
import threading
import time
from typing import Any
import urllib.error
import urllib.request

from .engine import API_LIMITER, USER_AGENT, DownloadCancelled
from .query import strip_html


BOARD_RE = re.compile(r"^[a-z0-9]+$", re.I)


@dataclass(slots=True)
class CatalogThread:
    board: str
    no: int
    title: str
    body: str
    replies: int
    images: int
    sticky: bool
    closed: bool
    time: int
    semantic_url: str = ""

    @property
    def url(self) -> str:
        return f"https://boards.4chan.org/{self.board}/thread/{self.no}"

    @property
    def short_id(self) -> str:
        return f"/{self.board}/{self.no}"

    @property
    def display_title(self) -> str:
        return self.title or (self.body[:80] + ("…" if len(self.body) > 80 else "")) or self.short_id

    def as_match_dict(self) -> dict[str, Any]:
        return {
            "board": self.board,
            "no": self.no,
            "title": self.title,
            "body": self.body,
            "replies": self.replies,
            "images": self.images,
            "sticky": self.sticky,
            "closed": self.closed,
            "time": self.time,
            "semantic_url": self.semantic_url,
        }


class CatalogClient:
    """Fetch and cache board catalogs with a shared rate limit."""

    def __init__(self, ttl: float = 45.0, timeout: float = 20.0) -> None:
        self.ttl = ttl
        self.timeout = timeout
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, list[CatalogThread]]] = {}

    def fetch(self, board: str, stop: threading.Event | None = None, force: bool = False) -> list[CatalogThread]:
        board = board.strip().lower().lstrip("/")
        if not board or not BOARD_RE.fullmatch(board):
            raise ValueError("Board must look like g, wg, or mu.")
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(board)
            if hit and not force and now - hit[0] < self.ttl:
                return list(hit[1])

        url = f"https://a.4cdn.org/{board}/catalog.json"
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        request = urllib.request.Request(url, headers=headers)
        API_LIMITER.wait(stop or threading.Event())
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise ValueError(f"Board /{board}/ was not found.") from error
            raise

        threads = self._parse(board, payload)
        with self._lock:
            self._cache[board] = (time.monotonic(), threads)
        return list(threads)

    @staticmethod
    def _parse(board: str, payload: Any) -> list[CatalogThread]:
        if not isinstance(payload, list):
            raise ValueError("Unexpected catalog response.")
        out: list[CatalogThread] = []
        for page in payload:
            if not isinstance(page, dict):
                continue
            for row in page.get("threads") or []:
                if not isinstance(row, dict) or "no" not in row:
                    continue
                # Skip board index OP-only noise if no images/replies markers? keep all.
                title = strip_html(str(row.get("sub") or ""))
                body = strip_html(str(row.get("com") or ""))
                out.append(
                    CatalogThread(
                        board=board,
                        no=int(row["no"]),
                        title=title,
                        body=body,
                        replies=int(row.get("replies") or 0),
                        images=int(row.get("images") or 0),
                        sticky=bool(row.get("sticky")),
                        closed=bool(row.get("closed")),
                        time=int(row.get("time") or 0),
                        semantic_url=str(row.get("semantic_url") or ""),
                    )
                )
        return out


def search_catalog(
    client: CatalogClient,
    board: str,
    query_text: str,
    stop: threading.Event | None = None,
) -> list[CatalogThread]:
    from .query import filter_threads, parse_query

    q = parse_query(query_text)
    threads = client.fetch(board, stop=stop)
    matched = filter_threads(q, [t.as_match_dict() for t in threads])
    by_no = {t.no: t for t in threads}
    return [by_no[int(m["no"])] for m in matched if int(m["no"]) in by_no]
