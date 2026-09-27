from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any
from uuid import uuid4

from .models import AppSettings, ThreadConfig, WatchRule


def app_config_dir() -> Path:
    root = os.environ.get("XDG_CONFIG_HOME")
    if root:
        return Path(root) / "threadsyphon"
    legacy = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if legacy and os.name == "nt":
        return Path(legacy) / "threadsyphon"
    return Path.home() / ".config" / "threadsyphon"


MAX_PERSISTED_JSON_BYTES = 4 * 1024 * 1024


def is_safe_regular_file(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1


def load_json_file(path: Path, maximum: int = MAX_PERSISTED_JSON_BYTES) -> Any | None:
    try:
        with path.open("rb") as stream:
            raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            return None
        return json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        return None


def atomic_json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def atomic_text_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


class ConfigStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or app_config_dir() / "threads.json"
        self.settings = AppSettings()
        self.rules: list[WatchRule] = []

    def load(self) -> list[ThreadConfig]:
        self.settings = AppSettings()
        self.rules = []
        payload = load_json_file(self.path)
        if not isinstance(payload, dict):
            return []
        self.settings = AppSettings.from_dict(payload.get("settings"))
        raw_rules = payload.get("rules", [])
        if not isinstance(raw_rules, list):
            raw_rules = []
        for row in raw_rules:
            try:
                if isinstance(row, dict):
                    self.rules.append(WatchRule.from_dict(row))
            except (TypeError, ValueError, KeyError, OverflowError):
                continue
        unique: dict[str, ThreadConfig] = {}
        seen_ids: set[str] = set()
        raw_threads = payload.get("threads", [])
        if not isinstance(raw_threads, list):
            raw_threads = []
        for row in raw_threads:
            try:
                if not isinstance(row, dict):
                    continue
                config = ThreadConfig.from_dict(row)
            except (TypeError, ValueError, KeyError, OverflowError):
                continue
            if config.id in seen_ids:
                config.id = uuid4().hex
            seen_ids.add(config.id)
            unique[f"{config.board}/{config.thread_no}"] = config
        return list(unique.values())

    def save(
        self,
        configs: list[ThreadConfig],
        settings: AppSettings | None = None,
        rules: list[WatchRule] | None = None,
    ) -> None:
        if settings is not None:
            self.settings = settings
        if rules is not None:
            self.rules = rules
        atomic_json_write(
            self.path,
            {
                "version": 3,
                "settings": self.settings.to_dict(),
                "threads": [item.to_dict() for item in configs],
                "rules": [item.to_dict() for item in self.rules],
            },
        )
