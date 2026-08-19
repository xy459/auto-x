"""REST API for account settings and browser lifecycle operations."""
from __future__ import annotations

import os
import re
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationError

from . import cloak
from .browser import session_registry
from .config import Account, AccountsConfig, ProxyConfig, store

router = APIRouter(prefix="/api")


class SettingsUpdate(BaseModel):
    cloakBrowserPath: str = ""
    cloakUserDataBase: str = ""
    extensionPaths: list[str] = Field(default_factory=list)


class BrowserBatchRequest(BaseModel):
    action: Literal["open", "close", "restart"]
    accounts: list[str]


def _validation_errors(exc: ValidationError) -> list[dict]:
    """Return JSON-safe Pydantic details (ctx may contain raw ValueError objects)."""
    return exc.errors(include_context=False)


def _new_acc_id() -> str:
    existing = {account.acc for account in store.accounts.accounts}
    while True:
        candidate = "acc-" + uuid.uuid4().hex[:8]
        if candidate not in existing:
            return candidate


def _public_account(account: Account) -> dict:
    data = account.model_dump()
    proxy = account.network.proxy
    if proxy:
        public_proxy = data["network"]["proxy"]
        public_proxy.pop("passwordRef", None)
        public_proxy["hasPassword"] = proxy.has_password
    data["proxyDisplay"] = account.proxy_display or None
    return data


def _resolved_browser_versions(accounts: list[Account]) -> dict[str, str | None]:
    """Resolve each unique official channel/version once for the account list."""
    cache: dict[tuple[str, str | None], str | None] = {}
    result: dict[str, str | None] = {}
    for account in accounts:
        if account.browserPath or store.accounts.cloakBrowserPath:
            result[account.acc] = account.browserVersion
            continue
        key = (account.releaseChannel, account.browserVersion)
        if key not in cache:
            try:
                cache[key] = cloak.browser_binary_info(*key).get("version")
            except Exception:  # noqa: BLE001
                cache[key] = account.browserVersion
        result[account.acc] = cache[key]
    return result


def _fingerprint_summary(account: Account, runtime: dict, browser_version: str | None) -> dict:
    runtime_fingerprint = runtime.get("fingerprint") or None
    if runtime_fingerprint:
        fingerprint = dict(runtime_fingerprint)
    elif account.network.regionMode == "disabled":
        fingerprint = {
            "source": "disabled", "webrtcIp": None, "region": None,
            "locale": None, "timezone": None, "stale": False,
        }
    else:
        check = account.network.lastCheck
        timezone_value = (
            (check.appliedTimezone or check.timezone) if check else None
        ) or account.network.timezoneOverride
        locale_value = (
            (check.appliedLocale or check.locale) if check else None
        ) or account.network.localeOverride
        fingerprint = {
            "source": "lastCheck" if check else (
                "configured" if timezone_value or locale_value else "notChecked"
            ),
            "webrtcIp": (check.webrtcIp or check.exitIp) if check else None,
            "region": cloak.region_label_for_timezone(timezone_value),
            "locale": locale_value,
            "timezone": timezone_value,
            "stale": bool(check.stale) if check else False,
        }

    fingerprint["browserVersion"] = fingerprint.get("browserVersion") or browser_version
    runtime_user_agent = fingerprint.get("userAgent")
    fingerprint["userAgent"] = runtime_user_agent or cloak.projected_user_agent(
        account.fpPlatform, fingerprint["browserVersion"], account.brandVersion,
    )
    fingerprint["userAgentSource"] = "runtime" if runtime_user_agent else "projected"
    if not fingerprint.get("region"):
        fingerprint["region"] = cloak.region_label_for_timezone(fingerprint.get("timezone"))
    return fingerprint


def _prepare_account_payload(body: dict, acc: str, existing: Account | None = None) -> dict:
    """Merge write-only proxy passwords without ever returning them to the UI."""
    payload = deepcopy(body)
    payload["acc"] = acc
    network = payload.get("network")
    if not isinstance(network, dict):
        return payload
    proxy = network.get("proxy")
    if not isinstance(proxy, dict):
        return payload
    proxy.pop("hasPassword", None)
    proxy.pop("passwordRef", None)
    password_supplied = "password" in proxy
    password = proxy.get("password")
    old_proxy = existing.network.proxy if existing else None
    same_identity = bool(
        old_proxy
        and str(proxy.get("server") or "").strip() == old_proxy.server
        and str(proxy.get("username") or "").strip() == (old_proxy.username or "")
    )
    if not password_supplied and same_identity:
        proxy["password"] = old_proxy.password
        proxy["passwordRef"] = old_proxy.passwordRef
    elif password:
        proxy["passwordRef"] = f"proxy:{acc}"
    else:
        proxy["password"] = None
        proxy["passwordRef"] = None
    return payload


def _mark_stale_network_check(account: Account) -> None:
    check = account.network.lastCheck
    if check and check.proxySignature != cloak.proxy_signature(account.network.proxy):
        check.stale = True


def _save_accounts(accounts: list[Account], *, browser_path: str | None = None,
                   user_base: str | None = None,
                   extension_paths: list[str] | None = None) -> AccountsConfig:
    payload = {
        "cloakBrowserPath": store.accounts.cloakBrowserPath if browser_path is None else browser_path,
        "cloakUserDataBase": store.accounts.cloakUserDataBase if user_base is None else user_base,
        "extensionPaths": (
            store.accounts.extensionPaths if extension_paths is None else extension_paths
        ),
        # Keep write-only in-memory proxy passwords attached while AccountsConfig
        # revalidates uniqueness. ConfigStore persists them in the secret store;
        # ProxyConfig.model_dump() deliberately excludes the password.
        "accounts": accounts,
    }
    try:
        config = AccountsConfig.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(400, _validation_errors(exc)) from exc
    store.save(config)
    return config


@router.get("/health")
def health():
    return {"ok": True, "architecture": "cloakbrowser-playwright-persistent"}


@router.get("/settings")
def settings():
    return {
        "cloakBrowserPath": store.accounts.cloakBrowserPath,
        "cloakUserDataBase": store.accounts.cloakUserDataBase,
        "extensionPaths": store.accounts.extensionPaths,
    }


@router.put("/settings")
def update_settings(body: SettingsUpdate):
    if any(session_registry.is_running(account.acc) for account in store.accounts.accounts):
        raise HTTPException(409, "有浏览器正在运行，请全部关闭后再修改全局设置")
    _save_accounts(store.accounts.accounts, browser_path=body.cloakBrowserPath,
                   user_base=body.cloakUserDataBase,
                   extension_paths=body.extensionPaths)
    return {"ok": True}


@router.get("/accounts")
def list_accounts():
    statuses = {item["acc"]: item for item in session_registry.status(store.accounts.accounts)}
    browser_versions = _resolved_browser_versions(store.accounts.accounts)
    return {
        "accounts": [
            {
                **_public_account(account),
                "runtime": statuses[account.acc],
                "fingerprint": _fingerprint_summary(
                    account, statuses[account.acc], browser_versions[account.acc]
                ),
            }
            for account in store.accounts.accounts
        ]
    }


@router.post("/accounts")
def add_account(body: dict):
    acc = _new_acc_id()
    payload = _prepare_account_payload(body, acc)
    try:
        account = Account.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(400, _validation_errors(exc)) from exc
    _mark_stale_network_check(account)
    _save_accounts([*store.accounts.accounts, account])
    return {"ok": True, "account": _public_account(account)}


@router.put("/accounts/{acc}")
def update_account(acc: str, body: dict):
    index = next((i for i, account in enumerate(store.accounts.accounts) if account.acc == acc), None)
    if index is None:
        raise HTTPException(404, f"未知账户: {acc}")
    if session_registry.is_running(acc):
        raise HTTPException(409, "浏览器正在运行，请先关闭后再修改配置")
    existing = store.accounts.accounts[index]
    try:
        account = Account.model_validate(_prepare_account_payload(body, acc, existing))
    except ValidationError as exc:
        raise HTTPException(400, _validation_errors(exc)) from exc
    _mark_stale_network_check(account)
    accounts = list(store.accounts.accounts)
    accounts[index] = account
    _save_accounts(accounts)
    return {"ok": True, "account": _public_account(account)}


@router.delete("/accounts/{acc}")
async def delete_account(acc: str):
    account = store.accounts.get(acc)
    if account is None:
        raise HTTPException(404, f"未知账户: {acc}")
    await session_registry.close(account)
    _save_accounts([item for item in store.accounts.accounts if item.acc != acc])
    return {"ok": True}


@router.get("/browser/status")
def browser_status():
    return {"accounts": session_registry.status(store.accounts.accounts)}


async def _run_browser_action(account: Account, action: str) -> dict:
    if action == "open":
        await session_registry.ensure_started(account, store.accounts)
        return {"running": True}
    if action == "close":
        return await session_registry.close(account)
    if action == "restart":
        await session_registry.restart(account, store.accounts)
        return {"running": True}
    raise ValueError(f"未知操作: {action}")


@router.post("/browser/batch")
async def browser_batch_action(body: BrowserBatchRequest):
    results = []
    seen = set()
    for acc in body.accounts:
        if acc in seen:
            continue
        seen.add(acc)
        account = store.accounts.get(acc)
        if account is None:
            results.append({"acc": acc, "ok": False, "error": f"未知账户: {acc}"})
            continue
        try:
            result = await _run_browser_action(account, body.action)
            results.append({"acc": acc, "ok": True, **result})
        except Exception as exc:  # noqa: BLE001
            results.append({"acc": acc, "ok": False, "error": str(exc)})
    return {
        "ok": all(item["ok"] for item in results),
        "action": body.action,
        "results": results,
    }


@router.post("/browser/{acc}/{action}")
async def browser_action(acc: str, action: str):
    account = store.accounts.get(acc)
    if account is None:
        raise HTTPException(404, f"未知账户: {acc}")
    try:
        return {"ok": True, **await _run_browser_action(account, action)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, str(exc)) from exc


@router.get("/cloak/options")
def cloak_options():
    return {
        "platforms": cloak.PLATFORM_OPTIONS,
        "defaultPlatform": cloak.default_platform(),
        "hostPlatformLabel": cloak.host_platform_label(),
        "releaseChannels": cloak.RELEASE_CHANNELS,
        "timezones": cloak.TIMEZONE_OPTIONS,
        "locales": cloak.LOCALE_OPTIONS,
        "sep": os.sep,
        "cloakBrowserPath": store.accounts.cloakBrowserPath,
        "cloakUserDataBase": store.accounts.cloakUserDataBase,
        "extensionPaths": store.accounts.extensionPaths,
    }


@router.get("/cloak/browser-info")
def cloak_browser_info(
    releaseChannel: Literal["stable", "preview"] = "stable",
    browserVersion: str | None = None,
):
    browser_version = (browserVersion or "").strip() or None
    if browser_version and not re.fullmatch(r"\d+(?:\.\d+){3,4}", browser_version):
        raise HTTPException(400, "CloakBrowser 精确版本格式应为四或五段数字")
    try:
        return cloak.browser_binary_info(releaseChannel, browser_version)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"无法解析 CloakBrowser 二进制信息: {exc}") from exc


def _probe_proxy(body: dict) -> tuple[ProxyConfig | None, str | None, str | None]:
    # Backward-compatible string payload for older clients.
    if isinstance(body.get("proxy"), str):
        raw = str(body["proxy"]).strip()
        if not raw:
            return None, None, None
        account = Account.model_validate({
            "acc": "probe", "userDataDir": os.path.abspath("probe"), "proxy": raw,
        })
        return account.network.proxy, None, None

    network = body.get("network") or {}
    proxy_data = deepcopy(network.get("proxy"))
    proxy = None
    if isinstance(proxy_data, dict):
        proxy_data.pop("hasPassword", None)
        proxy_data.pop("passwordRef", None)
        acc = str(body.get("acc") or "")
        existing = store.accounts.get(acc) if acc else None
        old_proxy = existing.network.proxy if existing else None
        if "password" not in proxy_data and old_proxy:
            same_identity = (
                str(proxy_data.get("server") or "").strip() == old_proxy.server
                and str(proxy_data.get("username") or "").strip() == (old_proxy.username or "")
            )
            if same_identity:
                proxy_data["password"] = old_proxy.password
        proxy = ProxyConfig.model_validate(proxy_data)
    return proxy, network.get("timezoneOverride") or None, network.get("localeOverride") or None


@router.post("/cloak/network-test")
@router.post("/cloak/proxy-test")
async def proxy_test(body: dict):
    try:
        proxy, timezone_override, locale_override = _probe_proxy(body)
        result = await cloak.probe_network_identity(proxy, timezone_override, locale_override)
        result["checkedAt"] = datetime.now(timezone.utc).isoformat()
        return result
    except ValidationError as exc:
        raise HTTPException(400, _validation_errors(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, str(exc)) from exc
