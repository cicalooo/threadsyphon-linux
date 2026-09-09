from __future__ import annotations

import argparse
import queue
import sys
import time

from . import __version__
from .engine import WatchManager, format_bytes
from .catalog import CatalogClient, search_catalog
from .models import ThreadConfig, WatchRule, default_download_dir, parse_thread_url
from .query import parse_query
from .storage import ConfigStore, app_config_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="threadsyphon",
        description="Watch 4chan threads and save their media.",
    )
    parser.add_argument("--version", action="version", version=f"threadsyphon {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("gui", help="Open the desktop app (default)")

    add = sub.add_parser("add", help="Add a thread to the watch list")
    add.add_argument("url")
    add.add_argument("--dir", dest="directory", default="")
    add.add_argument("--interval", type=int, default=0)
    add.add_argument("--label", default="")
    add.add_argument("--no-start", action="store_true")

    sub.add_parser("list", help="Show saved watchers")
    sub.add_parser("watch", help="Run saved watchers without a window")
    sub.add_parser("config", help="Print the config path")

    find_p = sub.add_parser("find", help="Search a board catalog")
    find_p.add_argument("board")
    find_p.add_argument("query", nargs="?", default="")
    find_p.add_argument("--limit", type=int, default=30)

    rules_p = sub.add_parser("rules", help="List or manage watchdog rules")
    rules_sub = rules_p.add_subparsers(dest="rules_cmd")
    rules_sub.add_parser("list", help="List rules")
    r_add = rules_sub.add_parser("add", help="Add a rule")
    r_add.add_argument("board")
    r_add.add_argument("query")
    r_add.add_argument("--name", default="")
    r_add.add_argument("--interval", type=int, default=120)
    r_add.add_argument("--limit", type=int, default=5)
    r_add.add_argument("--disabled", action="store_true")
    r_rm = rules_sub.add_parser("remove", help="Remove a rule by id prefix or name")
    r_rm.add_argument("id_or_name")
    r_en = rules_sub.add_parser("enable", help="Enable a rule")
    r_en.add_argument("id_or_name")
    r_dis = rules_sub.add_parser("disable", help="Disable a rule")
    r_dis.add_argument("id_or_name")

    args = parser.parse_args(argv)
    if args.cmd in (None, "gui"):
        from .app import run
        return run()
    if args.cmd == "add":
        return cmd_add(args)
    if args.cmd == "list":
        return cmd_list()
    if args.cmd == "watch":
        return cmd_watch()
    if args.cmd == "config":
        print(app_config_dir() / "threads.json")
        return 0
    if args.cmd == "find":
        return cmd_find(args)
    if args.cmd == "rules":
        return cmd_rules(args)
    parser.print_help()
    return 1


def cmd_add(args: argparse.Namespace) -> int:
    store = ConfigStore()
    configs = store.load()
    try:
        board, number, canonical = parse_thread_url(args.url)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    if any(item.board == board and item.thread_no == number for item in configs):
        print("That thread is already in the watch list.", file=sys.stderr)
        return 1
    settings = store.settings
    folder = args.directory.strip() or default_download_dir(board, number, settings)
    config = ThreadConfig(
        canonical,
        board,
        number,
        folder,
        args.label.strip(),
        interval=args.interval or settings.default_interval,
        filename_mode=settings.filename_mode,
        media_filter=settings.media_filter,
        max_file_mb=settings.max_file_mb,
        save_gallery=settings.save_gallery,
        verify_md5=settings.verify_md5,
        auto_start=not args.no_start,
    )
    configs.append(config)
    store.save(configs)
    print(f"Added {canonical}")
    print(f"Saving to {folder}")
    return 0


def cmd_list() -> int:
    store = ConfigStore()
    configs = store.load()
    if not configs:
        print("No threads saved.")
        return 0
    for config in configs:
        state = "auto" if config.auto_start else "paused"
        print(f"{config.short_id:16}  {state:6}  {config.interval:>4}s  {config.display_name}  →  {config.output_dir}")
    return 0


def cmd_watch() -> int:
    store = ConfigStore()
    configs = store.load()
    if not configs:
        print("No threads saved. Add one with: threadsyphon add <url>", file=sys.stderr)
        return 1
    events: queue.Queue[dict] = queue.Queue()
    manager = WatchManager(events.put)
    manager.apply_limits(store.settings.rate_gap, store.settings.cdn_gap)
    started = 0
    for config in configs:
        worker = manager.add(config)
        if config.auto_start:
            worker.start()
            started += 1
    print(f"Watching {started}/{len(configs)} threads. Ctrl+C to stop.")
    try:
        while True:
            try:
                event = events.get(timeout=0.5)
            except queue.Empty:
                if started and not any(worker.alive for worker in manager.workers.values()):
                    print("All watchers finished.")
                    break
                continue
            status = event.get("status", "")
            message = event.get("message", "")
            thread_id = event.get("thread_id", "")
            config = next((item for item in configs if item.id == thread_id), None)
            label = config.short_id if config else thread_id[:8]
            extra = ""
            if event.get("downloaded") is not None:
                extra = f"  {event.get('downloaded')}/{event.get('known', '?')}"
            if event.get("bytes"):
                extra += f"  {format_bytes(int(event['bytes']))}"
            print(f"{event.get('updated', '')}  {label}  {status} — {message}{extra}")
    except KeyboardInterrupt:
        print("\nStopping…")
    finally:
        manager.stop_all()
        store.save(configs)
    return 0


def cmd_find(args: argparse.Namespace) -> int:
    try:
        parse_query(args.query)
        hits = search_catalog(CatalogClient(), args.board, args.query)
    except Exception as error:
        print(error, file=sys.stderr)
        return 1
    if not hits:
        print("No matches.")
        return 0
    for hit in hits[: max(1, args.limit)]:
        title = (hit.title or hit.body or "").replace("\n", " ")[:70]
        print(f"{hit.short_id:16}  r={hit.replies:<4} i={hit.images:<4}  {title}")
    if len(hits) > args.limit:
        print(f"… {len(hits) - args.limit} more")
    return 0


def _find_rule(store: ConfigStore, key: str) -> WatchRule | None:
    key_l = key.lower()
    for rule in store.rules:
        if rule.id.startswith(key) or rule.name.lower() == key_l:
            return rule
    return None


def cmd_rules(args: argparse.Namespace) -> int:
    store = ConfigStore()
    configs = store.load()
    cmd = getattr(args, "rules_cmd", None) or "list"
    if cmd == "list":
        if not store.rules:
            print("No rules saved.")
            return 0
        for rule in store.rules:
            state = "on " if rule.enabled else "off"
            print(f"{rule.id[:8]}  {state}  /{rule.board}/  every {rule.interval}s  {rule.name}  ·  {rule.query}")
        return 0
    if cmd == "add":
        try:
            parse_query(args.query)
            rule = WatchRule(
                name=args.name,
                board=args.board,
                query=args.query,
                enabled=not args.disabled,
                interval=args.interval,
                match_limit=args.limit,
            )
        except ValueError as error:
            print(error, file=sys.stderr)
            return 1
        store.rules.append(rule)
        store.save(configs, rules=store.rules)
        print(f"Added rule {rule.id[:8]}  /{rule.board}/  {rule.query}")
        return 0
    if cmd in {"remove", "enable", "disable"}:
        rule = _find_rule(store, args.id_or_name)
        if not rule:
            print("Rule not found.", file=sys.stderr)
            return 1
        if cmd == "remove":
            store.rules = [r for r in store.rules if r.id != rule.id]
            store.save(configs, rules=store.rules)
            print(f"Removed {rule.name}")
            return 0
        rule.enabled = cmd == "enable"
        store.save(configs, rules=store.rules)
        print(f"{'Enabled' if rule.enabled else 'Disabled'} {rule.name}")
        return 0
    print("Usage: threadsyphon rules [list|add|remove|enable|disable]", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
