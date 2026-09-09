from __future__ import annotations

from datetime import datetime
import threading
import time
from typing import Callable
from uuid import uuid4

from .catalog import CatalogClient, search_catalog
from .models import AppSettings, ThreadConfig, WatchRule, default_download_dir
from .query import parse_query


EventSink = Callable[[dict], None]


class RuleScout:
    """While the app is open, poll catalogs for enabled rules and emit matches."""

    def __init__(
        self,
        events: EventSink,
        catalog: CatalogClient | None = None,
    ) -> None:
        self.events = events
        self.catalog = catalog or CatalogClient()
        self.rules: dict[str, WatchRule] = {}
        self._seen: set[str] = set()  # board/no ever matched this session
        self._watched: Callable[[], set[str]] = lambda: set()
        self._settings: Callable[[], AppSettings] = AppSettings
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def set_context(
        self,
        watched_keys: Callable[[], set[str]],
        settings: Callable[[], AppSettings],
    ) -> None:
        self._watched = watched_keys
        self._settings = settings

    def set_rules(self, rules: list[WatchRule]) -> None:
        with self._lock:
            self.rules = {r.id: r for r in rules}
        self.wake_event.set()

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            self.wake_event.set()
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="rule-scout", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.wake_event.set()
        if self.thread:
            self.thread.join(timeout=3.0)

    def mark_seen(self, board: str, thread_no: int) -> None:
        self._seen.add(f"{board}/{thread_no}")

    def _emit(self, **values: object) -> None:
        event = {
            "type": "scout",
            "updated": datetime.now().strftime("%H:%M:%S"),
        }
        event.update(values)
        self.events(event)

    def _run(self) -> None:
        next_due: dict[str, float] = {}
        while not self.stop_event.is_set():
            now = time.monotonic()
            with self._lock:
                rules = [r for r in self.rules.values() if r.enabled]
            if not rules:
                self.wake_event.wait(2.0)
                self.wake_event.clear()
                continue
            for rule in rules:
                due = next_due.get(rule.id, 0.0)
                if now < due:
                    continue
                try:
                    parse_query(rule.query)  # validate
                    hits = search_catalog(self.catalog, rule.board, rule.query, self.stop_event)
                except Exception as error:
                    self._emit(status="error", rule_id=rule.id, message=str(error))
                    next_due[rule.id] = now + max(60, rule.interval)
                    continue
                watched = self._watched()
                added = 0
                settings = self._settings()
                for hit in hits:
                    if self.stop_event.is_set():
                        break
                    key = f"{hit.board}/{hit.no}"
                    if key in watched or key in self._seen:
                        continue
                    if added >= rule.match_limit:
                        break
                    self._seen.add(key)
                    folder = default_download_dir(hit.board, hit.no, settings)
                    config = ThreadConfig(
                        url=hit.url,
                        board=hit.board,
                        thread_no=hit.no,
                        output_dir=folder,
                        label=rule.label_prefix.strip(),
                        subject=hit.title or hit.display_title,
                        interval=rule.thread_interval or settings.default_interval,
                        filename_mode=settings.filename_mode,
                        media_filter=settings.media_filter,
                        max_file_mb=settings.max_file_mb,
                        save_gallery=settings.save_gallery,
                        verify_md5=settings.verify_md5,
                        auto_start=True,
                        id=uuid4().hex,
                    )
                    self._emit(
                        status="match",
                        rule_id=rule.id,
                        rule_name=rule.name,
                        message=f"Matched {hit.short_id}",
                        config=config,
                        notify=rule.notify,
                    )
                    added += 1
                next_due[rule.id] = now + max(60, rule.interval)
                self._emit(
                    status="scanned",
                    rule_id=rule.id,
                    rule_name=rule.name,
                    message=f"Scanned /{rule.board}/ · {len(hits)} match(es) · added {added}",
                    matches=len(hits),
                    added=added,
                )
            # sleep until nearest due or wake
            with self._lock:
                enabled = [r for r in self.rules.values() if r.enabled]
            if not enabled:
                wait = 2.0
            else:
                soonest = min(next_due.get(r.id, now) for r in enabled)
                wait = max(0.5, min(30.0, soonest - time.monotonic()))
            self.wake_event.wait(wait)
            self.wake_event.clear()
