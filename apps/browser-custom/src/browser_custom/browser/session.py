"""One account owns one CloakBrowser persistent context and one user-data-dir."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from ..cloak import region_label_for_timezone, resolve_launch_identity
from ..config import Account, AccountsConfig
from .launch_args import build_cloak_args, resolve_cloak_exe
from .procutil import kill_for_data_dir, wait_for_exit

logger = logging.getLogger(__name__)

# CloakBrowser resolves a custom binary through a process-wide environment variable.
# Serialize env mutation + launch so concurrent accounts cannot inherit each other's path.
_LAUNCH_LOCK = asyncio.Lock()
_PLAYWRIGHT_DISABLE_EXTENSIONS_ARG = "--disable-extensions"


def _allow_profile_extensions(cloak_browser_module: Any) -> None:
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


class AccountSession:
    def __init__(self, account: Account, config: AccountsConfig) -> None:
        self.account = account
        self.config = config
        self.acc = account.acc
        self._context = None
        self._alive = False
        self._fingerprint: dict[str, Any] | None = None

    def is_alive(self) -> bool:
        return self._context is not None and self._alive

    def _on_close(self, *_args) -> None:
        self._alive = False
        logger.info("%s browser context closed", self.acc)

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

        executable = resolve_cloak_exe(self.account, self.config)
        if executable and not Path(executable).exists():
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

        import cloakbrowser as cloak
        import cloakbrowser.browser as cloak_browser

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
        try:
            self._context.on("close", self._on_close)
        except Exception:  # noqa: BLE001
            pass
        logger.info("%s CloakBrowser started (extensions=%d)", self.acc, len(extension_paths))

    async def close(self) -> dict:
        context, self._context = self._context, None
        self._alive = False
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
