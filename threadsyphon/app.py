from __future__ import annotations

from collections import deque
from pathlib import Path
import queue
import subprocess
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Pango", "1.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from . import __version__
from .catalog import CatalogClient, CatalogThread, search_catalog
from .engine import WatchManager, format_bytes
from .models import (
    FILENAME_MODES,
    MEDIA_FILTERS,
    AppSettings,
    ThreadConfig,
    WatchRule,
    default_download_dir,
    default_root,
    parse_thread_url,
)
from .query import parse_query
from .scout import RuleScout
from .storage import ConfigStore

try:
    gi.require_version("Notify", "0.7")
    from gi.repository import Notify

    Notify.init("threadsyphon")
    HAS_NOTIFY = True
except (ValueError, ImportError):
    HAS_NOTIFY = False


ACTIVE = {"Downloading", "Watching", "Checking", "Starting", "Retrying", "Verifying"}
CSS_PATH = Path(__file__).with_name("style.css")


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / relative


def load_css() -> None:
    if not CSS_PATH.is_file():
        return
    provider = Gtk.CssProvider()
    provider.load_from_path(str(CSS_PATH))
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(),
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )


def open_path(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    Gio.AppInfo.launch_default_for_uri(path.resolve().as_uri(), None)


def notify(title: str, body: str) -> None:
    if not HAS_NOTIFY:
        return
    try:
        n = Notify.Notification.new(title, body, "threadsyphon")
        n.show()
    except Exception:
        pass


class ThreadRow(Gtk.ListBoxRow):
    def __init__(self, config: ThreadConfig) -> None:
        super().__init__()
        self.config_id = config.id
        self.add_css_class("thread-row")
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(10)
        box.set_margin_bottom(10)

        self.dot = Gtk.Box()
        self.dot.add_css_class("status-dot")
        self.dot.set_valign(Gtk.Align.CENTER)
        box.append(self.dot)

        mid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        mid.set_hexpand(True)
        self.title = Gtk.Label(xalign=0)
        self.title.add_css_class("heading")
        self.title.set_ellipsize(Pango.EllipsizeMode.END)
        self.subtitle = Gtk.Label(xalign=0)
        self.subtitle.add_css_class("dim-label")
        self.subtitle.add_css_class("caption")
        self.subtitle.set_ellipsize(Pango.EllipsizeMode.END)
        mid.append(self.title)
        mid.append(self.subtitle)
        box.append(mid)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        right.set_halign(Gtk.Align.END)
        self.counts = Gtk.Label(xalign=1)
        self.counts.add_css_class("stat-value")
        self.counts.add_css_class("numeric")
        self.next_check = Gtk.Label(xalign=1)
        self.next_check.add_css_class("dim-label")
        self.next_check.add_css_class("caption")
        self.next_check.add_css_class("numeric")
        right.append(self.counts)
        right.append(self.next_check)
        box.append(right)

        self.set_child(box)
        self.refresh(config, {"status": "Ready", "message": "Ready to watch", "downloaded": 0, "known": 0, "next_check": "—"})

    def refresh(self, config: ThreadConfig, state: dict) -> None:
        self.title.set_text(config.display_name)
        status = str(state.get("status", "Ready"))
        message = str(state.get("message", ""))
        self.subtitle.set_text(f"{status} · {message}" if message else status)
        self.counts.set_text(f"{state.get('downloaded', 0)} / {state.get('known', 0)}")
        self.next_check.set_text(str(state.get("next_check", "—")))
        for cls in list(self.dot.get_css_classes()):
            if cls.startswith("status-") is False and cls != "status-dot":
                continue
            if cls != "status-dot":
                self.dot.remove_css_class(cls)
        self.dot.add_css_class(status.lower().replace(" ", "-"))


class PreferencesWindow(Adw.PreferencesWindow):
    def __init__(self, parent: Gtk.Window, settings: AppSettings, on_save) -> None:
        super().__init__(transient_for=parent, title="Preferences")
        self.set_search_enabled(False)
        self.settings = settings
        self.on_save = on_save

        page = Adw.PreferencesPage(title="General")
        self.add(page)

        watch = Adw.PreferencesGroup(title="Watching")
        page.add(watch)

        self.interval = Adw.SpinRow(
            title="Default check interval",
            subtitle="Seconds between checks for new threads",
            adjustment=Gtk.Adjustment(value=settings.default_interval, lower=15, upper=3600, step_increment=5, page_increment=30),
            digits=0,
        )
        watch.add(self.interval)

        self.notifications = Adw.SwitchRow(title="Desktop notifications", subtitle="Notify when new files are saved or a thread finishes")
        self.notifications.set_active(settings.notifications)
        watch.add(self.notifications)

        self.clipboard = Adw.SwitchRow(title="Watch clipboard", subtitle="Offer to add a 4chan thread URL when you copy one")
        self.clipboard.set_active(settings.watch_clipboard)
        watch.add(self.clipboard)

        media = Adw.PreferencesGroup(title="Downloads")
        page.add(media)

        self.root = Adw.EntryRow(title="Default download root")
        self.root.set_text(settings.default_root or str(default_root(settings)))
        media.add(self.root)

        self.filename = Adw.ComboRow(title="Filename mode")
        self.filename.set_model(Gtk.StringList.new(["Original names", "Server names", "Numbered"]))
        self.filename.set_selected({"original": 0, "server": 1, "numbered": 2}.get(settings.filename_mode, 0))
        media.add(self.filename)

        self.filter = Adw.ComboRow(title="Media filter")
        self.filter.set_model(Gtk.StringList.new(["All media", "Images only", "Video only"]))
        self.filter.set_selected({"all": 0, "images": 1, "video": 2}.get(settings.media_filter, 0))
        media.add(self.filter)

        self.max_mb = Adw.SpinRow(
            title="Skip files larger than",
            subtitle="Megabytes · 0 means no limit",
            adjustment=Gtk.Adjustment(value=settings.max_file_mb, lower=0, upper=2048, step_increment=1, page_increment=10),
            digits=0,
        )
        media.add(self.max_mb)

        self.gallery = Adw.SwitchRow(title="Write gallery.html + thread.json")
        self.gallery.set_active(settings.save_gallery)
        media.add(self.gallery)

        self.verify = Adw.SwitchRow(title="Verify MD5 checksums")
        self.verify.set_active(settings.verify_md5)
        media.add(self.verify)

        rate = Adw.PreferencesGroup(title="Rate limits")
        page.add(rate)
        self.api_gap = Adw.SpinRow(
            title="API gap",
            subtitle="Seconds between thread JSON requests",
            adjustment=Gtk.Adjustment(value=settings.rate_gap, lower=0.25, upper=10, step_increment=0.25, page_increment=1),
            digits=2,
        )
        rate.add(self.api_gap)
        self.cdn_gap = Adw.SpinRow(
            title="CDN gap",
            subtitle="Seconds between media downloads",
            adjustment=Gtk.Adjustment(value=settings.cdn_gap, lower=0.05, upper=5, step_increment=0.05, page_increment=0.25),
            digits=2,
        )
        rate.add(self.cdn_gap)

        self.connect("close-request", self._save)

    def _save(self, *_args) -> bool:
        self.settings.default_interval = int(self.interval.get_value())
        self.settings.notifications = self.notifications.get_active()
        self.settings.watch_clipboard = self.clipboard.get_active()
        self.settings.default_root = self.root.get_text().strip()
        self.settings.filename_mode = FILENAME_MODES[self.filename.get_selected()]
        self.settings.media_filter = MEDIA_FILTERS[self.filter.get_selected()]
        self.settings.max_file_mb = int(self.max_mb.get_value())
        self.settings.save_gallery = self.gallery.get_active()
        self.settings.verify_md5 = self.verify.get_active()
        self.settings.rate_gap = float(self.api_gap.get_value())
        self.settings.cdn_gap = float(self.cdn_gap.get_value())
        self.on_save(self.settings)
        return False



class FindResultRow(Gtk.ListBoxRow):
    def __init__(self, hit: CatalogThread) -> None:
        super().__init__()
        self.hit = hit
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_start(10)
        box.set_margin_end(10)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        mid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        mid.set_hexpand(True)
        title = Gtk.Label(xalign=0, label=hit.display_title)
        title.add_css_class("heading")
        title.set_ellipsize(Pango.EllipsizeMode.END)
        sub = Gtk.Label(
            xalign=0,
            label=f"{hit.short_id}  ·  {hit.replies} replies  ·  {hit.images} images",
        )
        sub.add_css_class("dim-label")
        sub.add_css_class("caption")
        mid.append(title)
        mid.append(sub)
        box.append(mid)
        self.set_child(box)


class FindDialog(Adw.Window):
    def __init__(self, parent: "ThreadsyphonWindow") -> None:
        super().__init__(transient_for=parent, title="Find threads", modal=True, default_width=560, default_height=520)
        self.parent_win = parent
        self.catalog = parent.catalog
        self._hits: list[CatalogThread] = []

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(root)
        header = Adw.HeaderBar()
        root.append(header)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.close())
        header.pack_start(close)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        body.set_margin_start(16)
        body.set_margin_end(16)
        body.set_margin_top(8)
        body.set_margin_bottom(16)
        body.set_vexpand(True)
        root.append(body)

        hint = Gtk.Label(
            xalign=0,
            label='Filter: linux  ·  title:"daily" OR body:arch  ·  min_images:10  ·  id:123  ·  -sticky:true',
        )
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        hint.set_wrap(True)
        body.append(hint)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.board = Gtk.Entry(text="g")
        self.board.set_placeholder_text("board")
        self.board.set_width_chars(6)
        row.append(self.board)
        self.query = Gtk.Entry()
        self.query.set_placeholder_text("title, body, id, min_images…")
        self.query.set_hexpand(True)
        self.query.connect("activate", lambda *_: self._search())
        row.append(self.query)
        search_btn = Gtk.Button(label="Search")
        search_btn.add_css_class("suggested-action")
        search_btn.connect("clicked", lambda *_: self._search())
        row.append(search_btn)
        body.append(row)

        self.status = Gtk.Label(xalign=0, label="Enter a board and query, then Search.")
        self.status.add_css_class("dim-label")
        body.append(self.status)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        scroll.set_child(self.list)
        body.append(scroll)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_halign(Gtk.Align.END)
        add_btn = Gtk.Button(label="Add selected")
        add_btn.connect("clicked", lambda *_: self._add_selected())
        actions.append(add_btn)
        body.append(actions)

    def _clear_results(self) -> None:
        while True:
            row = self.list.get_row_at_index(0)
            if row is None:
                break
            self.list.remove(row)
        self._hits = []

    def _search(self) -> None:
        board = self.board.get_text().strip()
        query = self.query.get_text().strip()
        self.status.set_text("Searching…")
        self._clear_results()

        def work() -> None:
            try:
                parse_query(query)
                hits = search_catalog(self.catalog, board, query)
                err = ""
            except Exception as error:
                hits = []
                err = str(error)

            def done() -> bool:
                if err:
                    self.status.set_text(err)
                    return False
                self._hits = hits
                for hit in hits[:200]:
                    self.list.append(FindResultRow(hit))
                self.status.set_text(f"{len(hits)} match(es)" + (" — showing first 200" if len(hits) > 200 else ""))
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, daemon=True).start()

    def _add_selected(self) -> None:
        rows = list(self.list.get_selected_rows())
        if not rows:
            self.status.set_text("Select one or more threads first.")
            return
        added = 0
        for row in rows:
            if isinstance(row, FindResultRow):
                if self.parent_win.add_catalog_hit(row.hit):
                    added += 1
        self.status.set_text(f"Added {added} thread(s).")
        if added:
            self.parent_win._toast(f"Added {added} from Find")


class RuleEditDialog(Adw.Window):
    def __init__(self, parent: Gtk.Window, rule: WatchRule | None, on_save) -> None:
        super().__init__(transient_for=parent, title="Edit rule" if rule else "New rule", modal=True, default_width=460)
        self.on_save = on_save
        self.rule_id = rule.id if rule else ""
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(root)
        header = Adw.HeaderBar()
        root.append(header)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda *_: self._save())
        header.pack_end(save)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.set_margin_start(16)
        body.set_margin_end(16)
        body.set_margin_top(8)
        body.set_margin_bottom(16)
        root.append(body)

        self.name = Gtk.Entry(text=(rule.name if rule else ""), placeholder_text="Name")
        body.append(self.name)
        row = Gtk.Box(spacing=8)
        self.board = Gtk.Entry(text=(rule.board if rule else "g"), placeholder_text="board")
        self.board.set_width_chars(8)
        row.append(self.board)
        self.query = Gtk.Entry(text=(rule.query if rule else ""), placeholder_text="query filter")
        self.query.set_hexpand(True)
        row.append(self.query)
        body.append(row)

        row2 = Gtk.Box(spacing=8)
        row2.append(Gtk.Label(label="Scout every"))
        self.interval = Gtk.SpinButton.new_with_range(60, 3600, 30)
        self.interval.set_value(rule.interval if rule else 120)
        row2.append(self.interval)
        row2.append(Gtk.Label(label="s · max add"))
        self.limit = Gtk.SpinButton.new_with_range(1, 50, 1)
        self.limit.set_value(rule.match_limit if rule else 5)
        row2.append(self.limit)
        body.append(row2)

        self.enabled = Gtk.CheckButton(label="Enabled while app is open")
        self.enabled.set_active(rule.enabled if rule else True)
        body.append(self.enabled)
        self.notify_sw = Gtk.CheckButton(label="Notify on match")
        self.notify_sw.set_active(rule.notify if rule else True)
        body.append(self.notify_sw)
        self.label_prefix = Gtk.Entry(text=(rule.label_prefix if rule else ""), placeholder_text="Optional label prefix")
        body.append(self.label_prefix)
        self.error = Gtk.Label(xalign=0)
        self.error.add_css_class("error")
        body.append(self.error)

    def _save(self) -> None:
        try:
            parse_query(self.query.get_text())
            board = self.board.get_text().strip().lower().lstrip("/")
            if not board:
                raise ValueError("Board is required")
            rule = WatchRule(
                name=self.name.get_text().strip() or f"/{board}/ rule",
                board=board,
                query=self.query.get_text().strip(),
                enabled=self.enabled.get_active(),
                interval=int(self.interval.get_value()),
                match_limit=int(self.limit.get_value()),
                label_prefix=self.label_prefix.get_text().strip(),
                notify=self.notify_sw.get_active(),
                id=self.rule_id,
            )
        except (ValueError, TypeError) as error:
            self.error.set_text(str(error))
            return
        self.on_save(rule)
        self.close()


class RulesWindow(Adw.Window):
    def __init__(self, parent: "ThreadsyphonWindow") -> None:
        super().__init__(transient_for=parent, title="Watchdog rules", modal=False, default_width=520, default_height=420)
        self.parent_win = parent
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(root)
        header = Adw.HeaderBar()
        root.append(header)
        add = Gtk.Button(label="Add rule")
        add.add_css_class("suggested-action")
        add.connect("clicked", lambda *_: self._edit(None))
        header.pack_end(add)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.set_margin_start(12)
        body.set_margin_end(12)
        body.set_margin_top(8)
        body.set_margin_bottom(12)
        body.set_vexpand(True)
        root.append(body)

        note = Gtk.Label(
            xalign=0,
            label="Enabled rules scan the catalog while this app is open and auto-add matching threads.",
        )
        note.add_css_class("dim-label")
        note.set_wrap(True)
        body.append(note)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.NONE)
        scroll.set_child(self.list)
        body.append(scroll)
        self.reload()

    def reload(self) -> None:
        while True:
            row = self.list.get_row_at_index(0)
            if row is None:
                break
            self.list.remove(row)
        for rule in self.parent_win.store.rules:
            self.list.append(self._row(rule))

    def _row(self, rule: WatchRule) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.set_margin_start(10)
        box.set_margin_end(10)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        mid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        mid.set_hexpand(True)
        title = Gtk.Label(xalign=0, label=rule.name)
        title.add_css_class("heading")
        sub = Gtk.Label(
            xalign=0,
            label=f"/{rule.board}/ · {rule.query or '(all)'} · every {rule.interval}s",
        )
        sub.add_css_class("dim-label")
        sub.add_css_class("caption")
        sub.set_ellipsize(Pango.EllipsizeMode.END)
        mid.append(title)
        mid.append(sub)
        box.append(mid)
        enabled = Gtk.Switch()
        enabled.set_active(rule.enabled)
        enabled.set_valign(Gtk.Align.CENTER)

        def on_enable(sw, _pspec, rid=rule.id):
            for r in self.parent_win.store.rules:
                if r.id == rid:
                    r.enabled = sw.get_active()
                    break
            self.parent_win._rules_changed()

        enabled.connect("notify::active", on_enable)
        box.append(enabled)
        edit = Gtk.Button(label="Edit")
        edit.add_css_class("flat")
        edit.connect("clicked", lambda *_ , r=rule: self._edit(r))
        box.append(edit)
        rm = Gtk.Button(label="Remove")
        rm.add_css_class("flat")
        rm.connect("clicked", lambda *_ , rid=rule.id: self._remove(rid))
        box.append(rm)
        row.set_child(box)
        return row

    def _edit(self, rule: WatchRule | None) -> None:
        def on_save(saved: WatchRule) -> None:
            rules = self.parent_win.store.rules
            for i, r in enumerate(rules):
                if r.id == saved.id:
                    rules[i] = saved
                    break
            else:
                rules.append(saved)
            self.parent_win._rules_changed()
            self.reload()

        RuleEditDialog(self, rule, on_save).present()

    def _remove(self, rule_id: str) -> None:
        self.parent_win.store.rules = [r for r in self.parent_win.store.rules if r.id != rule_id]
        self.parent_win._rules_changed()
        self.reload()


class ThreadsyphonWindow(Adw.ApplicationWindow):
    def __init__(self, app: "ThreadsyphonApp") -> None:
        super().__init__(application=app, title="threadsyphon", default_width=980, default_height=680)
        self.set_icon_name("threadsyphon")
        self.app_ref = app
        self.store = app.store
        self.configs: dict[str, ThreadConfig] = {}
        self.states: dict[str, dict] = {}
        self.logs: dict[str, deque[str]] = {}
        self.rows: dict[str, ThreadRow] = {}
        self.events: queue.Queue[dict] = queue.Queue()
        self.manager = WatchManager(self.events.put)
        self.manager.apply_limits(self.store.settings.rate_gap, self.store.settings.cdn_gap)
        self.catalog = CatalogClient()
        self.scout = RuleScout(self.events.put, self.catalog)
        self.scout.set_context(
            lambda: {f"{c.board}/{c.thread_no}" for c in self.configs.values()},
            lambda: self.store.settings,
        )
        self._closing = False
        self._clip_last = ""
        self._rules_window = None

        self._build()
        self._load_configs()
        self.scout.set_rules(list(self.store.rules))
        self.scout.start()
        GLib.timeout_add(100, self._drain_events)
        GLib.timeout_add(1500, self._poll_clipboard)

    def _build(self) -> None:
        toast = Adw.ToastOverlay()
        self.set_content(toast)
        self.toasts = toast

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        toast.set_child(root)

        self.header = Adw.HeaderBar()
        root.append(self.header)

        title = Adw.WindowTitle(title="threadsyphon", subtitle="thread media watcher")
        self.header.set_title_widget(title)

        find_btn = Gtk.Button(label="Find")
        find_btn.connect("clicked", lambda *_: self._open_find())
        self.header.pack_start(find_btn)
        rules_btn = Gtk.Button(label="Rules")
        rules_btn.add_css_class("flat")
        rules_btn.connect("clicked", lambda *_: self._open_rules())
        self.header.pack_start(rules_btn)
        start_all = Gtk.Button(label="Start all")
        start_all.connect("clicked", lambda *_: self._start_all())
        self.header.pack_start(start_all)
        pause_all = Gtk.Button(label="Pause all")
        pause_all.add_css_class("flat")
        pause_all.connect("clicked", lambda *_: self._pause_all())
        self.header.pack_start(pause_all)

        menu_btn = Gtk.MenuButton()
        menu_btn.set_icon_name("open-menu-symbolic")
        menu = Gio.Menu()
        menu.append("Find threads", "app.find")
        menu.append("Watchdog rules", "app.rules")
        menu.append("Preferences", "app.preferences")
        menu.append("About", "app.about")
        menu.append("Quit", "app.quit")
        menu_btn.set_menu_model(menu)
        self.header.pack_end(menu_btn)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        body.set_vexpand(True)
        root.append(body)

        add_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        add_box.set_margin_start(16)
        add_box.set_margin_end(16)
        add_box.set_margin_top(12)
        add_box.set_margin_bottom(8)
        body.append(add_box)

        row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.url = Gtk.Entry()
        self.url.set_placeholder_text("Paste a 4chan thread URL")
        self.url.set_hexpand(True)
        self.url.connect("activate", lambda *_: self._add_thread())
        row1.append(self.url)
        self.folder = Gtk.Entry()
        self.folder.set_placeholder_text("Save folder (optional)")
        self.folder.set_hexpand(True)
        row1.append(self.folder)
        browse = Gtk.Button(label="Browse")
        browse.connect("clicked", lambda *_: self._browse_folder())
        row1.append(browse)
        add_box.append(row1)

        row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.label = Gtk.Entry()
        self.label.set_placeholder_text("Optional label")
        self.label.set_width_chars(18)
        row2.append(self.label)
        self.interval = Gtk.SpinButton.new_with_range(15, 3600, 5)
        self.interval.set_value(self.store.settings.default_interval)
        row2.append(self.interval)
        row2.append(Gtk.Label(label="sec"))
        self.original_names = Gtk.CheckButton(label="Original names")
        self.original_names.set_active(self.store.settings.filename_mode == "original")
        row2.append(self.original_names)
        add_btn = Gtk.Button(label="Add & watch")
        add_btn.add_css_class("suggested-action")
        add_btn.connect("clicked", lambda *_: self._add_thread())
        row2.append(add_btn)
        add_box.append(row2)

        self.summary = Gtk.Label(xalign=0, label="No threads yet")
        self.summary.add_css_class("dim-label")
        self.summary.set_margin_start(16)
        self.summary.set_margin_end(16)
        self.summary.set_margin_bottom(8)
        body.append(self.summary)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_vexpand(True)
        paned.set_wide_handle(True)
        body.append(paned)

        left_scroll = Gtk.ScrolledWindow()
        left_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        left_scroll.set_size_request(340, -1)
        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list.add_css_class("navigation-sidebar")
        self.list.connect("row-selected", lambda *_: self._selection_changed())
        left_scroll.set_child(self.list)
        paned.set_start_child(left_scroll)

        detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        detail.set_margin_start(16)
        detail.set_margin_end(16)
        detail.set_margin_top(12)
        detail.set_margin_bottom(16)
        paned.set_end_child(detail)
        paned.set_position(380)

        self.detail_title = Gtk.Label(xalign=0, label="Select a thread")
        self.detail_title.add_css_class("title-2")
        self.detail_title.set_ellipsize(Pango.EllipsizeMode.END)
        detail.append(self.detail_title)

        self.detail_status = Gtk.Label(xalign=0, label="Live activity appears here.")
        self.detail_status.add_css_class("dim-label")
        self.detail_status.set_wrap(True)
        detail.append(self.detail_status)

        self.progress = Gtk.ProgressBar()
        self.progress.set_show_text(False)
        detail.append(self.progress)

        stats = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        self.stat_saved = Gtk.Label(label="0 saved")
        self.stat_saved.add_css_class("stat-value")
        self.stat_bytes = Gtk.Label(label="0 B")
        self.stat_bytes.add_css_class("stat-value")
        self.stat_next = Gtk.Label(label="Next —")
        self.stat_next.add_css_class("numeric")
        stats.append(self.stat_saved)
        stats.append(self.stat_bytes)
        stats.append(self.stat_next)
        detail.append(stats)

        self.detail_location = Gtk.Label(xalign=0, label="")
        self.detail_location.add_css_class("dim-label")
        self.detail_location.add_css_class("caption")
        self.detail_location.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.detail_location.set_selectable(True)
        detail.append(self.detail_location)

        actions = Gtk.FlowBox()
        actions.set_selection_mode(Gtk.SelectionMode.NONE)
        actions.set_max_children_per_line(3)
        actions.set_homogeneous(True)
        for label, handler in (
            ("Start / resume", self._start_selected),
            ("Pause", self._pause_selected),
            ("Check now", self._check_selected),
            ("Open folder", self._open_selected_folder),
            ("Open gallery", self._open_gallery),
            ("Change folder", self._change_selected_folder),
            ("Remove", self._remove_selected),
        ):
            btn = Gtk.Button(label=label)
            btn.connect("clicked", handler)
            if label == "Remove":
                btn.add_css_class("destructive-action")
            actions.append(btn)
        detail.append(actions)

        detail.append(Gtk.Label(xalign=0, label="Recent activity", css_classes=["heading"]))
        log_scroll = Gtk.ScrolledWindow()
        log_scroll.set_vexpand(True)
        log_scroll.set_min_content_height(160)
        self.log = Gtk.TextView()
        self.log.set_editable(False)
        self.log.set_cursor_visible(False)
        self.log.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log.add_css_class("log-view")
        log_scroll.set_child(self.log)
        detail.append(log_scroll)

        self.connect("close-request", self._on_close)

    def _load_configs(self) -> None:
        for config in self.store.load():
            self._register_config(config, start=config.auto_start)
        self._update_summary()

    def _register_config(self, config: ThreadConfig, start: bool = True) -> None:
        self.configs[config.id] = config
        self.states[config.id] = {
            "status": "Ready",
            "message": "Ready to watch",
            "downloaded": 0,
            "known": 0,
            "next_check": "—",
            "progress": 0,
            "bytes": 0,
        }
        self.logs[config.id] = deque(maxlen=40)
        self.manager.add(config)
        row = ThreadRow(config)
        self.rows[config.id] = row
        self.list.append(row)
        if start:
            self.manager.workers[config.id].start()

    def _selected_id(self) -> str | None:
        row = self.list.get_selected_row()
        return row.config_id if isinstance(row, ThreadRow) else None

    def _toast(self, message: str) -> None:
        self.toasts.add_toast(Adw.Toast.new(message))

    def _error(self, message: str) -> None:
        dialog = Adw.MessageDialog(transient_for=self, heading="Could not add thread", body=message)
        dialog.add_response("ok", "OK")
        dialog.present()

    def _browse_folder(self) -> None:
        dialog = Gtk.FileDialog(title="Choose download folder")
        initial = self.folder.get_text().strip() or str(default_root(self.store.settings))
        dialog.set_initial_folder(Gio.File.new_for_path(initial))

        def done(dlg, result):
            try:
                folder = dlg.select_folder_finish(result)
            except GLib.Error:
                return
            if folder:
                self.folder.set_text(folder.get_path())

        dialog.select_folder(self, None, done)

    def _add_thread(self, url: str | None = None) -> None:
        raw = (url or self.url.get_text()).strip()
        try:
            board, number, canonical = parse_thread_url(raw)
            if any(item.board == board and item.thread_no == number for item in self.configs.values()):
                raise ValueError("That thread is already in the watch list.")
            settings = self.store.settings
            folder = self.folder.get_text().strip() or default_download_dir(board, number, settings)
            mode = "original" if self.original_names.get_active() else settings.filename_mode
            if not self.original_names.get_active() and settings.filename_mode == "original":
                mode = "server"
            config = ThreadConfig(
                canonical,
                board,
                number,
                folder,
                self.label.get_text().strip(),
                interval=int(self.interval.get_value()),
                filename_mode=mode,
                media_filter=settings.media_filter,
                max_file_mb=settings.max_file_mb,
                save_gallery=settings.save_gallery,
                verify_md5=settings.verify_md5,
                auto_start=True,
            )
            self._register_config(config, start=True)
            self._save()
            self.list.select_row(self.rows[config.id])
            self.url.set_text("")
            self.folder.set_text("")
            self.label.set_text("")
            self._update_summary()
            self._toast(f"Watching {config.short_id}")
        except (ValueError, OSError) as error:
            self._error(str(error))

    def _selection_changed(self) -> None:
        config_id = self._selected_id()
        if not config_id or config_id not in self.configs:
            self.detail_title.set_text("Select a thread")
            self.detail_status.set_text("Live activity appears here.")
            self.detail_location.set_text("")
            self.progress.set_fraction(0)
            self.stat_saved.set_text("0 saved")
            self.stat_bytes.set_text("0 B")
            self.stat_next.set_text("Next —")
            self.log.get_buffer().set_text("No activity yet.")
            return
        config = self.configs[config_id]
        state = self.states.get(config_id, {})
        self.detail_title.set_text(config.display_name)
        self.detail_status.set_text(f"{state.get('status', 'Ready')} — {state.get('message', '')}")
        self.detail_location.set_text(config.output_dir)
        self.progress.set_fraction(max(0.0, min(1.0, float(state.get("progress", 0) or 0) / 100.0)))
        self.stat_saved.set_text(f"{state.get('downloaded', 0)} / {state.get('known', 0)} files")
        self.stat_bytes.set_text(format_bytes(int(state.get("bytes", 0) or 0)) or "0 B")
        self.stat_next.set_text(f"Next {state.get('next_check', '—')}")
        lines = list(self.logs.get(config_id, []))
        self.log.get_buffer().set_text("\n".join(reversed(lines)) if lines else "No activity yet.")

    def _drain_events(self) -> bool:
        if self._closing:
            return False
        changed = False
        try:
            while True:
                event = self.events.get_nowait()
                if event.get("type") == "scout":
                    status = str(event.get("status", ""))
                    if status == "match":
                        config = event.get("config")
                        if isinstance(config, ThreadConfig) and self.add_config_from_scout(config):
                            msg = f"Rule {event.get('rule_name', '')}: added {config.short_id}"
                            self._toast(msg)
                            if event.get("notify") and self.store.settings.notifications:
                                notify(str(event.get("rule_name") or "Rule"), f"Watching {config.short_id}")
                        changed = True
                    elif status == "error":
                        self._toast(f"Rule error: {event.get('message', '')}")
                    continue
                config_id = str(event.get("thread_id", ""))
                if config_id not in self.configs:
                    continue
                state = self.states.setdefault(config_id, {})
                old_message, old_status = state.get("message"), state.get("status")
                state.update(event)
                if event.get("subject"):
                    self.configs[config_id].subject = str(event["subject"])
                status = str(state.get("status", "Ready"))
                if config_id in self.rows:
                    self.rows[config_id].refresh(self.configs[config_id], state)
                if event.get("message") and (event.get("message") != old_message or status != old_status):
                    self.logs[config_id].append(f"{event.get('updated', '')}  {status} — {event['message']}")
                if self.store.settings.notifications:
                    new_files = int(event.get("new_files") or 0)
                    if new_files > 0 and status == "Watching":
                        notify(self.configs[config_id].display_name, f"Saved {new_files} new file{'s' if new_files != 1 else ''}")
                    if status == "Complete" and old_status != "Complete":
                        notify(self.configs[config_id].display_name, str(event.get("message", "Finished")))
                changed = True
        except queue.Empty:
            pass
        if changed:
            self._selection_changed()
            self._update_summary()
        return True

    def _poll_clipboard(self) -> bool:
        if self._closing or not self.store.settings.watch_clipboard:
            return True
        display = Gdk.Display.get_default()
        if not display:
            return True
        clipboard = display.get_clipboard()

        def on_text(_clip, result):
            try:
                text = clipboard.read_text_finish(result) or ""
            except GLib.Error:
                return
            text = text.strip()
            if not text or text == self._clip_last:
                return
            self._clip_last = text
            try:
                parse_thread_url(text)
            except ValueError:
                return
            if any(text.rstrip("/") in c.url or c.url in text for c in self.configs.values()):
                return
            dialog = Adw.MessageDialog(
                transient_for=self,
                heading="Add copied thread?",
                body=text,
            )
            dialog.add_response("cancel", "Ignore")
            dialog.add_response("add", "Add & watch")
            dialog.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)
            dialog.connect("response", lambda d, r: self._add_thread(text) if r == "add" else None)
            dialog.present()

        clipboard.read_text_async(None, on_text)
        return True

    def _update_summary(self) -> None:
        total = len(self.configs)
        active = sum(1 for state in self.states.values() if state.get("status") in ACTIVE)
        saved = sum(int(state.get("downloaded", 0) or 0) for state in self.states.values())
        nbytes = sum(int(state.get("bytes", 0) or 0) for state in self.states.values())
        self.summary.set_text(
            f"{total} thread{'s' if total != 1 else ''}  ·  {active} active  ·  {saved} files  ·  {format_bytes(nbytes) or '0 B'}"
        )
        rules_on = sum(1 for r in self.store.rules if r.enabled)
        base = f"{active} active · {saved} files" if total else "thread media watcher"
        if rules_on:
            base += f" · {rules_on} rule{'s' if rules_on != 1 else ''}"
        self.header.get_title_widget().set_subtitle(base)

    def _save(self) -> None:
        try:
            self.store.save(list(self.configs.values()), self.store.settings, self.store.rules)
        except OSError as error:
            self._toast(f"Settings not saved: {error}")

    def _rules_changed(self) -> None:
        self.scout.set_rules(list(self.store.rules))
        self._save()
        self._update_summary()

    def _open_find(self) -> None:
        FindDialog(self).present()

    def _open_rules(self) -> None:
        if self._rules_window is None:
            self._rules_window = RulesWindow(self)
        else:
            self._rules_window.reload()
        self._rules_window.present()

    def add_catalog_hit(self, hit: CatalogThread, label: str = "") -> bool:
        if any(c.board == hit.board and c.thread_no == hit.no for c in self.configs.values()):
            return False
        settings = self.store.settings
        mode = "original" if self.original_names.get_active() else settings.filename_mode
        if not self.original_names.get_active() and settings.filename_mode == "original":
            mode = "server"
        config = ThreadConfig(
            hit.url,
            hit.board,
            hit.no,
            default_download_dir(hit.board, hit.no, settings),
            label,
            subject=hit.title or hit.display_title,
            interval=int(self.interval.get_value()),
            filename_mode=mode,
            media_filter=settings.media_filter,
            max_file_mb=settings.max_file_mb,
            save_gallery=settings.save_gallery,
            verify_md5=settings.verify_md5,
            auto_start=True,
        )
        self._register_config(config, start=True)
        self.scout.mark_seen(hit.board, hit.no)
        self._save()
        self._update_summary()
        return True

    def add_config_from_scout(self, config: ThreadConfig) -> bool:
        if any(c.board == config.board and c.thread_no == config.thread_no for c in self.configs.values()):
            return False
        self._register_config(config, start=True)
        self.scout.mark_seen(config.board, config.thread_no)
        self._save()
        self._update_summary()
        return True

    def apply_settings(self, settings: AppSettings) -> None:
        self.store.settings = settings
        self.manager.apply_limits(settings.rate_gap, settings.cdn_gap)
        self.interval.set_value(settings.default_interval)
        self.original_names.set_active(settings.filename_mode == "original")
        self._save()
        self._toast("Preferences saved")

    def _start_selected(self, *_a) -> None:
        config_id = self._selected_id()
        if config_id:
            self.configs[config_id].auto_start = True
            self.manager.workers[config_id].resume()
            self._save()

    def _pause_selected(self, *_a) -> None:
        config_id = self._selected_id()
        if config_id:
            self.configs[config_id].auto_start = False
            self.manager.workers[config_id].pause()
            self._save()

    def _check_selected(self, *_a) -> None:
        config_id = self._selected_id()
        if config_id:
            self.manager.workers[config_id].check_now()

    def _start_all(self) -> None:
        for config_id, worker in self.manager.workers.items():
            self.configs[config_id].auto_start = True
            worker.resume()
        self._save()

    def _pause_all(self) -> None:
        for config_id, worker in self.manager.workers.items():
            self.configs[config_id].auto_start = False
            worker.pause()
        self._save()

    def _open_selected_folder(self, *_a) -> None:
        config_id = self._selected_id()
        if not config_id:
            return
        try:
            open_path(Path(self.configs[config_id].output_dir))
        except Exception as error:
            self._toast(str(error))

    def _open_gallery(self, *_a) -> None:
        config_id = self._selected_id()
        if not config_id:
            return
        path = Path(self.configs[config_id].output_dir) / "gallery.html"
        if not path.is_file():
            self._toast("No gallery yet — wait for the first successful check")
            return
        Gio.AppInfo.launch_default_for_uri(path.resolve().as_uri(), None)

    def _change_selected_folder(self, *_a) -> None:
        config_id = self._selected_id()
        if not config_id:
            return
        config = self.configs[config_id]
        dialog = Gtk.FileDialog(title="Choose a new download folder")
        dialog.set_initial_folder(Gio.File.new_for_path(config.output_dir))

        def done(dlg, result):
            try:
                folder = dlg.select_folder_finish(result)
            except GLib.Error:
                return
            if not folder:
                return
            chosen = folder.get_path()
            if not chosen or chosen == config.output_dir:
                return
            confirm = Adw.MessageDialog(
                transient_for=self,
                heading="Change folder?",
                body="The watcher will restart in the new folder. Existing files will not be moved.",
            )
            confirm.add_response("cancel", "Cancel")
            confirm.add_response("ok", "Change")
            confirm.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

            def on_resp(_d, response):
                if response != "ok":
                    return
                worker = self.manager.workers[config_id]
                was_running = worker.alive and not worker.paused_event.is_set()
                self.manager.remove(config_id)
                config.output_dir = chosen
                worker = self.manager.add(config)
                if was_running:
                    worker.start()
                self._selection_changed()
                self._save()

            confirm.connect("response", on_resp)
            confirm.present()

        dialog.select_folder(self, None, done)

    def _remove_selected(self, *_a) -> None:
        config_id = self._selected_id()
        if not config_id:
            return
        config = self.configs[config_id]
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Remove watcher?",
            body=f"Stop watching {config.display_name}?\nDownloaded files will be kept.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_resp(_d, response):
            if response != "remove":
                return
            row = self.rows.pop(config_id)
            self.list.remove(row)
            self.manager.remove(config_id)
            self.configs.pop(config_id, None)
            self.states.pop(config_id, None)
            self.logs.pop(config_id, None)
            self._save()
            self._selection_changed()
            self._update_summary()

        dialog.connect("response", on_resp)
        dialog.present()

    def _on_close(self, *_a) -> bool:
        if self._closing:
            return False
        self._closing = True
        self._save()
        self.scout.stop()
        self.manager.stop_all()
        return False


class ThreadsyphonApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id="org.threadsyphon.Threadsyphon", flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.store = ConfigStore()
        self.window: ThreadsyphonWindow | None = None
        self.connect("activate", self.on_activate)

    def on_activate(self, _app) -> None:
        load_css()
        if not self.window:
            self.window = ThreadsyphonWindow(self)
        self.window.present()

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        prefs = Gio.SimpleAction.new("preferences", None)
        prefs.connect("activate", self._preferences)
        self.add_action(prefs)
        find_a = Gio.SimpleAction.new("find", None)
        find_a.connect("activate", lambda *_: self.window and self.window._open_find())
        self.add_action(find_a)
        rules_a = Gio.SimpleAction.new("rules", None)
        rules_a.connect("activate", lambda *_: self.window and self.window._open_rules())
        self.add_action(rules_a)
        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self._about)
        self.add_action(about)
        quit_a = Gio.SimpleAction.new("quit", None)
        quit_a.connect("activate", lambda *_: self.quit())
        self.add_action(quit_a)
        self.set_accels_for_action("app.quit", ["<primary>q"])
        self.set_accels_for_action("app.preferences", ["<primary>comma"])
        self.set_accels_for_action("app.find", ["<primary>f"])
        self.set_accels_for_action("app.rules", ["<primary>r"])

    def _preferences(self, *_a) -> None:
        if not self.window:
            return
        PreferencesWindow(self.window, self.store.settings, self.window.apply_settings).present()

    def _about(self, *_a) -> None:
        dialog = Adw.AboutWindow(
            transient_for=self.window,
            application_name="threadsyphon",
            application_icon="threadsyphon",
            developer_name="threadsyphon contributors",
            version=__version__,
            comments="Watch 4chan threads and save their media. Built for Linux with GTK4 and libadwaita.",
            license_type=Gtk.License.MIT_X11,
            website="https://github.com/cicalooo/threadsyphon-linux",
        )
        dialog.present()


def run() -> int:
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.PREFER_LIGHT)
    # Follow system preference when available.
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.DEFAULT)
    app = ThreadsyphonApp()
    return int(app.run(None))
