"""FastAPI application assembly."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .api import router
from .browser import session_registry
from .config import store
from .environment import PACKAGE_ROOT

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    store.reload()
    yield
    await session_registry.close_all(store.accounts.accounts)


app = FastAPI(
    title="browser-custom",
    version="0.1.0",
    description="CloakBrowser + Playwright persistent profile manager",
    lifespan=lifespan,
)
app.include_router(router)
app.mount("/", StaticFiles(directory=str(PACKAGE_ROOT / "web"), html=True), name="web")
