"""Small JSON-backed stores for console-owned configuration.

Task/TaskRun persistence remains in the core storage module.  These stores are
only for runtime knobs and AI console settings, which are owned by the admin
layer.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from threading import RLock
from typing import Any

DEFAULT_RUNTIME_SETTINGS: dict[str, Any] = {
    "max_concurrent_browser_tasks": 4,
    "cancellation_poll_interval_seconds": 1.0,
    "default_task_timeout_seconds": 3600,
    "browser_acquire_timeout_seconds": 120,
    "default_browser_end_policy": "keep_open",
    "task_log_retention_days": 30,
    "queue_poll_interval_seconds": 1.0,
}


class JsonSettingsStore:
    def __init__(self, path: Path, defaults: Mapping[str, Any]):
        self.path = path
        self.defaults = dict(defaults)
        self._lock = RLock()

    def get(self) -> dict[str, Any]:
        with self._lock:
            if not self.path.exists():
                return deepcopy(self.defaults)
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return deepcopy(self.defaults)
            if not isinstance(loaded, dict):
                return deepcopy(self.defaults)
            return {**deepcopy(self.defaults), **loaded}

    def update(self, values: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            updated = {**self.get(), **dict(values)}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.chmod(temporary, 0o600)
            temporary.replace(self.path)
            return deepcopy(updated)
