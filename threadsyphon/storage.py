from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .models import AppSettings, ThreadConfig, WatchRule


def app_config_dir() -> Path:
    root = os.environ.get("XDG_CONFIG_HOME")
    if root:
        return Path(root) / "threadsyphon"
    legacy = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if legacy and os.name == "nt":
        return Path(legacy) / "threadsyphon"
    return Path.home() / ".config" / "threadsyphon"


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


class ConfigStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or app_config_dir() / "threads.json"
        self.settings = AppSettings()
        self.rules: list[WatchRule] = []

    def load(self) -> list[ThreadConfig]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return []
            self.settings = AppSettings.from_dict(payload.get("settings"))
            self.rules = []
            for row in payload.get("rules", []) or []:
                try:
                    self.rules.append(WatchRule.from_dict(row))
                except (TypeError, ValueError, KeyError):
                    continue
            rows = payload.get("threads", [])
            configs = [ThreadConfig.from_dict(row) for row in rows]
            unique: dict[str, ThreadConfig] = {}
            for config in configs:
                unique[f"{config.board}/{config.thread_no}"] = config
            return list(unique.values())
        except (OSError, ValueError, TypeError, KeyError):
            self.settings = AppSettings()
            self.rules = []
            return []

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
