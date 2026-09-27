from __future__ import annotations

from dataclasses import replace
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
        self._rules_generation = 0

    def set_context(
        self,
        watched_keys: Callable[[], set[str]],
        settings: Callable[[], AppSettings],
    ) -> None:
        self._watched = watched_keys
        self._settings = settings

    def set_rules(self, rules: list[WatchRule]) -> None:
        with self._lock:
            self._rules_generation += 1
            self.rules = {r.id: replace(r) for r in rules}
        self.wake_event.set()

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            self.wake_event.set()
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="rule-scout", daemon=True)
        self.thread.start()

    def stop(self) -> bool:
        self.stop_event.set()
        self.wake_event.set()
        if not self.thread:
            return True
        try:
            timeout = max(3.0, float(getattr(self.catalog, "timeout", 20.0)) + 2.0)
        except (TypeError, ValueError, OverflowError):
            timeout = 22.0
        self.thread.join(timeout=timeout)
        return not self.thread.is_alive()

    def mark_seen(self, board: str, thread_no: int) -> None:
        with self._lock:
            self._seen.add(f"{board}/{thread_no}")

    def is_rule_generation_current(self, rule_id: str, generation: int) -> bool:
        with self._lock:
            rule = self.rules.get(rule_id)
            return bool(rule and rule.enabled and generation == self._rules_generation)

    def _rule_is_current(self, rule_id: str, generation: int | None) -> bool:
        with self._lock:
            rule = self.rules.get(rule_id)
            return bool(
                rule
                and rule.enabled
                and (generation is None or generation == self._rules_generation)
            )

    def _emit(self, **values: object) -> None:
        event = {
            "type": "scout",
            "updated": datetime.now().strftime("%H:%M:%S"),
        }
        event.update(values)
        self.events(event)

    def scan_rule(
        self,
        rule: WatchRule,
        *,
        enforce_live: bool = False,
        generation: int | None = None,
    ) -> tuple[int, int]:
        """Scan one rule synchronously; the GUI and tests can reuse this seam."""
        if enforce_live and not self._rule_is_current(rule.id, generation):
            return 0, 0
        parse_query(rule.query)
        hits = search_catalog(self.catalog, rule.board, rule.query, self.stop_event)
        watched = self._watched()
        settings = self._settings()
        added = 0
        for hit in hits:
            if self.stop_event.is_set():
                break
            if enforce_live and not self._rule_is_current(rule.id, generation):
                return len(hits), added
            key = f"{hit.board}/{hit.no}"
            if key in watched or added >= rule.match_limit:
                continue
            with self._lock:
                current = self.rules.get(rule.id) if enforce_live else rule
                if enforce_live and (
                    current is None
                    or not current.enabled
                    or (generation is not None and generation != self._rules_generation)
                ):
                    return len(hits), added
                if key in self._seen:
                    continue
                self._seen.add(key)
            prefix = rule.label_prefix.strip()
            label = f"{prefix} {hit.display_title}".strip() if prefix else ""
            config = ThreadConfig(
                url=hit.url,
                board=hit.board,
                thread_no=hit.no,
                output_dir=default_download_dir(hit.board, hit.no, settings),
                label=label,
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
                rule_generation=generation,
            )
            added += 1
        if enforce_live and not self._rule_is_current(rule.id, generation):
            return len(hits), added
        self._emit(
            status="scanned",
            rule_id=rule.id,
            rule_name=rule.name,
            message=f"Scanned /{rule.board}/ · {len(hits)} match(es) · added {added}",
            matches=len(hits),
            added=added,
            rule_generation=generation,
        )
        return len(hits), added

    def _run(self) -> None:
        next_due: dict[str, float] = {}
        while not self.stop_event.is_set():
            now = time.monotonic()
            with self._lock:
                generation = self._rules_generation
                rules = [(replace(r), generation) for r in self.rules.values() if r.enabled]
            if not rules:
                # No enabled rules: sleep longer to avoid needless wakeups while idle.
                self.wake_event.wait(5.0)
                self.wake_event.clear()
                continue
            for rule, generation in rules:
                if self.stop_event.is_set():
                    break
                due = next_due.get(rule.id, 0.0)
                if now < due:
                    continue
                try:
                    self.scan_rule(rule, enforce_live=True, generation=generation)
                except Exception as error:
                    if self.stop_event.is_set():
                        break
                    self._emit(status="error", rule_id=rule.id, message=str(error), rule_generation=generation)
                    next_due[rule.id] = now + max(60, rule.interval)
                    continue
                next_due[rule.id] = now + max(60, rule.interval)
            # sleep until nearest due or wake
            with self._lock:
                enabled = [r for r in self.rules.values() if r.enabled]
            if not enabled:
                wait = 5.0
            else:
                soonest = min(next_due.get(r.id, now) for r in enabled)
                wait = max(0.5, min(30.0, soonest - time.monotonic()))
            self.wake_event.wait(wait)
            self.wake_event.clear()
