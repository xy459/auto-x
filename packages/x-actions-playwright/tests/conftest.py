from __future__ import annotations

import os
from pathlib import Path

import pytest_asyncio
from playwright.async_api import async_playwright


def _installed_chromium() -> str | None:
    override = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    if override:
        return override
    cache = Path.home() / "Library" / "Caches" / "ms-playwright"
    candidates = sorted(cache.glob("chromium_headless_shell-*/**/chrome-headless-shell"), reverse=True)
    return str(candidates[0]) if candidates else None


@pytest_asyncio.fixture
async def page():
    async with async_playwright() as playwright:
        executable = _installed_chromium()
        browser = await playwright.chromium.launch(headless=True, **({"executable_path": executable} if executable else {}))
        context = await browser.new_context()

        async def route_x(route):
            await route.fulfill(status=200, content_type="text/html", body="<!doctype html><title>X fixture</title>")

        await context.route("https://x.com/**", route_x)
        page = await context.new_page()
        await page.goto("https://x.com/home")
        yield page
        await context.close()
        await browser.close()
