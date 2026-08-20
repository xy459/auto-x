import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import cloakbrowser
import pytest
from cloakbrowser.browser import build_args

from browser_custom.browser import session as session_module
from browser_custom.browser.session import (
    AccountSession,
    _resolve_extension_paths,
    _sync_chromium_profile_name,
)
from browser_custom.cloak import resolve_launch_identity
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


def test_sync_chromium_profile_name_preserves_existing_preferences(tmp_path: Path):
    profile_dir = tmp_path / "profile"
    preferences_path = profile_dir / "Default" / "Preferences"
    local_state_path = profile_dir / "Local State"
    preferences_path.parent.mkdir(parents=True)
    preferences_path.write_text(
        '{"profile":{"name":"Person 1","avatar_index":26},"intl":{"selected_languages":"zh-CN"}}',
        encoding="utf-8",
    )
    local_state_path.write_text(
        '{"profile":{"info_cache":{"Default":{"name":"Person 1","is_using_default_name":true,"avatar_icon":"avatar"}}},"browser":{"enabled_labs_experiments":["example"]}}',
        encoding="utf-8",
    )
    account = Account(acc="account-1", name="X主账号", userDataDir=str(profile_dir))

    _sync_chromium_profile_name(account)

    preferences = json.loads(preferences_path.read_text(encoding="utf-8"))
    local_state = json.loads(local_state_path.read_text(encoding="utf-8"))
    assert preferences["profile"]["name"] == "X主账号"
    assert preferences["profile"]["avatar_index"] == 26
    assert preferences["intl"]["selected_languages"] == "zh-CN"
    cached = local_state["profile"]["info_cache"]["Default"]
    assert cached["name"] == "X主账号"
    assert cached["is_using_default_name"] is False
    assert cached["avatar_icon"] == "avatar"
    assert local_state["browser"]["enabled_labs_experiments"] == ["example"]


def test_sync_chromium_profile_name_initializes_new_profile(tmp_path: Path):
    profile_dir = tmp_path / "profile"
    account = Account(acc="account-1", name="", userDataDir=str(profile_dir))

    _sync_chromium_profile_name(account)

    preferences = json.loads(
        (profile_dir / "Default" / "Preferences").read_text(encoding="utf-8")
    )
    local_state = json.loads(
        (profile_dir / "Local State").read_text(encoding="utf-8")
    )
    assert preferences["profile"]["name"] == "account-1"
    assert local_state["profile"]["info_cache"]["Default"] == {
        "name": "account-1",
        "is_using_default_name": False,
    }


@pytest.mark.asyncio
async def test_launch_identity_uses_one_official_geoip_resolution(monkeypatch):
    calls = []

    def fake_resolve(*args):
        calls.append(args)
        return "Asia/Tokyo", "ja-JP", "203.0.113.8"

    monkeypatch.setattr(cloakbrowser, "maybe_resolve_geoip", fake_resolve)
    proxy = Account.model_validate({
        "acc": "a", "userDataDir": "/tmp/a",
        "network": {"proxy": {
            "server": "socks5://127.0.0.1:1080", "username": "u", "password": "p",
        }},
    }).network.proxy

    result = await resolve_launch_identity(proxy, "Asia/Tokyo", None)

    assert result == {
        "timezone": "Asia/Tokyo", "locale": "ja-JP", "exitIp": "203.0.113.8",
    }
    assert calls == [(
        True,
        {"server": "socks5://127.0.0.1:1080", "username": "u", "password": "p"},
        "Asia/Tokyo",
        None,
        None,
    )]


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
    resolve_identity = AsyncMock(return_value={
        "timezone": "Asia/Shanghai", "locale": "zh-CN", "exitIp": "203.0.113.8",
    })
    monkeypatch.setattr(session_module, "resolve_launch_identity", resolve_identity)

    await AccountSession(account, config).start()

    assert resolve_identity.await_count == 1
    assert launch.await_args.kwargs["extension_paths"] == [str(extension)]
    assert launch.await_args.kwargs["headless"] is False
    assert launch.await_args.kwargs["geoip"] is False
    assert launch.await_args.kwargs["timezone"] == "Asia/Shanghai"
    assert launch.await_args.kwargs["locale"] == "zh-CN"
    assert "--fingerprint-webrtc-ip=203.0.113.8" in launch.await_args.kwargs["args"]
    assert launch.await_args.kwargs["release_channel"] == "preview"
    assert launch.await_args.kwargs["browser_version"] == "145.0.7632.109.2"


@pytest.mark.asyncio
async def test_session_captures_runtime_browser_version_and_user_agent(tmp_path: Path, monkeypatch):
    account = Account(acc="a", userDataDir=str(tmp_path / "profile"))
    config = AccountsConfig(accounts=[account])
    page = SimpleNamespace(evaluate=AsyncMock(return_value="Mozilla/5.0 Chrome/145.0.0.0"))
    context = SimpleNamespace(
        browser=SimpleNamespace(version="145.0.7632.109"),
        pages=[page],
        on=lambda *_args: None,
    )
    launch = AsyncMock(return_value=context)
    resolve_identity = AsyncMock(return_value={
        "timezone": "Asia/Tokyo", "locale": "ja-JP", "exitIp": "203.0.113.8",
    })

    monkeypatch.setattr(cloakbrowser, "launch_persistent_context_async", launch)
    monkeypatch.setattr(session_module, "resolve_launch_identity", resolve_identity)
    monkeypatch.setattr(session_module, "kill_for_data_dir", lambda _path: [])
    monkeypatch.setattr(session_module, "resolve_cloak_exe", lambda _account, _config: "")

    session = AccountSession(account, config)
    await session.start()

    assert session.fingerprint_info() == {
        "source": "runtime",
        "webrtcIp": "203.0.113.8",
        "region": "日本（东京）",
        "locale": "ja-JP",
        "timezone": "Asia/Tokyo",
        "browserVersion": "145.0.7632.109",
        "userAgent": "Mozilla/5.0 Chrome/145.0.0.0",
        "stale": False,
    }
    page.evaluate.assert_awaited_once_with("navigator.userAgent")


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
    resolve_identity = AsyncMock(return_value={
        "timezone": "Asia/Tokyo", "locale": "zh-CN", "exitIp": "203.0.113.8",
    })

    monkeypatch.setattr(cloakbrowser, "launch_persistent_context_async", launch)
    monkeypatch.setattr(session_module, "resolve_launch_identity", resolve_identity)
    monkeypatch.setattr(session_module, "kill_for_data_dir", lambda _path: [])
    monkeypatch.setattr(session_module, "resolve_cloak_exe", lambda _account, _config: "")

    await AccountSession(account, config).start()

    assert resolve_identity.await_count == 1
    assert launch.await_args.kwargs["proxy"] == {
        "server": "socks5://127.0.0.1:1080", "username": "u", "password": "p",
    }
    assert launch.await_args.kwargs["geoip"] is False
    assert launch.await_args.kwargs["timezone"] == "Asia/Tokyo"
    assert launch.await_args.kwargs["locale"] == "zh-CN"
    assert "--fingerprint-webrtc-ip=203.0.113.8" in launch.await_args.kwargs["args"]
    assert launch.await_args.kwargs["geolocation"]["latitude"] == 35.68
    assert launch.await_args.kwargs["permissions"] == ["geolocation"]


@pytest.mark.asyncio
async def test_strict_proxy_resolution_failure_stops_launch(tmp_path: Path, monkeypatch):
    account = Account.model_validate({
        "acc": "a", "userDataDir": str(tmp_path / "profile"),
        "network": {
            "proxy": {"server": "socks5://127.0.0.1:1080"},
            "regionMode": "auto", "strictProxy": True,
        },
    })
    config = AccountsConfig(accounts=[account])
    launch = AsyncMock()
    resolve_identity = AsyncMock(side_effect=RuntimeError("proxy unavailable"))

    monkeypatch.setattr(cloakbrowser, "launch_persistent_context_async", launch)
    monkeypatch.setattr(session_module, "resolve_launch_identity", resolve_identity)
    monkeypatch.setattr(session_module, "kill_for_data_dir", lambda _path: [])
    monkeypatch.setattr(session_module, "resolve_cloak_exe", lambda _account, _config: "")

    with pytest.raises(RuntimeError, match="proxy unavailable"):
        await AccountSession(account, config).start()

    assert resolve_identity.await_count == 1
    launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_strict_resolution_failure_does_not_retry_at_launch(tmp_path: Path, monkeypatch):
    account = Account.model_validate({
        "acc": "a", "userDataDir": str(tmp_path / "profile"),
        "network": {
            "proxy": {"server": "socks5://127.0.0.1:1080"},
            "regionMode": "auto", "timezoneOverride": "Asia/Tokyo", "strictProxy": False,
        },
    })
    config = AccountsConfig(accounts=[account])
    context = type("Context", (), {"on": lambda self, *_args: None})()
    launch = AsyncMock(return_value=context)
    resolve_identity = AsyncMock(side_effect=RuntimeError("proxy unavailable"))

    monkeypatch.setattr(cloakbrowser, "launch_persistent_context_async", launch)
    monkeypatch.setattr(session_module, "resolve_launch_identity", resolve_identity)
    monkeypatch.setattr(session_module, "kill_for_data_dir", lambda _path: [])
    monkeypatch.setattr(session_module, "resolve_cloak_exe", lambda _account, _config: "")

    await AccountSession(account, config).start()

    assert resolve_identity.await_count == 1
    assert launch.await_args.kwargs["geoip"] is False
    assert launch.await_args.kwargs["timezone"] == "Asia/Tokyo"
    assert launch.await_args.kwargs["locale"] is None
    assert not any(
        arg.startswith("--fingerprint-webrtc-ip=")
        for arg in launch.await_args.kwargs["args"]
    )


@pytest.mark.asyncio
async def test_disabled_region_mode_skips_identity_resolution(tmp_path: Path, monkeypatch):
    account = Account.model_validate({
        "acc": "a", "userDataDir": str(tmp_path / "profile"),
        "network": {
            "proxy": {"server": "socks5://127.0.0.1:1080"},
            "regionMode": "disabled", "strictProxy": True,
        },
    })
    config = AccountsConfig(accounts=[account])
    context = type("Context", (), {"on": lambda self, *_args: None})()
    launch = AsyncMock(return_value=context)
    resolve_identity = AsyncMock()

    monkeypatch.setattr(cloakbrowser, "launch_persistent_context_async", launch)
    monkeypatch.setattr(session_module, "resolve_launch_identity", resolve_identity)
    monkeypatch.setattr(session_module, "kill_for_data_dir", lambda _path: [])
    monkeypatch.setattr(session_module, "resolve_cloak_exe", lambda _account, _config: "")

    await AccountSession(account, config).start()

    resolve_identity.assert_not_awaited()
    assert launch.await_args.kwargs["geoip"] is False
    assert launch.await_args.kwargs["timezone"] is None
    assert launch.await_args.kwargs["locale"] is None
    assert not any(
        arg.startswith("--fingerprint-webrtc-ip=")
        for arg in launch.await_args.kwargs["args"]
    )
