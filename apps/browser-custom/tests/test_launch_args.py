from pathlib import Path
from types import SimpleNamespace

from browser_custom.browser.launch_args import build_cloak_args, resolve_cloak_exe
from browser_custom.browser.procutil import cloak_seed
from browser_custom.browser import procutil
from browser_custom.browser.session import _allow_profile_extensions
from browser_custom.config import Account, AccountsConfig


def test_seed_is_stable_and_account_specific():
    assert cloak_seed("acc-a") == cloak_seed("acc-a")
    assert cloak_seed("acc-a") != cloak_seed("acc-b")


def test_process_scan_permission_error_degrades_safely(tmp_path: Path, monkeypatch):
    def denied(*_args, **_kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(procutil.psutil, "process_iter", denied)
    assert procutil.find_main_pids_for(tmp_path) == []


def test_process_stats_many_scans_once_and_groups_process_trees(tmp_path: Path, monkeypatch):
    profile_a = tmp_path / "a"
    profile_b = tmp_path / "b"
    calls = 0
    processes = [
        SimpleNamespace(info={
            "pid": 100, "ppid": 1,
            "cmdline": ["chrome", f"--user-data-dir={profile_a}"],
        }),
        SimpleNamespace(info={
            "pid": 101, "ppid": 100,
            "cmdline": ["chrome", "--type=renderer"],
        }),
        SimpleNamespace(info={
            "pid": 102, "ppid": 101,
            "cmdline": ["chrome", "--type=utility"],
        }),
        SimpleNamespace(info={
            "pid": 200, "ppid": 1,
            "cmdline": ["chrome", "--user-data-dir", str(profile_b)],
        }),
        SimpleNamespace(info={
            "pid": 201, "ppid": 200,
            "cmdline": ["chrome", "--type=gpu-process"],
        }),
    ]

    def fake_process_iter(_attrs):
        nonlocal calls
        calls += 1
        return processes

    monkeypatch.setattr(procutil.psutil, "process_iter", fake_process_iter)

    stats = procutil.process_stats_many([profile_a, profile_b])

    assert calls == 1
    assert stats == [
        {"mainPids": [100], "processCount": 3},
        {"mainPids": [200], "processCount": 2},
    ]


def test_build_cloak_args_contains_fingerprint_fields(tmp_path: Path):
    account = Account(
        acc="acc-a", userDataDir=str(tmp_path / "a"),
        platformVersion="15.0.0", brandVersion="146.0.0.0",
    )
    args = build_cloak_args(account, webrtc_ip="203.0.113.8")
    assert f"--fingerprint={cloak_seed('acc-a')}" in args
    assert "--fingerprint-brand-version=146.0.0.0" in args
    assert not any(arg.startswith("--fingerprint-platform=") for arg in args)
    assert "--fingerprint-brand=Chrome" not in args
    assert "--fingerprint-webrtc-ip=203.0.113.8" in args
    assert not any(arg.startswith("--fingerprint-timezone") for arg in args)
    assert not any(arg.startswith("--fingerprint-locale") for arg in args)


def test_resolve_binary_directory(tmp_path: Path):
    browser_dir = tmp_path / "cloak"
    browser_dir.mkdir()
    account = Account(acc="a", userDataDir=str(tmp_path / "a"))
    config = AccountsConfig(cloakBrowserPath=str(browser_dir), accounts=[account])
    resolved = resolve_cloak_exe(account, config)
    assert Path(resolved).parent == browser_dir
    assert Path(resolved).name in {"chrome", "chrome.exe"}


def test_build_macos_platform_args(tmp_path: Path):
    account = Account(
        acc="mac", userDataDir=str(tmp_path / "mac"),
        fpPlatform="macos", platformVersion="14.0.0",
    )
    args = build_cloak_args(account)
    assert "--fingerprint-platform=macos" in args
    assert "--fingerprint-platform-version=14.0.0" in args


def test_fingerprint_flags_cannot_be_duplicated_in_extra_args(tmp_path: Path):
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="设备指纹设置"):
        Account(
            acc="a", userDataDir=str(tmp_path / "a"),
            cloakArgs=["--fingerprint-platform=windows"],
        )


def test_profile_extensions_remove_playwright_disable_default():
    cloak_browser = SimpleNamespace(
        IGNORE_DEFAULT_ARGS=["--enable-automation", "--enable-unsafe-swiftshader"]
    )

    _allow_profile_extensions(cloak_browser)
    _allow_profile_extensions(cloak_browser)

    assert cloak_browser.IGNORE_DEFAULT_ARGS.count("--disable-extensions") == 1
