from __future__ import annotations

import os
from pathlib import Path

from browser_custom.secrets import SecretStore


def data_dir() -> Path:
    configured = os.environ.get("X_OPS_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "data"


def store() -> SecretStore:
    return SecretStore(data_dir())


def reference(kind: str, task_id: str, account_id: str) -> str:
    safe_kind = "".join(ch for ch in kind if ch.isalnum() or ch in "-_")
    return f"x-login:{task_id}:{account_id}:{safe_kind}"
