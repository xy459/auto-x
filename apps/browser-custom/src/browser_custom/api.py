"""REST API for account settings and browser lifecycle operations."""
from __future__ import annotations

import os
import re
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationError

from . import cloak
from .browser import session_registry
from .config import Account, AccountsConfig, ProxyConfig, store

router = APIRouter(prefix="/api")

JsonObject = dict[str, Any]


class SettingsUpdate(BaseModel):
    cloakBrowserPath: str = ""
    cloakUserDataBase: str = ""
    extensionPaths: list[str] = Field(default_factory=list)


class BrowserBatchRequest(BaseModel):
    action: Literal["open", "close", "restart"]
    accounts: list[str]


class AccountBatchCreateRequest(BaseModel):
    accounts: list[JsonObject] = Field(min_length=1, max_length=500)


def _validation_errors(exc: ValidationError) -> list[JsonObject]:
    """Return JSON-safe Pydantic details (ctx may contain raw ValueError objects)."""
    return [dict(item) for item in exc.errors(include_context=False)]


def _new_acc_id(reserved: set[str] | None = None) -> str:
    existing = {account.acc for account in store.accounts.accounts}
    existing.update(reserved or set())
    while True:
        candidate = "acc-" + uuid.uuid4().hex[:8]
        if candidate not in existing:
            return candidate


def _public_account(account: Account) -> JsonObject:
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


def _fingerprint_summary(
    account: Account, runtime: JsonObject, browser_version: str | None
) -> JsonObject:
    runtime_fingerprint = runtime.get("fingerprint") or None
    if runtime_fingerprint:
        fingerprint: JsonObject = dict(runtime_fingerprint)
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


def _prepare_account_payload(
    body: JsonObject, acc: str, existing: Account | None = None
) -> JsonObject:
    """Merge write-only proxy passwords without ever returning them to the UI."""
    payload = deepcopy(body)
    payload["acc"] = acc
    if existing and "xUsername" not in payload:
        payload["xUsername"] = existing.xUsername
    if existing and "autoAssignAvatar" not in payload:
        payload["autoAssignAvatar"] = existing.autoAssignAvatar
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
    if not password_supplied and same_identity and old_proxy is not None:
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


def _launch_configuration(account: Account) -> JsonObject:
    """Return account settings that affect the running browser session."""
    payload = account.model_dump(mode="json")
    payload.pop("xUsername", None)
    network = payload.get("network")
    if isinstance(network, dict):
        network.pop("lastCheck", None)
        proxy = network.get("proxy")
        if isinstance(proxy, dict) and account.network.proxy:
            proxy["password"] = account.network.proxy.password
    return payload


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
def health() -> JsonObject:
    return {"ok": True, "architecture": "cloakbrowser-playwright-persistent"}


@router.get("/settings")
def settings() -> JsonObject:
    return {
        "cloakBrowserPath": store.accounts.cloakBrowserPath,
        "cloakUserDataBase": store.accounts.cloakUserDataBase,
        "extensionPaths": store.accounts.extensionPaths,
    }


@router.put("/settings")
def update_settings(body: SettingsUpdate) -> JsonObject:
    if any(session_registry.is_running(account.acc) for account in store.accounts.accounts):
        raise HTTPException(409, "有浏览器正在运行，请全部关闭后再修改全局设置")
    _save_accounts(store.accounts.accounts, browser_path=body.cloakBrowserPath,
                   user_base=body.cloakUserDataBase,
                   extension_paths=body.extensionPaths)
    return {"ok": True}


@router.get("/accounts")
def list_accounts() -> JsonObject:
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
def add_account(body: JsonObject) -> JsonObject:
    acc = _new_acc_id()
    payload = _prepare_account_payload(body, acc)
    try:
        account = Account.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(400, _validation_errors(exc)) from exc
    _mark_stale_network_check(account)
    _save_accounts([*store.accounts.accounts, account])
    return {"ok": True, "account": _public_account(account)}


@router.post("/accounts/batch")
def add_accounts_batch(body: AccountBatchCreateRequest) -> JsonObject:
    created: list[Account] = []
    reserved = {account.acc for account in store.accounts.accounts}
    for line, raw in enumerate(body.accounts, 1):
        acc = _new_acc_id(reserved)
        reserved.add(acc)
        try:
            account = Account.model_validate(_prepare_account_payload(raw, acc))
        except ValidationError as exc:
            raise HTTPException(400, {
                "line": line,
                "name": str(raw.get("name") or "").strip() or None,
                "errors": _validation_errors(exc),
            }) from exc
        _mark_stale_network_check(account)
        created.append(account)
    _save_accounts([*store.accounts.accounts, *created])
    return {
        "ok": True,
        "count": len(created),
        "accounts": [_public_account(account) for account in created],
    }


@router.put("/accounts/{acc}")
def update_account(acc: str, body: JsonObject) -> JsonObject:
    index = next((i for i, account in enumerate(store.accounts.accounts) if account.acc == acc), None)
    if index is None:
        raise HTTPException(404, f"未知账户: {acc}")
    existing = store.accounts.accounts[index]
    try:
        account = Account.model_validate(_prepare_account_payload(body, acc, existing))
    except ValidationError as exc:
        raise HTTPException(400, _validation_errors(exc)) from exc
    if (
        session_registry.is_running(acc)
        and _launch_configuration(account) != _launch_configuration(existing)
    ):
        raise HTTPException(409, "浏览器正在运行；仅可保存网络检测记录，修改其他配置前请先关闭浏览器")
    _mark_stale_network_check(account)
    accounts = list(store.accounts.accounts)
    accounts[index] = account
    _save_accounts(accounts)
    return {"ok": True, "account": _public_account(account)}


@router.delete("/accounts/{acc}")
async def delete_account(acc: str) -> JsonObject:
    account = store.accounts.get(acc)
    if account is None:
        raise HTTPException(404, f"未知账户: {acc}")
    await session_registry.close(account)
    _save_accounts([item for item in store.accounts.accounts if item.acc != acc])
    return {"ok": True}


@router.get("/browser/status")
def browser_status() -> JsonObject:
    return {"accounts": session_registry.status(store.accounts.accounts)}


async def _run_browser_action(account: Account, action: str) -> JsonObject:
    if action == "open":
        return await session_registry.open(account, store.accounts)
    if action == "close":
        return await session_registry.close(account)
    if action == "restart":
        await session_registry.restart(account, store.accounts)
        return {"running": True}
    raise ValueError(f"未知操作: {action}")


@router.post("/browser/batch")
async def browser_batch_action(body: BrowserBatchRequest) -> JsonObject:
    results: list[JsonObject] = []
    seen: set[str] = set()
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
async def browser_action(acc: str, action: str) -> JsonObject:
    account = store.accounts.get(acc)
    if account is None:
        raise HTTPException(404, f"未知账户: {acc}")
    try:
        return {"ok": True, **await _run_browser_action(account, action)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@router.get("/cloak/options")
def cloak_options() -> JsonObject:
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
) -> JsonObject:
    browser_version = (browserVersion or "").strip() or None
    if browser_version and not re.fullmatch(r"\d+(?:\.\d+){3,4}", browser_version):
        raise HTTPException(400, "CloakBrowser 精确版本格式应为四或五段数字")
    try:
        return cloak.browser_binary_info(releaseChannel, browser_version)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"无法解析 CloakBrowser 二进制信息: {exc}") from exc


def _probe_proxy(body: JsonObject) -> tuple[ProxyConfig | None, str | None, str | None]:
    # Backward-compatible string payload for older clients.
    if isinstance(body.get("proxy"), str):
        raw = str(body["proxy"]).strip()
        if not raw:
            return None, None, None
        account = Account.model_validate({
            "acc": "probe", "userDataDir": os.path.abspath("probe"), "proxy": raw,
        })
        return account.network.proxy, None, None

    network_value = body.get("network")
    network: JsonObject = network_value if isinstance(network_value, dict) else {}
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
    timezone_value = network.get("timezoneOverride")
    locale_value = network.get("localeOverride")
    return (
        proxy,
        str(timezone_value) if timezone_value else None,
        str(locale_value) if locale_value else None,
    )


@router.post("/cloak/network-test")
@router.post("/cloak/proxy-test")
async def proxy_test(body: JsonObject) -> JsonObject:
    try:
        proxy, timezone_override, locale_override = _probe_proxy(body)
        result = await cloak.probe_network_identity(
            proxy, timezone_override, locale_override
        )
        result["checkedAt"] = datetime.now(UTC).isoformat()
        return result
    except ValidationError as exc:
        raise HTTPException(400, _validation_errors(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc
