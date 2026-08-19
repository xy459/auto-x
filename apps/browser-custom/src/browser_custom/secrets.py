"""Small secret store with OS-keyring support and a restricted-file fallback."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class SecretStore:
    """Store proxy passwords outside accounts.json.

    macOS and Windows use the system credential store when ``keyring`` is
    available. Headless systems without a usable backend fall back to a local
    file with owner-only permissions.
    """

    SERVICE = "browser-custom"

    def __init__(self, config_dir: Path) -> None:
        self.fallback_path = config_dir / "secrets.json"

    def get(self, reference: str) -> str | None:
        try:
            import keyring

            value = keyring.get_password(self.SERVICE, reference)
            if value is not None:
                return value
        except Exception:
            pass
        return self._read_fallback().get(reference)

    def set(self, reference: str, value: str) -> None:
        if self._set_keyring(reference, value):
            self._delete_fallback(reference)
            return
        data = self._read_fallback()
        data[reference] = value
        self._write_fallback(data)

    def delete(self, reference: str) -> None:
        try:
            import keyring

            keyring.delete_password(self.SERVICE, reference)
        except Exception:
            pass
        self._delete_fallback(reference)

    def _set_keyring(self, reference: str, value: str) -> bool:
        try:
            import keyring

            keyring.set_password(self.SERVICE, reference, value)
            return True
        except Exception:
            return False

    def _read_fallback(self) -> dict[str, str]:
        try:
            with self.fallback_path.open(encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _write_fallback(self, data: dict[str, str]) -> None:
        self.fallback_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=str(self.fallback_path.parent), suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            os.replace(temporary, self.fallback_path)
            os.chmod(self.fallback_path, 0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _delete_fallback(self, reference: str) -> None:
        data = self._read_fallback()
        if reference not in data:
            return
        data.pop(reference, None)
        if data:
            self._write_fallback(data)
        else:
            try:
                self.fallback_path.unlink()
            except FileNotFoundError:
                pass
