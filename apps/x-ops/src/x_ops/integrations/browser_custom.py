from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from inspect import isawaitable
from typing import Any, Protocol

from ..models import CleanupReport


class BrowserLease(Protocol):
    @property
    def page(self) -> Any: ...

    @property
    def browser_was_started(self) -> bool: ...

    async def release(self, *, close_browser: bool) -> CleanupReport: ...


class BrowserGateway(Protocol):
    async def acquire(self, *, browser_account_id: str, task_run_id: str) -> BrowserLease: ...


class _MemoryPage:
    def __init__(self, name: str) -> None:
        self.name = name
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@dataclass(slots=True)
class _MemoryLease:
    page: Any
    browser_was_started: bool
    gateway: InMemoryBrowserGateway
    browser_account_id: str
    released: bool = False

    async def release(self, *, close_browser: bool) -> CleanupReport:
        if self.released:
            return CleanupReport()
        self.released = True
        warnings: list[dict[str, Any]] = []
        try:
            close = getattr(self.page, "close", None)
            if close:
                result = close()
                if hasattr(result, "__await__"):
                    await result
        except Exception as exc:
            warnings.append({"code": "TASK_PAGE_CLOSE_FAILED", "message": str(exc)})
        if close_browser:
            self.gateway.running_accounts.discard(self.browser_account_id)
        self.gateway.active_leases -= 1
        self.gateway.release_count += 1
        return CleanupReport(tuple(warnings))


class InMemoryBrowserGateway:
    """Fake gateway with observable lease accounting and no Playwright dependency."""

    def __init__(self, page_factory: Callable[[str, str], Any] | None = None) -> None:
        self.page_factory = page_factory or (lambda account, run: _MemoryPage(f"{account}:{run}"))
        self.running_accounts: set[str] = set()
        self.active_leases = 0
        self.max_active_leases = 0
        self.acquire_count = 0
        self.release_count = 0
        self.fail_acquire: Exception | None = None

    async def acquire(self, *, browser_account_id: str, task_run_id: str) -> BrowserLease:
        if self.fail_acquire:
            raise self.fail_acquire
        was_started = browser_account_id not in self.running_accounts
        self.running_accounts.add(browser_account_id)
        self.active_leases += 1
        self.max_active_leases = max(self.max_active_leases, self.active_leases)
        self.acquire_count += 1
        page = self.page_factory(browser_account_id, task_run_id)
        return _MemoryLease(page, was_started, self, browser_account_id)


@dataclass(slots=True)
class _BrowserCustomLeaseAdapter:
    _lease: Any

    @property
    def page(self) -> Any:
        return self._lease.page

    @property
    def browser_was_started(self) -> bool:
        return bool(self._lease.browser_was_started)

    async def release(self, *, close_browser: bool) -> CleanupReport:
        result = await self._lease.release(close_browser=close_browser)
        warnings: list[dict[str, Any]] = [
            {"code": "TASK_PAGE_CLOSE_FAILED", "message": str(message)}
            for message in result.get("pageErrors", [])
        ]
        browser = result.get("browser") or {}
        if browser.get("closeError"):
            warnings.append(
                {"code": "BROWSER_CLOSE_FAILED", "message": str(browser["closeError"])}
            )
        return CleanupReport(tuple(warnings))


class BrowserCustomGateway:
    """In-process adapter for browser-custom's existing SessionRegistry.

    ``account_resolver`` returns ``(browser_custom Account, AccountsConfig)``.
    Page objects never cross a JSON/HTTP boundary.
    """

    def __init__(
        self,
        account_resolver: Callable[[str], Awaitable[tuple[Any, Any]] | tuple[Any, Any]],
        *,
        registry: Any | None = None,
    ) -> None:
        if registry is None:
            from browser_custom.browser.registry import session_registry

            registry = session_registry
        self._registry = registry
        self._account_resolver = account_resolver

    async def acquire(self, *, browser_account_id: str, task_run_id: str) -> BrowserLease:
        resolved = self._account_resolver(browser_account_id)
        if isawaitable(resolved):
            resolved = await resolved
        account, config = resolved
        lease = await self._registry.acquire_page(account, config)
        return _BrowserCustomLeaseAdapter(lease)
