"""Account configuration, validation and atomic persistence."""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Self
from urllib.parse import unquote, urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

from .environment import PROJECT_ROOT, load_project_env
from .secrets import SecretStore

load_project_env()
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "config"


def _normalized_path(value: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.expanduser(value.strip())))


RESERVED_CLOAK_ARG_KEYS = {
    "--proxy-server",
    "--fingerprint-timezone",
    "--fingerprint-locale",
    "--lang",
    "--fingerprint-webrtc-ip",
    "--fingerprint-location",
}

RESERVED_FINGERPRINT_ARG_KEYS = {
    "--fingerprint",
    "--fingerprint-platform",
    "--fingerprint-platform-version",
    "--fingerprint-brand",
    "--fingerprint-brand-version",
}

CURRENT_CONFIG_VERSION = 2


def _default_fingerprint_platform() -> str:
    return "macos" if sys.platform == "darwin" else "windows"


def _server_without_credentials(value: str) -> tuple[str, str | None, str | None]:
    parsed = urlparse(value)
    hostname = parsed.hostname or ""
    host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    server = f"{parsed.scheme}://{host}:{parsed.port}"
    return server, unquote(parsed.username) if parsed.username else None, unquote(parsed.password) if parsed.password else None


class ProxyConfig(BaseModel):
    server: str
    username: str | None = None
    password: str | None = Field(default=None, exclude=True, repr=False)
    passwordRef: str | None = None

    @field_validator("server")
    @classmethod
    def _valid_server(cls, value: str) -> str:
        value = value.strip()
        parsed = urlparse(value)
        if parsed.scheme not in {"socks5", "socks5h", "http", "https"} or not parsed.hostname or not parsed.port:
            raise ValueError("代理格式应为 socks5://host:port 或 http(s)://host:port")
        if parsed.username or parsed.password:
            raise ValueError("代理账号和密码请使用独立字段填写")
        return value

    @field_validator("username", "password", "passwordRef", mode="before")
    @classmethod
    def _empty_to_none(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    def cloak_value(self) -> str | dict[str, str]:
        if not self.username and not self.password:
            return self.server
        value = {"server": self.server}
        if self.username:
            value["username"] = self.username
        if self.password:
            value["password"] = self.password
        return value

    @property
    def display(self) -> str:
        parsed = urlparse(self.server)
        auth = f"{self.username}:***@" if self.username else ""
        hostname = parsed.hostname or ""
        host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
        return f"{parsed.scheme}://{auth}{host}:{parsed.port}"

    @property
    def has_password(self) -> bool:
        return bool(self.password or self.passwordRef)


class NetworkCheck(BaseModel):
    exitIp: str | None = None
    timezone: str | None = None
    locale: str | None = None
    detectedTimezone: str | None = None
    detectedLocale: str | None = None
    appliedTimezone: str | None = None
    appliedLocale: str | None = None
    timezoneSource: Literal["auto", "custom"] | None = None
    localeSource: Literal["auto", "custom"] | None = None
    webrtcIp: str | None = None
    checkedAt: datetime | None = None
    proxySignature: str | None = None
    latencyMs: int | None = None
    stale: bool = False


class NetworkConfig(BaseModel):
    proxy: ProxyConfig | None = None
    regionMode: Literal["auto", "manual", "disabled"] = "auto"
    timezoneOverride: str | None = None
    localeOverride: str | None = None
    strictProxy: bool = True
    lastCheck: NetworkCheck | None = None

    @field_validator("timezoneOverride", "localeOverride", mode="before")
    @classmethod
    def _empty_to_none(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @model_validator(mode="after")
    def _manual_requires_identity(self) -> Self:
        if self.regionMode == "manual" and (not self.timezoneOverride or not self.localeOverride):
            raise ValueError("手动地区模式必须同时设置时区和语言")
        if self.regionMode == "disabled" and (self.timezoneOverride or self.localeOverride):
            raise ValueError("关闭地区匹配时不能设置时区或语言覆盖")
        return self


class GeolocationConfig(BaseModel):
    enabled: bool = False
    latitude: float | None = None
    longitude: float | None = None
    accuracy: float = 5000

    @model_validator(mode="after")
    def _valid_coordinates(self) -> Self:
        if self.enabled and (self.latitude is None or self.longitude is None):
            raise ValueError("启用网站定位时必须填写经纬度")
        if self.latitude is not None and not -90 <= self.latitude <= 90:
            raise ValueError("纬度必须在 -90 到 90 之间")
        if self.longitude is not None and not -180 <= self.longitude <= 180:
            raise ValueError("经度必须在 -180 到 180 之间")
        if self.accuracy <= 0:
            raise ValueError("定位精度必须大于 0")
        return self


class Account(BaseModel):
    acc: str
    name: str = ""
    userDataDir: str
    browserPath: str | None = None

    network: NetworkConfig = Field(default_factory=NetworkConfig)
    geolocation: GeolocationConfig = Field(default_factory=GeolocationConfig)
    fpPlatform: Literal["auto", "windows", "macos"] = "auto"
    platformVersion: str | None = None
    brandVersion: str | None = None
    releaseChannel: Literal["stable", "preview"] = "stable"
    browserVersion: str | None = None
    cloakArgs: list[str] = Field(default_factory=list)

    headless: bool = False
    humanize: bool = True
    humanPreset: Literal["default", "careful"] = "careful"

    @field_validator("acc", "userDataDir")
    @classmethod
    def _required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不能为空")
        return value

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_network(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "network" in value:
            return value
        payload = dict(value)
        proxy_value = payload.get("proxy")
        proxy: dict[str, str | None] | None = None
        if isinstance(proxy_value, str) and proxy_value.strip():
            server, username, password = _server_without_credentials(proxy_value.strip())
            proxy = {"server": server, "username": username, "password": password}
        timezone = payload.get("timezone") or None
        locale = payload.get("locale") or None
        old_exit_ip = payload.get("exitIp") or None
        payload["network"] = {
            "proxy": proxy,
            "regionMode": "manual" if timezone and locale else "auto",
            "timezoneOverride": timezone,
            "localeOverride": locale,
            "strictProxy": True,
            "lastCheck": {"exitIp": old_exit_ip, "stale": True} if old_exit_ip else None,
        }
        location = str(payload.get("location") or "").strip()
        if location and "," in location:
            try:
                latitude, longitude = (float(part.strip()) for part in location.split(",", 1))
                payload["geolocation"] = {
                    "enabled": False,
                    "latitude": latitude,
                    "longitude": longitude,
                    "accuracy": 5000,
                }
            except ValueError:
                pass
        return payload

    @field_validator(
        "browserPath", "platformVersion", "brandVersion", "browserVersion", mode="before"
    )
    @classmethod
    def _empty_to_none(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("fpPlatform", mode="before")
    @classmethod
    def _normalize_platform(cls, value: Any) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in {"", "auto", "default"}:
            return "auto"
        if normalized in {"mac", "macos", "darwin"}:
            return "macos"
        if normalized in {"win", "windows"}:
            return "windows"
        return normalized

    @field_validator("platformVersion")
    @classmethod
    def _valid_platform_version(cls, value: str | None) -> str | None:
        if value and not re.fullmatch(r"\d+(?:\.\d+){1,2}", value):
            raise ValueError("系统版本格式应为 major.minor 或 major.minor.patch")
        return value

    @field_validator("brandVersion")
    @classmethod
    def _valid_brand_version(cls, value: str | None) -> str | None:
        if value and not re.fullmatch(r"\d+(?:\.\d+){3}", value):
            raise ValueError("UA/CH 浏览器版本格式应为四段数字，例如 146.0.0.0")
        return value

    @field_validator("browserVersion")
    @classmethod
    def _valid_browser_version(cls, value: str | None) -> str | None:
        if value and not re.fullmatch(r"\d+(?:\.\d+){3,4}", value):
            raise ValueError("CloakBrowser 精确版本格式应为四或五段数字")
        return value

    @field_validator("cloakArgs")
    @classmethod
    def _clean_args(cls, values: list[str]) -> list[str]:
        result = [str(value).strip() for value in values if str(value).strip()]
        for arg in result:
            key = arg.split("=", 1)[0]
            if key in RESERVED_CLOAK_ARG_KEYS:
                if key == "--fingerprint-location":
                    raise ValueError("--fingerprint-location 已被 CloakBrowser 移除；请使用网站定位设置")
                raise ValueError(f"{key} 已由网络与地区身份设置管理，不能放入额外启动参数")
            if key in RESERVED_FINGERPRINT_ARG_KEYS:
                raise ValueError(f"{key} 已由设备指纹设置管理，不能放入额外启动参数")
        return result

    @model_validator(mode="after")
    def _absolute_paths(self) -> Self:
        if not Path(self.userDataDir).expanduser().is_absolute():
            raise ValueError(f"userDataDir 必须是绝对路径: {self.userDataDir}")
        if self.browserPath and not Path(self.browserPath).expanduser().is_absolute():
            raise ValueError(f"browserPath 必须是绝对路径: {self.browserPath}")
        return self

    @property
    def display_name(self) -> str:
        return self.name or self.acc

    @property
    def data_dir(self) -> Path:
        return Path(self.userDataDir).expanduser().resolve()

    @property
    def proxy(self) -> str | None:
        return self.network.proxy.server if self.network.proxy else None

    @property
    def proxy_display(self) -> str:
        return self.network.proxy.display if self.network.proxy else ""

    @property
    def proxy_value(self) -> str | dict[str, str] | None:
        return self.network.proxy.cloak_value() if self.network.proxy else None


class AccountsConfig(BaseModel):
    schemaVersion: int = CURRENT_CONFIG_VERSION
    cloakBrowserPath: str = ""
    cloakUserDataBase: str = ""
    extensionPaths: list[str] = Field(default_factory=list)
    accounts: list[Account] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _migrate_fingerprint_defaults(cls, value: Any) -> Any:
        """Migrate pre-v2 platform defaults without erasing real overrides."""
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        try:
            schema_version = int(payload.get("schemaVersion", 1))
        except (TypeError, ValueError):
            schema_version = 1
        if schema_version >= CURRENT_CONFIG_VERSION:
            return payload

        migrated_accounts: list[Any] = []
        host_default = _default_fingerprint_platform()
        for raw_account in payload.get("accounts", []):
            if not isinstance(raw_account, dict):
                migrated_accounts.append(raw_account)
                continue
            account = dict(raw_account)
            platform = str(account.get("fpPlatform") or "").strip().lower()
            normalized = {
                "mac": "macos", "darwin": "macos", "win": "windows"
            }.get(platform, platform)
            if not normalized or normalized == host_default:
                account["fpPlatform"] = "auto"
            migrated_accounts.append(account)
        payload["accounts"] = migrated_accounts
        payload["schemaVersion"] = CURRENT_CONFIG_VERSION
        return payload

    @field_validator("cloakBrowserPath", "cloakUserDataBase")
    @classmethod
    def _global_abs(cls, value: str) -> str:
        value = value.strip()
        if value and not Path(value).expanduser().is_absolute():
            raise ValueError(f"路径必须是绝对路径: {value}")
        return value

    @field_validator("extensionPaths", mode="before")
    @classmethod
    def _clean_extension_paths(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = value.splitlines()
        return [str(item).strip() for item in value if str(item).strip()]

    @field_validator("extensionPaths")
    @classmethod
    def _valid_extension_paths(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not Path(value).expanduser().is_absolute():
                raise ValueError(f"插件目录必须是绝对路径: {value}")
            key = _normalized_path(value)
            if key not in seen:
                seen.add(key)
                result.append(value)
        return result

    @model_validator(mode="after")
    def _unique_accounts(self) -> Self:
        ids: set[str] = set()
        dirs: dict[str, str] = {}
        for account in self.accounts:
            if account.acc in ids:
                raise ValueError(f"账户 id 重复: {account.acc}")
            ids.add(account.acc)
            key = _normalized_path(account.userDataDir)
            if key in dirs:
                raise ValueError(
                    f"账户 {account.acc} 与 {dirs[key]} 共用 userDataDir；"
                    "Playwright persistent context 要求每账户独立目录"
                )
            dirs[key] = account.acc
        return self

    def get(self, acc: str) -> Account | None:
        return next((account for account in self.accounts if account.acc == acc), None)

    @property
    def absolute_extension_paths(self) -> list[Path]:
        # Keep the configured absolute path instead of resolving symlinks. A stable
        # path helps unpacked extensions retain the same Chromium extension ID when
        # their underlying version directory is replaced.
        return [Path(os.path.abspath(os.path.expanduser(value))) for value in self.extensionPaths]


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ConfigStore:
    def __init__(self, config_dir: Path | None = None) -> None:
        configured = os.environ.get("BROWSER_CUSTOM_CONFIG_DIR")
        self.config_dir = Path(configured).expanduser() if configured else (config_dir or DEFAULT_CONFIG_DIR)
        self.path = self.config_dir / "accounts.json"
        self.secret_store = SecretStore(self.config_dir)
        self._lock = threading.RLock()
        self.accounts = AccountsConfig()
        self.reload()

    def reload(self) -> AccountsConfig:
        with self._lock:
            if self.path.exists():
                with self.path.open(encoding="utf-8") as handle:
                    raw = json.load(handle)
                legacy_network = any(
                    isinstance(account, dict) and "network" not in account
                    for account in raw.get("accounts", [])
                )
                try:
                    schema_version = int(raw.get("schemaVersion", 1))
                except (TypeError, ValueError):
                    schema_version = 1
                legacy_fingerprint = schema_version < CURRENT_CONFIG_VERSION
                self.accounts = AccountsConfig.model_validate(raw)
                self._hydrate_secrets(self.accounts)
                if legacy_network:
                    backup = self.path.with_name("accounts.json.v1.bak")
                    if not backup.exists():
                        shutil.copy2(self.path, backup)
                if legacy_fingerprint:
                    backup = self.path.with_name("accounts.json.v2.bak")
                    if not backup.exists():
                        shutil.copy2(self.path, backup)
                if legacy_network or legacy_fingerprint:
                    self.save(self.accounts)
            else:
                self.accounts = AccountsConfig()
            return self.accounts

    def save(self, config: AccountsConfig) -> None:
        with self._lock:
            old_refs = self._secret_refs(self.accounts)
            for account in config.accounts:
                proxy = account.network.proxy
                if not proxy:
                    continue
                if proxy.password and not proxy.passwordRef:
                    proxy.passwordRef = f"proxy:{account.acc}"
                if proxy.password and proxy.passwordRef:
                    self.secret_store.set(proxy.passwordRef, proxy.password)
            # JSON mode converts datetime and other Pydantic types to values
            # accepted by json.dump(). Only publish the new in-memory snapshot
            # after the atomic disk replacement succeeds.
            _atomic_write(self.path, config.model_dump(mode="json"))
            self.accounts = config
            for reference in old_refs - self._secret_refs(config):
                self.secret_store.delete(reference)

    def _hydrate_secrets(self, config: AccountsConfig) -> None:
        for account in config.accounts:
            proxy = account.network.proxy
            if proxy and proxy.passwordRef:
                proxy.password = self.secret_store.get(proxy.passwordRef)

    @staticmethod
    def _secret_refs(config: AccountsConfig) -> set[str]:
        return {
            proxy.passwordRef
            for account in config.accounts
            if (proxy := account.network.proxy) and proxy.passwordRef
        }


store = ConfigStore()
