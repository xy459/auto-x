from pathlib import Path
from unittest.mock import AsyncMock

import cloakbrowser
import pytest
from cloakbrowser.browser import build_args

from browser_custom.browser import session as session_module
from browser_custom.browser.session import AccountSession, _resolve_extension_paths
from browser_custom.config import Account, AccountsConfig


def make_extension(path: Path) -> Path:
    path.mkdir()
    (path / "manifest.json").write_text('{"manifest_version": 3, "name": "test", "version": "1.0.0"}')
    return path


def test_resolve_extension_paths_validates_directory_and_manifest(tmp_path: Path):
    missing = AccountsConfig(extensionPaths=[str(tmp_path / "missing")])
    with pytest.raises(RuntimeError, match="插件目录不存在"):
        _resolve_extension_paths(missing)

    without_manifest = tmp_path / "without-manifest"
    without_manifest.mkdir()
    with pytest.raises(RuntimeError, match="缺少 manifest.json"):
        _resolve_extension_paths(AccountsConfig(extensionPaths=[str(without_manifest)]))

    extension = make_extension(tmp_path / "extension")
    assert _resolve_extension_paths(AccountsConfig(extensionPaths=[str(extension)])) == [str(extension)]


def test_cloakbrowser_builds_official_extension_flags(tmp_path: Path):
    extension = make_extension(tmp_path / "extension")
    args = build_args(False, [], extension_paths=[str(extension)])
    assert f"--load-extension={extension}" in args
    assert f"--disable-extensions-except={extension}" in args


@pytest.mark.asyncio
async def test_session_passes_global_extensions_to_cloakbrowser(tmp_path: Path, monkeypatch):
    extension = make_extension(tmp_path / "extension")
    account = Account(
        acc="a", userDataDir=str(tmp_path / "profile"),
        releaseChannel="preview", browserVersion="145.0.7632.109.2",
    )
    config = AccountsConfig(extensionPaths=[str(extension)], accounts=[account])
    context = type("Context", (), {"on": lambda self, *_args: None})()
    launch = AsyncMock(return_value=context)

    monkeypatch.setattr(cloakbrowser, "launch_persistent_context_async", launch)
    monkeypatch.setattr(session_module, "kill_for_data_dir", lambda _path: [])
    monkeypatch.setattr(session_module, "resolve_cloak_exe", lambda _account, _config: "")

    await AccountSession(account, config).start()

    assert launch.await_args.kwargs["extension_paths"] == [str(extension)]
    assert launch.await_args.kwargs["headless"] is False
    assert launch.await_args.kwargs["geoip"] is True
    assert launch.await_args.kwargs["release_channel"] == "preview"
    assert launch.await_args.kwargs["browser_version"] == "145.0.7632.109.2"


@pytest.mark.asyncio
async def test_session_uses_official_geoip_proxy_and_geolocation(tmp_path: Path, monkeypatch):
    account = Account.model_validate({
        "acc": "a", "userDataDir": str(tmp_path / "profile"),
        "network": {
            "proxy": {"server": "socks5://127.0.0.1:1080", "username": "u", "password": "p"},
            "regionMode": "auto", "timezoneOverride": "Asia/Tokyo", "strictProxy": True,
        },
        "geolocation": {"enabled": True, "latitude": 35.68, "longitude": 139.76, "accuracy": 5000},
    })
    config = AccountsConfig(accounts=[account])
    context = type("Context", (), {"on": lambda self, *_args: None})()
    launch = AsyncMock(return_value=context)
    probe = AsyncMock(return_value={"exitIp": "203.0.113.8"})

    monkeypatch.setattr(cloakbrowser, "launch_persistent_context_async", launch)
    monkeypatch.setattr(session_module, "probe_network_identity", probe)
    monkeypatch.setattr(session_module, "kill_for_data_dir", lambda _path: [])
    monkeypatch.setattr(session_module, "resolve_cloak_exe", lambda _account, _config: "")

    await AccountSession(account, config).start()

    assert probe.await_count == 1
    assert launch.await_args.kwargs["proxy"] == {
        "server": "socks5://127.0.0.1:1080", "username": "u", "password": "p",
    }
    assert launch.await_args.kwargs["geoip"] is True
    assert launch.await_args.kwargs["timezone"] == "Asia/Tokyo"
    assert launch.await_args.kwargs["locale"] is None
    assert launch.await_args.kwargs["geolocation"]["latitude"] == 35.68
    assert launch.await_args.kwargs["permissions"] == ["geolocation"]
