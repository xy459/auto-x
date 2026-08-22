"""One account owns one CloakBrowser persistent context and one user-data-dir."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from ..cloak import region_label_for_timezone, resolve_launch_identity
from ..config import Account, AccountsConfig, _atomic_write
from .launch_args import build_cloak_args, resolve_cloak_exe
from .procutil import activate_for_data_dir, kill_for_data_dir, wait_for_exit

logger = logging.getLogger(__name__)

# CloakBrowser resolves a custom binary through a process-wide environment variable.
# Serialize env mutation + launch so concurrent accounts cannot inherit each other's path.
_LAUNCH_LOCK = asyncio.Lock()
_PLAYWRIGHT_DISABLE_EXTENSIONS_ARG = "--disable-extensions"
_X_PROFILE_SELECTOR = 'a[data-testid="AppTabBar_Profile_Link"]'
# Chromium 150 exposes these non-placeholder local profile avatars. Keep index 0
# reserved for Chromium's generic fallback silhouette.
_PROFILE_AVATAR_INDICES = tuple(range(1, 28))
_X_RESERVED_PATHS = {
    "account", "compose", "explore", "home", "i", "jobs", "login", "logout",
    "messages", "notifications", "search", "settings", "signup",
}


def _x_username_from_href(href: str | None) -> str | None:
    if not href:
        return None
    path = urlparse(href).path.strip("/")
    if "/" in path or path.casefold() in _X_RESERVED_PATHS:
        return None
    return path if re.fullmatch(r"[A-Za-z0-9_]{1,15}", path) else None


async def detect_x_username(page: Any) -> str | None:
    """Read the authenticated account name from X's profile navigation link."""
    try:
        host = (urlparse(str(page.url)).hostname or "").casefold()
        if host not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
            return None
        profile_link = page.locator(_X_PROFILE_SELECTOR).first
        href = await profile_link.get_attribute("href", timeout=1500)
        return _x_username_from_href(href)
    except Exception:  # noqa: BLE001
        return None


class _CloakBrowserModule(Protocol):
    IGNORE_DEFAULT_ARGS: list[str]


def _allow_profile_extensions(cloak_browser_module: _CloakBrowserModule) -> None:
    """Keep Playwright from disabling extensions stored in the persistent profile."""
    ignored = list(cloak_browser_module.IGNORE_DEFAULT_ARGS)
    if _PLAYWRIGHT_DISABLE_EXTENSIONS_ARG not in ignored:
        cloak_browser_module.IGNORE_DEFAULT_ARGS = [
            *ignored,
            _PLAYWRIGHT_DISABLE_EXTENSIONS_ARG,
        ]


def _resolve_extension_paths(config: AccountsConfig) -> list[str]:
    result: list[str] = []
    for path in config.absolute_extension_paths:
        if not path.is_dir():
            raise RuntimeError(f"插件目录不存在或不是目录: {path}")
        manifest = path / "manifest.json"
        if not manifest.is_file():
            raise RuntimeError(f"插件目录缺少 manifest.json: {path}")
        result.append(str(path))
    return result


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"无法读取 Chromium Profile 配置: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Chromium Profile 配置不是 JSON 对象: {path}")
    return value


def _initial_profile_avatar_index(account: Account) -> int:
    """Choose a stable Chromium avatar for a newly initialized account."""
    digest = hashlib.sha256(account.display_name.encode("utf-8")).digest()
    return _PROFILE_AVATAR_INDICES[int.from_bytes(digest[:4], "big") % len(_PROFILE_AVATAR_INDICES)]


def _sync_chromium_profile_name(account: Account) -> None:
    """Keep Chromium's label aligned and initialize a new profile avatar once."""
    profile_name = account.display_name
    preferences_path = account.data_dir / "Default" / "Preferences"
    local_state_path = account.data_dir / "Local State"
    initialize_avatar = (
        account.autoAssignAvatar
        and not preferences_path.exists()
        and not local_state_path.exists()
    )
    preferences = _read_json_object(preferences_path)
    profile_preferences = preferences.setdefault("profile", {})
    if not isinstance(profile_preferences, dict):
        profile_preferences = {}
        preferences["profile"] = profile_preferences
    profile_preferences["name"] = profile_name
    avatar_index: int | None = None
    if initialize_avatar:
        avatar_index = _initial_profile_avatar_index(account)
        profile_preferences["avatar_index"] = avatar_index
    _atomic_write(preferences_path, preferences)

    local_state = _read_json_object(local_state_path)
    local_profiles = local_state.setdefault("profile", {})
    if not isinstance(local_profiles, dict):
        local_profiles = {}
        local_state["profile"] = local_profiles
    info_cache = local_profiles.setdefault("info_cache", {})
    if not isinstance(info_cache, dict):
        info_cache = {}
        local_profiles["info_cache"] = info_cache
    default_profile = info_cache.setdefault("Default", {})
    if not isinstance(default_profile, dict):
        default_profile = {}
        info_cache["Default"] = default_profile
    default_profile["name"] = profile_name
    default_profile["is_using_default_name"] = False
    if avatar_index is not None:
        default_profile["avatar_icon"] = f"chrome://theme/IDR_PROFILE_AVATAR_{avatar_index}"
        default_profile["is_using_default_avatar"] = True
    _atomic_write(local_state_path, local_state)


class AccountSession:
    def __init__(
        self,
        account: Account,
        config: AccountsConfig,
        on_x_username: Callable[[str, str], bool] | None = None,
    ) -> None:
        self.account = account
        self.config = config
        self.acc = account.acc
        self._context: Any | None = None
        self._alive = False
        self._fingerprint: dict[str, Any] | None = None
        self._on_x_username = on_x_username
        self._x_identity_task: asyncio.Task[None] | None = None

    def is_alive(self) -> bool:
        return self._context is not None and self._alive

    @property
    def context(self) -> Any:
        """Return the live persistent context for in-process integrations."""
        if not self.is_alive():
            raise RuntimeError(f"账户浏览器未运行: {self.acc}")
        return self._context

    async def new_page(self) -> Any:
        """Create a caller-owned page while keeping profile state in this context."""
        return await self.context.new_page()

    async def bring_to_front(self) -> bool:
        """Raise this account's existing browser window for a manual open action."""
        context = self.context
        pages = [page for page in list(getattr(context, "pages", [])) if not page.is_closed()]
        page = pages[-1] if pages else await context.new_page()
        await page.bring_to_front()
        activated = await asyncio.to_thread(activate_for_data_dir, self.account.data_dir)
        if not activated:
            logger.debug("%s browser page selected but native window activation was unavailable", self.acc)
        return activated

    def _on_close(self, *_args: object) -> None:
        self._alive = False
        if self._x_identity_task:
            self._x_identity_task.cancel()
        logger.info("%s browser context closed", self.acc)

    async def _scan_x_username(self) -> None:
        if self._context is None or self._on_x_username is None:
            return
        for page in list(getattr(self._context, "pages", [])):
            username = await detect_x_username(page)
            if not username or username == self.account.xUsername:
                continue
            await asyncio.to_thread(self._on_x_username, self.acc, username)
            self.account.xUsername = username
            logger.info("%s detected authenticated X username @%s", self.acc, username)
            return

    async def _monitor_x_identity(self) -> None:
        while self._alive:
            try:
                await self._scan_x_username()
            except Exception as exc:  # noqa: BLE001
                logger.debug("%s X username detection failed: %s", self.acc, exc)
            await asyncio.sleep(3)

    def fingerprint_info(self) -> dict[str, Any] | None:
        return dict(self._fingerprint) if self._fingerprint else None

    async def _capture_browser_fingerprint(self) -> None:
        """Best-effort capture of the actual runtime browser version and UA."""
        if self._context is None or self._fingerprint is None:
            return
        try:
            browser = getattr(self._context, "browser", None)
            version = getattr(browser, "version", None) if browser else None
            if version:
                self._fingerprint["browserVersion"] = str(version)
        except Exception:  # noqa: BLE001
            pass
        try:
            pages = getattr(self._context, "pages", [])
            if pages:
                user_agent = await pages[0].evaluate("navigator.userAgent")
                if user_agent:
                    self._fingerprint["userAgent"] = str(user_agent)
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s could not capture runtime user agent: %s", self.acc, exc)

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.account.data_dir.mkdir(parents=True, exist_ok=True)

        stale = await loop.run_in_executor(None, kill_for_data_dir, self.account.data_dir)
        if stale:
            logger.info("%s removed stale browser processes: %s", self.acc, stale)
        await asyncio.to_thread(_sync_chromium_profile_name, self.account)

        executable = resolve_cloak_exe(self.account, self.config)
        if executable and not await asyncio.to_thread(Path(executable).exists):
            raise RuntimeError(f"CloakBrowser 二进制不存在: {executable}")
        extension_paths = _resolve_extension_paths(self.config)
        proxy = self.account.proxy_value
        network = self.account.network
        launch_timezone = network.timezoneOverride
        launch_locale = network.localeOverride
        webrtc_ip: str | None = None

        if network.regionMode != "disabled":
            try:
                identity = await resolve_launch_identity(
                    network.proxy,
                    network.timezoneOverride,
                    network.localeOverride,
                )
                launch_timezone = identity["timezone"]
                launch_locale = identity["locale"]
                webrtc_ip = identity["exitIp"]
            except Exception as exc:  # noqa: BLE001
                if network.proxy and network.strictProxy:
                    raise
                logger.warning(
                    "%s network identity resolution failed; launching with explicit overrides only: %s",
                    self.acc,
                    exc,
                )

        import cloakbrowser as cloak  # type: ignore[import-untyped]
        import cloakbrowser.browser as cloak_browser  # type: ignore[import-untyped]

        # Playwright adds --disable-extensions by default. CloakBrowser 0.5.8
        # does not suppress it, so manually installed profile extensions appear
        # in profile data but cannot load or run. Patch the wrapper's process-wide
        # ignore list without modifying the installed third-party package.
        _allow_profile_extensions(cloak_browser)

        async with _LAUNCH_LOCK:
            previous = os.environ.get("CLOAKBROWSER_BINARY_PATH")
            if executable:
                os.environ["CLOAKBROWSER_BINARY_PATH"] = executable
            else:
                os.environ.pop("CLOAKBROWSER_BINARY_PATH", None)
            try:
                launch_kwargs: dict[str, Any] = {}
                if self.account.geolocation.enabled:
                    launch_kwargs.update({
                        "geolocation": {
                            "latitude": self.account.geolocation.latitude,
                            "longitude": self.account.geolocation.longitude,
                            "accuracy": self.account.geolocation.accuracy,
                        },
                        "permissions": ["geolocation"],
                    })
                self._context = await cloak.launch_persistent_context_async(
                    user_data_dir=str(self.account.data_dir),
                    headless=self.account.headless,
                    proxy=proxy,
                    # Network identity was resolved once above. Do not let the
                    # wrapper perform a second GeoIP/exit-IP request at launch.
                    geoip=False,
                    timezone=launch_timezone,
                    locale=launch_locale,
                    humanize=self.account.humanize,
                    human_preset=self.account.humanPreset,
                    extension_paths=extension_paths or None,
                    release_channel=self.account.releaseChannel,
                    browser_version=self.account.browserVersion,
                    args=build_cloak_args(self.account, webrtc_ip=webrtc_ip),
                    stealth_args=True,
                    **launch_kwargs,
                )
            finally:
                if previous is None:
                    os.environ.pop("CLOAKBROWSER_BINARY_PATH", None)
                else:
                    os.environ["CLOAKBROWSER_BINARY_PATH"] = previous

        self._fingerprint = {
            "source": "runtime" if network.regionMode != "disabled" else "disabled",
            "webrtcIp": webrtc_ip,
            "region": region_label_for_timezone(launch_timezone),
            "locale": launch_locale,
            "timezone": launch_timezone,
            "browserVersion": None,
            "userAgent": None,
            "stale": False,
        }
        await self._capture_browser_fingerprint()
        self._alive = True
        context = self._context
        try:
            context.on("close", self._on_close)
        except Exception:  # noqa: BLE001
            pass
        if self._on_x_username:
            self._x_identity_task = asyncio.create_task(
                self._monitor_x_identity(), name=f"x-identity:{self.acc}"
            )
        logger.info("%s CloakBrowser started (extensions=%d)", self.acc, len(extension_paths))

    async def close(self) -> dict[str, Any]:
        context, self._context = self._context, None
        self._alive = False
        identity_task, self._x_identity_task = self._x_identity_task, None
        if identity_task:
            identity_task.cancel()
            try:
                await identity_task
            except asyncio.CancelledError:
                pass
        close_error: str | None = None
        if context is not None:
            try:
                # cloakbrowser>=0.5.8 patches context.close() to also stop its
                # Playwright driver, fixing the driver leak present in older releases.
                await asyncio.wait_for(context.close(), timeout=20)
            except Exception as exc:  # noqa: BLE001
                close_error = str(exc)
                logger.warning("%s context close failed: %s", self.acc, exc)

        loop = asyncio.get_running_loop()
        remaining = await loop.run_in_executor(None, wait_for_exit, self.account.data_dir, 3.0)
        killed: list[int] = []
        if remaining:
            killed = await loop.run_in_executor(None, kill_for_data_dir, self.account.data_dir)
            logger.warning("%s force-killed residual browser processes: %s", self.acc, killed)
        return {"closeError": close_error, "killed": killed}


def _reset_launch_lock_for_tests() -> None:
    """Tests may create multiple event loops; replace the module-level asyncio lock."""
    global _LAUNCH_LOCK
    _LAUNCH_LOCK = asyncio.Lock()
