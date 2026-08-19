"""Resolve the CloakBrowser binary and build stable per-account fingerprint arguments."""
from __future__ import annotations

import os
from pathlib import Path

from ..config import Account, AccountsConfig
from .procutil import IS_WINDOWS, cloak_seed


def resolve_cloak_exe(account: Account, config: AccountsConfig) -> str:
    value = account.browserPath or config.cloakBrowserPath or os.environ.get("CLOAKBROWSER_BINARY_PATH", "")
    if value and Path(value).expanduser().is_dir():
        value = str(Path(value).expanduser() / ("chrome.exe" if IS_WINDOWS else "chrome"))
    return value


def build_cloak_args(account: Account, *, webrtc_ip: str | None = None) -> list[str]:
    args = [f"--fingerprint={cloak_seed(account.acc)}"]
    if account.fpPlatform != "auto":
        args.append(f"--fingerprint-platform={account.fpPlatform}")
    if account.brandVersion:
        args.append(f"--fingerprint-brand-version={account.brandVersion}")
    if account.platformVersion:
        args.append(f"--fingerprint-platform-version={account.platformVersion}")
    if webrtc_ip:
        args.append(f"--fingerprint-webrtc-ip={webrtc_ip}")
    args.extend(account.cloakArgs)
    args.extend([
        "--no-first-run",
        "--no-default-browser-check",
        "--no-service-autorun",
        "--disable-session-crashed-bubble",
        "--hide-crash-restore-bubble",
        "--disable-features=Translate,InfiniteSessionRestore",
        "--disable-backgrounding-occluded-windows",
    ])
    return args
