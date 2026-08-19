"""Temporary ownership of a task-specific page inside an account browser."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..config import Account
    from .registry import SessionRegistry
    from .session import AccountSession

logger = logging.getLogger(__name__)


class BrowserPageLease:
    """Tracks pages created for one caller without owning unrelated profile tabs."""

    def __init__(
        self,
        *,
        registry: SessionRegistry,
        account: Account,
        session: AccountSession,
        page: Any,
        browser_was_started: bool,
    ) -> None:
        self.account = account
        self.session = session
        self.context = session.context
        self.page = page
        self.browser_was_started = browser_was_started
        self._owned_pages: list[Any] = [page]
        self._registry = registry
        self._released = False
        self._release_result: dict[str, Any] | None = None
        self._release_lock = asyncio.Lock()
        try:
            page.on("popup", self._track_popup)
        except Exception as exc:  # noqa: BLE001
            logger.debug("could not register popup ownership callback: %s", exc)

    def _track_popup(self, page: Any) -> None:
        if not self._released and page not in self._owned_pages:
            self._owned_pages.append(page)

    async def _close_owned_pages(self) -> list[str]:
        errors: list[str] = []
        processed: set[int] = set()
        while True:
            pending = [page for page in reversed(self._owned_pages) if id(page) not in processed]
            if not pending:
                break
            for page in pending:
                processed.add(id(page))
                try:
                    is_closed = getattr(page, "is_closed", None)
                    if callable(is_closed) and is_closed():
                        continue
                    await asyncio.wait_for(page.close(), timeout=10)
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc))
            # Let synchronous Playwright popup callbacks append pages opened
            # while their owner page was closing, then sweep them as well.
            await asyncio.sleep(0)
        return errors

    async def release(self, *, close_browser: bool = False) -> dict[str, Any]:
        """Release caller-owned pages and optionally close the whole account browser."""
        async with self._release_lock:
            if self._released:
                return dict(self._release_result or {})

            page_errors = await self._close_owned_pages()
            browser_result: dict[str, Any] | None = None
            if close_browser:
                try:
                    browser_result = await self._registry.close(self.account)
                except Exception as exc:  # noqa: BLE001
                    browser_result = {
                        "closed": False,
                        "closeError": str(exc),
                        "killed": [],
                    }

            self._release_result = {
                "released": True,
                "pageErrors": page_errors,
                "browser": browser_result,
            }
            self._released = True
            return dict(self._release_result)


__all__ = ["BrowserPageLease"]
