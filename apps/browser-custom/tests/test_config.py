import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from browser_custom import config as config_module
from browser_custom.config import Account, AccountsConfig, ConfigStore


def account(tmp_path: Path, acc: str = "acc-a") -> Account:
    return Account(acc=acc, userDataDir=str(tmp_path / acc))


def test_account_requires_absolute_user_data_dir():
    with pytest.raises(ValidationError, match="绝对路径"):
        Account(acc="a", userDataDir="profiles/a")


def test_accounts_reject_shared_user_data_dir(tmp_path: Path):
    path = str(tmp_path / "shared")
    with pytest.raises(ValidationError, match="共用 userDataDir"):
        AccountsConfig(accounts=[Account(acc="a", userDataDir=path), Account(acc="b", userDataDir=path)])


def test_proxy_validation(tmp_path: Path):
    with pytest.raises(ValidationError, match="代理格式"):
        Account(acc="a", userDataDir=str(tmp_path / "a"), proxy="localhost:1080")
    valid = Account(acc="a", userDataDir=str(tmp_path / "a"), proxy="socks5://localhost:1080")
    assert valid.proxy == "socks5://localhost:1080"


def test_proxy_credentials_are_structured_and_excluded_from_config(tmp_path: Path):
    account = Account.model_validate({
        "acc": "a", "userDataDir": str(tmp_path / "a"),
        "network": {"proxy": {
            "server": "socks5://localhost:1080", "username": "user", "password": "secret",
        }},
    })
    assert account.proxy_value == {
        "server": "socks5://localhost:1080", "username": "user", "password": "secret",
    }
    dumped = account.model_dump()
    assert "password" not in dumped["network"]["proxy"]


def test_reserved_network_args_are_rejected(tmp_path: Path):
    with pytest.raises(ValidationError, match="网络与地区身份"):
        Account(
            acc="a", userDataDir=str(tmp_path / "a"),
            cloakArgs=["--fingerprint-webrtc-ip=203.0.113.8"],
        )
    with pytest.raises(ValidationError, match="已被 CloakBrowser 移除"):
        Account(
            acc="a", userDataDir=str(tmp_path / "a"),
            cloakArgs=["--fingerprint-location=1,2"],
        )


def test_manual_region_requires_timezone_and_locale(tmp_path: Path):
    with pytest.raises(ValidationError, match="必须同时设置"):
        Account.model_validate({
            "acc": "a", "userDataDir": str(tmp_path / "a"),
            "network": {"regionMode": "manual", "timezoneOverride": "Asia/Tokyo"},
        })


def test_platform_normalization_and_validation(tmp_path: Path):
    automatic = Account(acc="auto", userDataDir=str(tmp_path / "auto"))
    assert automatic.fpPlatform == "auto"
    account = Account(acc="a", userDataDir=str(tmp_path / "a"), fpPlatform="mac")
    assert account.fpPlatform == "macos"
    with pytest.raises(ValidationError, match="fpPlatform"):
        Account(acc="b", userDataDir=str(tmp_path / "b"), fpPlatform="linux")
    with pytest.raises(ValidationError, match="系统版本格式"):
        Account(acc="c", userDataDir=str(tmp_path / "c"), platformVersion="Sonoma")
    with pytest.raises(ValidationError, match="四段数字"):
        Account(acc="d", userDataDir=str(tmp_path / "d"), brandVersion="146")
    with pytest.raises(ValidationError, match="四或五段数字"):
        Account(acc="e", userDataDir=str(tmp_path / "e"), browserVersion="145.0")


def test_legacy_account_enabled_field_is_ignored(tmp_path: Path):
    account = Account.model_validate({
        "acc": "legacy",
        "userDataDir": str(tmp_path / "legacy"),
        "enabled": False,
    })

    assert "enabled" not in account.model_dump()


def test_legacy_fingerprint_platform_migrates_to_auto(tmp_path: Path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    host_platform = "macos" if sys.platform == "darwin" else "windows"
    other_platform = "windows" if host_platform == "macos" else "macos"
    path = config_dir / "accounts.json"
    path.write_text(json.dumps({
        "accounts": [
            {"acc": "same", "userDataDir": str(tmp_path / "same"), "fpPlatform": host_platform},
            {"acc": "override", "userDataDir": str(tmp_path / "override"), "fpPlatform": other_platform},
        ]
    }))

    config_store = ConfigStore(config_dir)

    assert config_store.accounts.schemaVersion == 2
    assert config_store.accounts.get("same").fpPlatform == "auto"
    assert config_store.accounts.get("override").fpPlatform == other_platform
    assert (config_dir / "accounts.json.v2.bak").is_file()
    assert json.loads(path.read_text())["schemaVersion"] == 2


def test_config_store_round_trip(tmp_path: Path):
    store = ConfigStore(tmp_path / "config")
    extension = tmp_path / "extension"
    config = AccountsConfig(
        cloakBrowserPath=str(tmp_path / "cloak"),
        extensionPaths=[str(extension)],
        accounts=[account(tmp_path)],
    )
    store.save(config)
    reloaded = ConfigStore(tmp_path / "config")
    assert reloaded.accounts.accounts[0].acc == "acc-a"
    assert reloaded.accounts.cloakBrowserPath == str(tmp_path / "cloak")
    assert reloaded.accounts.extensionPaths == [str(extension)]


def test_config_store_persists_network_check_datetime(tmp_path: Path):
    store = ConfigStore(tmp_path / "config")
    checked = Account.model_validate({
        "acc": "checked",
        "userDataDir": str(tmp_path / "checked"),
        "network": {"lastCheck": {"checkedAt": "2026-08-19T10:00:00Z"}},
    })

    store.save(AccountsConfig(accounts=[checked]))
    raw = json.loads(store.path.read_text())
    reloaded = ConfigStore(tmp_path / "config")

    assert raw["accounts"][0]["network"]["lastCheck"]["checkedAt"] == "2026-08-19T10:00:00Z"
    assert reloaded.accounts.get("checked").network.lastCheck.checkedAt.isoformat() == "2026-08-19T10:00:00+00:00"


def test_config_store_keeps_old_snapshot_when_atomic_write_fails(tmp_path: Path, monkeypatch):
    store = ConfigStore(tmp_path / "config")
    original = AccountsConfig(accounts=[account(tmp_path, "original")])
    store.save(original)
    original_disk = store.path.read_text()

    def fail_write(_path, _payload):
        raise OSError("disk full")

    monkeypatch.setattr(config_module, "_atomic_write", fail_write)
    replacement = AccountsConfig(accounts=[account(tmp_path, "replacement")])

    with pytest.raises(OSError, match="disk full"):
        store.save(replacement)

    assert store.accounts.get("original") is not None
    assert store.accounts.get("replacement") is None
    assert store.path.read_text() == original_disk


def test_extension_paths_require_absolute_and_remove_duplicates(tmp_path: Path):
    with pytest.raises(ValidationError, match="插件目录必须是绝对路径"):
        AccountsConfig(extensionPaths=["extensions/my-extension"])
    extension = str(tmp_path / "extension")
    config = AccountsConfig(extensionPaths=[extension, extension])
    assert config.extensionPaths == [extension]
