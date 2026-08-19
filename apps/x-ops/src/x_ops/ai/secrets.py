"""Secret storage for AI credentials.

Use the operating-system keyring when available.  Minimal/headless deployments
fall back to a separate mode-0600 file; the ordinary settings JSON never
contains the credential.
"""
from __future__ import annotations

import os
from pathlib import Path


class AISecretStore:
    SERVICE = "x-ops-ai"
    USERNAME = "default-api-key"

    def __init__(self, fallback_path: Path):
        self.fallback_path = fallback_path

    def get(self) -> str:
        environment = os.environ.get("X_OPS_AI_API_KEY", "")
        if environment:
            return environment
        try:
            import keyring

            value = keyring.get_password(self.SERVICE, self.USERNAME)
            if value:
                return value
        except Exception:
            pass
        try:
            return self.fallback_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def set(self, value: str) -> None:
        value = value.strip()
        try:
            import keyring

            if value:
                keyring.set_password(self.SERVICE, self.USERNAME, value)
            else:
                keyring.delete_password(self.SERVICE, self.USERNAME)
            if self.fallback_path.exists():
                self.fallback_path.unlink()
            return
        except Exception:
            pass
        if not value:
            try:
                self.fallback_path.unlink()
            except FileNotFoundError:
                pass
            return
        self.fallback_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.fallback_path.with_suffix(".tmp")
        temporary.write_text(value, encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.fallback_path)
